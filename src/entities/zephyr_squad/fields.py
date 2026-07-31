import asyncio

from ...service.qase import QaseService
from ...service.zephyr_squad import ZephyrSquadService
from ...support.config_manager import ConfigManager
from ...support.logger import Logger
from ...support.mappings import Mappings
from ...support.pools import Pools

# Zephyr Squad execution status NAME → Qase result status slug.
# Confirmed live on Zephyr Squad Cloud: PASS/FAIL/WIP/BLOCKED/SKIPPED/UNEXECUTED.
_EXEC_STATUS_NAME_TO_QASE = {
    "pass": "passed",
    "passed": "passed",
    "fail": "failed",
    "failed": "failed",
    "wip": "in_progress",
    "work in progress": "in_progress",
    "blocked": "blocked",
    "skipped": "skipped",
    "unexecuted": "untested",
    "not executed": "untested",
}


class Fields:
    """Register Qase system-field option ids and build the Zephyr→Qase status map.

    Kept deliberately small for classic Squad: test cases are Jira issues, so
    there is no Zephyr custom-field catalogue to mirror here (Jira custom fields
    are out of scope for the initial case migration). This step:
      1. Registers Qase priority/case-status option ids (for bulk case create).
      2. Builds mappings.result_statuses = {zephyr_status_id(str) → Qase result
         slug} from the live execution statuses (used later by the runs step).
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

    def import_fields(self) -> Mappings:
        return asyncio.run(self._import_fields_async())

    async def _import_fields_async(self) -> Mappings:
        # 1 — Qase system field option ids (priority / case status)
        self.logger.log("[Fields] Registering Qase system fields (priority/status options)")
        system_fields = await self.pools.qs(self.qase.get_system_fields)
        if system_fields:
            fields_as_dicts = [
                f.to_dict() if hasattr(f, "to_dict") else f
                for f in system_fields
                if hasattr(f, "to_dict") or isinstance(f, dict)
            ]
            self.mappings.register_qase_system_fields(fields_as_dicts)
            self.logger.log(
                f"[Fields] priority options: {list(self.mappings.qase_priority_keys_to_id)[:8]}; "
                f"status options: {list(self.mappings.qase_case_status_keys_to_id)[:8]}"
            )
        else:
            self.logger.log("[Fields] No system fields returned from Qase", "warning")

        # 2 — Zephyr execution status id → Qase result slug (for runs, later)
        overrides = {str(k): str(v).lower() for k, v in (self.config.get("runs.status_map") or {}).items()}
        status_map = {}
        try:
            for st in self.zephyr.get_execution_statuses():
                sid = st.get("id")
                name = (st.get("name") or "").strip().lower()
                if sid is None:
                    continue
                slug = overrides.get(str(sid)) or _EXEC_STATUS_NAME_TO_QASE.get(name)
                if slug:
                    status_map[str(sid)] = slug
        except Exception as e:
            self.logger.log(f"[Fields] Could not load execution statuses: {e}", "warning")
        self.mappings.result_statuses = status_map
        self.logger.log(f"[Fields] Zephyr→Qase result status map: {status_map}")

        # 3 — ensure the "Jira Reference" custom field exists on the target projects
        await self._ensure_jira_ref_field()

        # 4 — per-project component field ("Feature Area"), feature-flagged
        await self._ensure_component_field()

        # 5 — fixed values for the target's REQUIRED custom fields
        await self._ensure_required_field_defaults()

        # 6 — index the users that already exist in Qase (authorship retention)
        await self._load_qase_users()

        return self.mappings

    @staticmethod
    def _field_flags(cf) -> dict:
        """Current flags of an existing custom field, to resend on update.

        A Qase custom-field update must carry the FULL option list, and omitting
        the flags would reset them — which on a workspace-global field would
        silently change its scope. So they are always echoed back unchanged.
        """
        out = {}
        for attr in (
            "is_required", "is_enabled_for_all_projects",
            "is_visible", "is_filterable", "placeholder", "default_value",
        ):
            value = getattr(cf, attr, None)
            if value is not None:
                out[attr] = value
        codes = getattr(cf, "projects_codes", None)
        if codes:
            out["projects_codes"] = [str(c) for c in codes]
        return out

    async def _add_field_options(self, cf, cf_id: int, missing: list) -> bool:
        """Append option(s) to an existing select/multiselect field, preserving its
        existing options (with their ids) and all of its flags."""
        current = await self.pools.qs(self.qase.get_custom_field_options, cf_id) or []
        value = [{"id": o["id"], "title": o["title"]} for o in current]
        value += [{"title": t} for t in missing]
        payload = {"title": (getattr(cf, "title", "") or "").strip(), "value": value}
        payload.update(self._field_flags(cf))
        return bool(await self.pools.qs(self.qase.update_custom_field, cf_id, payload))

    _OPTION_FIELD_TYPES = ("selectbox", "multiselect", "radio")

    @staticmethod
    def _field_applies_to(cf, code: str) -> bool:
        """True when a custom field is actually usable in this Qase project.

        A field is valid only for the projects it is scoped to. Sending one that is
        not enabled for the target project makes Qase reject the ENTIRE case with
        "The field name is not valid." — so scope must be checked, not just title.
        """
        if bool(getattr(cf, "is_enabled_for_all_projects", False)):
            return True
        codes = [str(c) for c in (getattr(cf, "projects_codes", None) or [])]
        return code in codes

    async def _ensure_required_field_defaults(self):
        """Resolve ``features.required_field_defaults`` per project.

        The target workspace can have REQUIRED custom fields that Zephyr Squad has
        no equivalent for (so every case would 422). This stamps an explicit,
        configured value on every migrated case instead.

        Config keys are field TITLES, matched against fields that are both
        ``is_required`` AND in scope for the project being migrated. Resolution is
        per project because scope differs per project — a field required-and-global
        for one project may not exist in another. Option-based fields resolve the
        configured value to an option id (adding the option when missing); other
        types take the configured value literally.
        """
        wanted = self.config.get("features.required_field_defaults") or {}
        if not wanted:
            return
        try:
            existing = await self.pools.qs(self.qase.get_case_custom_fields) or []
        except Exception as e:
            self.logger.log(f"[Fields] Could not list Qase custom fields: {e}", "warning")
            return

        for project in self.mappings.projects:
            code = project.get("code")
            if not code:
                continue
            resolved = self.mappings.required_field_defaults.setdefault(code, {})
            for field_title, wanted_value in wanted.items():
                title = str(field_title).strip()
                wanted_value = str(wanted_value).strip()
                if not title or not wanted_value:
                    continue
                match = None
                for cf in existing:
                    if (getattr(cf, "title", "") or "").strip().lower() != title.lower():
                        continue
                    if not bool(getattr(cf, "is_required", False)):
                        continue
                    if not self._field_applies_to(cf, code):
                        continue
                    match = cf
                    break
                if match is None:
                    self.logger.log(
                        f"[{code}][Fields] required_field_defaults: no REQUIRED field titled "
                        f"{title!r} in scope for this project — skipping (nothing to satisfy)"
                    )
                    continue
                cf_id = int(getattr(match, "id"))
                ftype = str(getattr(match, "type", "") or "").strip().lower()

                if ftype not in self._OPTION_FIELD_TYPES:
                    resolved[str(cf_id)] = wanted_value
                    self.logger.log(
                        f"[{code}][Fields] Required field {title!r} (id={cf_id}, {ftype}) → "
                        f"every case gets {wanted_value!r}"
                    )
                    continue

                options = await self.pools.qs(self.qase.get_custom_field_options, cf_id) or []
                by_title = {o["title"].strip().lower(): o["id"] for o in options}
                option_id = by_title.get(wanted_value.lower())
                if option_id is None:
                    ok = await self._add_field_options(match, cf_id, [wanted_value])
                    if ok:
                        options = await self.pools.qs(
                            self.qase.get_custom_field_options, cf_id
                        ) or []
                        by_title = {o["title"].strip().lower(): o["id"] for o in options}
                        option_id = by_title.get(wanted_value.lower())
                    if option_id is None:
                        self.logger.log(
                            f"[{code}][Fields] Could not provide option {wanted_value!r} on "
                            f"required field {title!r} (id={cf_id}) — cases will fail validation",
                            "error",
                        )
                        continue
                    self.logger.log(
                        f"[{code}][Fields] Added option {wanted_value!r} to required field "
                        f"{title!r} (id={cf_id})"
                    )
                resolved[str(cf_id)] = str(option_id)
                self.logger.log(
                    f"[{code}][Fields] Required field {title!r} (id={cf_id}) → every migrated "
                    f"case gets {wanted_value!r} (option {option_id})"
                )

    async def _load_qase_users(self):
        """Index existing Qase workspace users for authorship matching.

        No user is ever created: source users are matched onto these by email,
        else display name, and anything unmatched falls back to ``users.default``.
        """
        resolve = self.config.get("users.resolve_from_jira")
        if resolve is not None and not bool(resolve):
            self.logger.log("[Fields] users.resolve_from_jira is off — all authorship → default user")
            return
        try:
            authors = await self.pools.qs(self.qase.get_all_users_sync)
        except Exception:
            authors = None
        if not authors:
            # get_all_users_sync is brittle in some SDK versions; fall back to one page
            try:
                authors = await self.pools.qs(self.qase._get_users, 100, 0)
            except Exception as e:
                self.logger.log(f"[Fields] Could not list Qase users: {e}", "warning")
                return
        self.mappings.register_qase_users(authors or [])
        self.logger.log(
            f"[Fields] Indexed {len(self.mappings.qase_user_by_email)} Qase user(s) by email, "
            f"{len(self.mappings.qase_user_by_name)} by name (authorship matching; no users created)"
        )

    async def _ensure_component_field(self):
        """Create (or reuse) a PER-PROJECT multiselect custom field holding each
        test's Jira components — feature flag ``features.component_custom_field``.

        One field per Qase project, scoped to that project only, whose options are
        that project's Jira component names. This is deliberately NOT a single
        workspace-wide field: each project keeps its own component taxonomy.
        A pre-existing field with the same title that is enabled for ALL projects
        (e.g. a customer's own global field) is never reused — only a field already
        scoped to this project is.
        """
        if not bool(self.config.get("features.component_custom_field")):
            return
        title = str(self.config.get("features.component_field_title") or "Feature Area").strip()
        try:
            existing = await self.pools.qs(self.qase.get_case_custom_fields) or []
        except Exception as e:
            self.logger.log(f"[Fields] Could not list Qase custom fields: {e}", "warning")
            existing = []

        for project in self.mappings.projects:
            code, zephyr_key = project.get("code"), project.get("zephyr_key")
            if not code or not zephyr_key:
                continue
            components = await self.pools.source(self.zephyr.get_component_names, zephyr_key) or {}
            names = sorted({(n or "").strip() for n in components.values() if (n or "").strip()})
            if not names:
                self.logger.log(
                    f"[{code}][Fields] No Jira components — skipping '{title}' field", "warning"
                )
                continue
            # Placeholder option for tests that carry no Jira component at all
            fallback = str(
                self.config.get("features.component_field_fallback_option") or ""
            ).strip()
            if fallback and fallback.lower() not in {n.lower() for n in names}:
                names.append(fallback)

            cf_id, current, reuse_cf = None, [], None
            for cf in existing:
                if (getattr(cf, "title", "") or "").strip().lower() != title.lower():
                    continue
                if bool(getattr(cf, "is_enabled_for_all_projects", False)):
                    continue  # a global field of the same name is NOT our per-project one
                codes = [str(c) for c in (getattr(cf, "projects_codes", None) or [])]
                if code in codes:
                    cf_id = int(getattr(cf, "id"))
                    reuse_cf = cf
                    current = await self.pools.qs(self.qase.get_custom_field_options, cf_id) or []
                    break

            if cf_id is None:
                cf_id = await self.pools.qs(self.qase.create_custom_field, {
                    "title": title, "entity": 0,
                    "type": self.mappings.qase_fields_type["multiselect"],
                    "is_required": False, "is_visible": True, "is_filterable": True,
                    "is_enabled_for_all_projects": False, "projects_codes": [code],
                    "value": [{"title": n} for n in names],
                })
                if not cf_id:
                    self.logger.log(
                        f"[{code}][Fields] Could not create '{title}' field — components "
                        f"will not be migrated for this project", "error",
                    )
                    continue
                self.logger.log(
                    f"[{code}][Fields] Created per-project '{title}' CF (id={cf_id}) "
                    f"with {len(names)} component option(s)"
                )
            else:
                have = {o["title"].strip().lower() for o in current}
                missing = [n for n in names if n.lower() not in have]
                if missing:
                    ok = await self._add_field_options(reuse_cf, cf_id, missing)
                    self.logger.log(
                        f"[{code}][Fields] {'Added' if ok else 'FAILED to add'} "
                        f"{len(missing)} new component option(s) to '{title}' (id={cf_id})",
                        "info" if ok else "warning",
                    )
                else:
                    self.logger.log(f"[{code}][Fields] Reusing per-project '{title}' CF (id={cf_id})")

            option_list = await self.pools.qs(self.qase.get_custom_field_options, cf_id) or []
            options = {o["title"].strip().lower(): o["id"] for o in option_list}
            if not options:
                self.logger.log(
                    f"[{code}][Fields] '{title}' (id={cf_id}) returned no options — "
                    f"component values will be skipped", "warning",
                )
            self.mappings.component_field[code] = {"field_id": int(cf_id), "options": options}

    async def _ensure_jira_ref_field(self):
        """Create (or reuse) a text custom field 'Jira Links' scoped to the target
        Qase projects. Each case stores the Jira issues it links to (stories, bugs,
        epics, etc. — excluding test-to-test clone/duplicate links), as key + URL.
        Qase has no native refs field. Id kept in mappings.jira_links_field_id.
        """
        title = "Jira Links"
        codes = sorted({p["code"] for p in self.mappings.projects if p.get("code")})
        try:
            existing = await self.pools.qs(self.qase.get_case_custom_fields) or []
        except Exception as e:
            self.logger.log(f"[Fields] Could not list Qase custom fields: {e}", "warning")
            existing = []
        for cf in existing:
            if (getattr(cf, "title", "") or "").strip().lower() == title.lower():
                cf_id = int(getattr(cf, "id"))
                self.mappings.jira_links_field_id = cf_id
                # CRUCIAL: a reused field must be scoped to the target project(s),
                # or Qase silently drops the value on case create. Merge target
                # codes into the field's existing scope (never shrink it).
                if not bool(getattr(cf, "is_enabled_for_all_projects", False)):
                    have = [str(c) for c in (getattr(cf, "projects_codes", None) or [])]
                    missing = [c for c in codes if c not in have]
                    if missing:
                        ok = await self.pools.qs(
                            self.qase.update_custom_field, cf_id,
                            {"title": title, "projects_codes": have + missing},
                        )
                        self.logger.log(
                            f"[Fields] {'Scoped' if ok else 'FAILED to scope'} '{title}' CF "
                            f"(id={cf_id}) to target project(s) {missing}",
                            "info" if ok else "warning",
                        )
                self.logger.log(f"[Fields] Reusing '{title}' CF (id={cf_id})")
                return
        cf_id = await self.pools.qs(self.qase.create_custom_field, {
            "title": title, "entity": 0, "type": 2,  # 0=case, 2=text
            "is_required": False, "is_visible": True, "is_filterable": True,
            "is_enabled_for_all_projects": False, "projects_codes": codes,
        })
        if cf_id:
            self.mappings.jira_links_field_id = cf_id
            self.logger.log(f"[Fields] Created '{title}' CF (id={cf_id})")
        else:
            self.logger.log("[Fields] Could not create 'Jira Reference' CF", "warning")
