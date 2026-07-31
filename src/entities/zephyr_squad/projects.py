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
                created = await self.pools.qs(
                    self.qase.create_project, name, "", forced_code, self.mappings.group_id
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
        code = await self._create_project(name, "")
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

    async def _create_project(self, title: str, description: str) -> Optional[str]:
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
        if await self.pools.qs(
            self.qase.create_project, title, description, code, self.mappings.group_id
        ):
            return code
        return None
