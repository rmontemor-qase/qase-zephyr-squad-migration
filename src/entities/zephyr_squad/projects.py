import asyncio
import re
from typing import Optional

from ...service.qase import QaseService
from ...service.zephyr_squad import ZephyrSquadService
from ...support.config_manager import ConfigManager
from ...support.logger import Logger
from ...support.mappings import Mappings
from ...support.pools import Pools


class Projects:
    """Import Zephyr-enabled Jira projects → Qase projects.

    Source: ZAPI ``GET /util/project-list``, which returns each project with its
    numeric ``id``, ``key`` and ``name`` inline. The numeric id is carried on the
    mapping entry (``zephyr_project_id``) because cycles and versions are queried
    by project id, not key.
    """

    def __init__(
        self,
        qase_service: QaseService,
        source_service: ZephyrSquadService,
        logger: Logger,
        mappings: Mappings,
        config: ConfigManager,
        pools: Pools,
    ):
        self.qase = qase_service
        self.zephyr = source_service
        self.config = config
        self.logger = logger
        self.mappings = mappings
        self.pools = pools
        self.existing_codes = set()
        self.existing_projects_by_title = {}
        self.logger.divider()

    def import_projects(self) -> Mappings:
        return asyncio.run(self._import_projects_async())

    async def _import_projects_async(self) -> Mappings:
        self.logger.log("[Projects] Importing projects (config keys resolved via Jira)")

        await self._load_existing_qase_projects()

        # ZAPI Cloud has NO project-list endpoint. Projects are taken from
        # config.projects.import (explicit Jira project keys) and resolved to
        # their numeric id + name via Jira. import_all is unsupported for Squad
        # Cloud (no enumeration source) — require an explicit list.
        import_all = bool(self.config.get("projects.import_all"))
        keys = [str(p).strip() for p in (self.config.get("projects.import") or []) if str(p).strip()]
        exclude = {
            str(p).strip() for p in (self.config.get("projects.exclude") or []) if str(p).strip()
        }
        if import_all:
            self.logger.log(
                "[Projects] projects.import_all is not supported for Zephyr Squad Cloud "
                "(no project-list endpoint) — list the Jira project keys in "
                "projects.import instead",
                "warning",
            )
        keys = [k for k in keys if k not in exclude]
        if not keys:
            self.logger.log("[Projects] No project keys in projects.import — nothing to do", "error")
            return self.mappings

        total = len(keys)
        self.logger.print_status("Importing projects", total=total)
        await asyncio.gather(*[
            self._import_project(i, key, total) for i, key in enumerate(keys)
        ])
        return self.mappings

    async def _import_project(self, i: int, key: str, total: int):
        proj = await self.pools.source(self.zephyr.get_jira_project, key)
        pid = proj.get("id")
        name = (proj.get("name") or "").strip() or key
        if not pid:
            self.logger.log(
                f"[Projects] Could not resolve Jira project {key!r} (check the key and "
                f"Jira access) — skipping", "error",
            )
            self.mappings.stats.add_issue(key, "project", f"{key}: not found in Jira — skipped", "error")
            self.logger.print_status("Importing projects", i + 1, total)
            return

        def _record(code: str):
            self.mappings.projects.append(
                {"zephyr_key": key, "zephyr_project_id": pid, "name": name, "code": code}
            )
            self.mappings.project_map[key] = code
            self.mappings.stats.add_project(code, name)

        # Explicit target Qase project code from config.projects.mapping
        projects_mapping = self.config.get("projects.mapping") or {}
        forced_code = projects_mapping.get(key) or projects_mapping.get(name)
        if forced_code:
            # Historically a mapping meant "import into a PRE-EXISTING Qase
            # project". We now create the mapped project when it is absent, so a
            # mapping can deterministically target a fresh project (e.g. to
            # isolate a pilot) without relying on title-matching — which would
            # otherwise silently reuse a same-titled project.
            if forced_code.upper() in self.existing_codes:
                self.logger.log(
                    f"[Projects] Using mapped Qase project [{forced_code}] for Zephyr project {name} [{key}]"
                )
            else:
                gid = await self._ensure_access_group(key, name)
                created = await self.pools.qs(
                    self.qase.create_project, self._title(name), "", forced_code,
                    gid if gid is not None else self.mappings.group_id,
                    self.config.get("projects.access") or "all",
                )
                if not created:
                    self.logger.log(
                        f"[Projects] Failed to create mapped Qase project [{forced_code}] "
                        f"for {name} [{key}]", "error",
                    )
                    self.mappings.stats.add_issue(
                        forced_code, "project", f"{forced_code}: mapped-project create failed", "error"
                    )
                    self.logger.print_status("Importing projects", i + 1, total)
                    return
                self.existing_codes.add(forced_code.upper())
                self.logger.log(
                    f"[Projects] Created mapped Qase project [{forced_code}] for Zephyr project {name} [{key}]"
                )
            _record(forced_code)
            self.logger.print_status("Importing projects", i + 1, total)
            return

        self.logger.log(f"[Projects] Importing project: {name} [{key}] (zephyr id={pid})")
        code = await self._create_project(self._title(name), "", source_key=key)
        if code:
            _record(code)
        else:
            self.logger.log(f"[Projects] Failed to create/find project: {name}", "error")

        self.logger.print_status("Importing projects", i + 1, total)

    async def _load_existing_qase_projects(self):
        limit, offset = 100, 0
        while True:
            result = await self.pools.qs(self.qase.get_projects, limit, offset)
            entities = getattr(result, "entities", None) if result else None
            if not entities:
                break
            for p in entities:
                code = getattr(p, "code", None)
                title = getattr(p, "title", None)
                if code:
                    self.existing_codes.add(code.upper())
                    if title:
                        self.existing_projects_by_title[title.strip().lower()] = code
            if len(entities) < limit:
                break
            offset += limit
        self.logger.log(
            f"[Projects] Found {len(self.existing_projects_by_title)} existing Qase project(s)"
        )

    def _match_existing_project(self, title: str) -> Optional[str]:
        return self.existing_projects_by_title.get((title or "").strip().lower())

    def _short_code(self, s: str) -> str:
        s = s.replace("-", " ")
        s = re.sub("[^a-zA-Z ]", "", s)
        words = s.split()
        if len(words) > 1:
            code = "".join(word[0] for word in words).upper()
        else:
            code = s.upper()
        code = code.replace(" ", "")[:10]
        original_code = code
        postfix = ""
        while code in self.existing_codes or len(code) < 2:
            postfix = self._next_postfix(postfix)
            code = (original_code[: 10 - len(postfix)] + postfix).upper()
        self.existing_codes.add(code)
        return code

    def _next_postfix(self, postfix: str) -> str:
        if not postfix:
            return "A"
        elif postfix[-1] == "Z":
            return self._next_postfix(postfix[:-1]) + "A"
        else:
            return postfix[:-1] + chr(ord(postfix[-1]) + 1)


    def _title(self, name: str) -> str:
        """Project title, plus ``projects.title_suffix`` when set.

        A Qase project cannot be renamed through the API (/project/{code} is
        GET, HEAD and DELETE only), so the title has to be right at creation.
        Used when the same source project is migrated twice under different
        settings and the two need telling apart.
        """
        suffix = (self.config.get("projects.title_suffix") or "").strip()
        return f"{name} {suffix}".strip() if suffix else name

    async def _ensure_access_group(self, key: str, name: str):
        """Create a SCIM group holding exactly the people who worked on this
        Zephyr project, and return its id for ``access: "group"``.

        Qase takes a group, never a user list, and project access cannot be
        changed after creation, so this has to happen before the project exists.
        Membership comes from ``users.source_file`` filtered to this project key.
        The workspace owner keeps access regardless of group membership.
        """
        if not self.config.get("projects.access_group"):
            return None
        # A dry run must write NOTHING. The SCIM client is a real client, not the
        # dry-run shim, so creating a group here would be a genuine side effect.
        if type(self.qase).__name__.startswith("DryRun"):
            self.logger.log(
                f"[Projects] DRY RUN: would create an access group for {key} "
                f"(no SCIM group created)")
            return None
        import json as _json, os as _os
        from ...service.qase_scim import QaseScimService
        path = self.config.get("users.source_file")
        if not path or not _os.path.exists(path):
            self.logger.log(
                f"[Projects] projects.access_group is on but users.source_file is "
                f"missing ({path!r}) — creating {key} with the default access", "error")
            return None
        with open(path, encoding="utf-8") as fh:
            people = _json.load(fh)
        wanted = sorted({
            (u.get("email") or "").strip().lower() for u in people.values()
            if (u.get("type") or "atlassian") == "atlassian"
            and key in (u.get("projects") or [])
            and (u.get("email") or "").strip()
        })
        if not wanted:
            self.logger.log(
                f"[Projects] no users recorded for {key} — creating with default access",
                "warning")
            return None
        scim = QaseScimService(self.config, self.logger)
        ids, missing = [], []
        for email in wanted:
            try:
                found = await self.pools.qs(scim.find_user, email)
            except Exception as e:
                self.logger.log(f"[Projects] SCIM lookup failed for {email}: {e}", "warning")
                found = None
            (ids.append(found["id"]) if found and found.get("id") else missing.append(email))
        if missing:
            self.logger.log(
                f"[Projects] {len(missing)} of {len(wanted)} {key} user(s) are not members of "
                f"this workspace and will NOT get access: {missing[:5]}", "error")
        if not ids:
            return None
        try:
            gid = await self.pools.qs(
                scim.create_group_with_members,
                self.config.get("projects.access_group_prefix") or "", key, ids)
        except Exception as e:
            self.logger.log(
                f"[Projects] could not create the access group for {key}: {e} — "
                f"creating with the default access", "error")
            return None
        self.logger.log(
            f"[Projects] access group for {key}: {len(ids)} of {len(wanted)} user(s) added "
            f"(group {gid})")
        return gid

    async def _create_project(self, title: str, description: str,
                              source_key: str = None) -> Optional[str]:
        existing = self._match_existing_project(title)
        if existing:
            self.logger.log(
                f"[Projects] Reusing existing Qase project: {title} [{existing}]"
            )
            self.mappings.stats.add_issue(
                existing, "project",
                f"importing into pre-existing Qase project [{existing}] (title match "
                f"on {title!r})",
            )
            self.existing_codes.add(existing.upper())
            return existing
        code = self._short_code(title)
        gid = await self._ensure_access_group(source_key, title) if source_key else None
        if await self.pools.qs(
            self.qase.create_project, title, description, code,
            gid if gid is not None else self.mappings.group_id,
            self.config.get("projects.access") or "all",
        ):
            return code
        return None
