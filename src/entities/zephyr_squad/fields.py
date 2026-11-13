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
        # 3b — same field, RUN entity, for defects raised against a cycle
        await self._ensure_jira_run_field()
        # 3c — Jira issue comments mirrored onto the case (no Qase comments API)
        await self._ensure_comments_field()
        await self._ensure_changehistory_field()

        # 4 — SHARED component field ("Components"), feature-flagged
        await self._ensure_component_field()

        # 4b — SHARED "Labels" multiselect: Jira labels land here, not in tags
        await self._ensure_labels_field()

        # 4c — SHARED "Fix Versions" multiselect (milestone_id is singular)
        await self._ensure_fixversion_field()
        await self._ensure_affectedversion_field()

        # 4d — arbitrary Jira custom fields, per cases.jira_custom_fields
        await self._ensure_jira_custom_fields()

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

    def _labels_custom_field(self) -> bool:
        """features.labels_custom_field — Jira labels go to a 'Labels' custom field
        instead of Qase tags. True by default: Qase caps a case at 10 tags and
        silently drops the rest, which loses data on tests carrying many labels."""
        v = self.config.get("features.labels_custom_field")
        return True if v is None else bool(v)

    def _fixversion_custom_field(self) -> bool:
        """features.fixversion_custom_field — a 'Fix Versions' custom field holding
        ALL of a case's Jira fix versions. True by default, because a case's native
        milestone_id is singular and cases routinely carry several."""
        v = self.config.get("features.fixversion_custom_field")
        return True if v is None else bool(v)

    def _shared_option_fields(self) -> bool:
        """features.shared_option_fields — one option field across all projects.

        True (default): a single field per concept (Components/Labels/Fix
        Versions) scoped to every target project, options being the union. Enables
        cross-project filtering, at the cost of each project's picker also listing
        other projects' values.

        False: one field PER project, scoped to it alone and carrying only its own
        values. Cleaner pickers, but the same-titled fields cannot be queried
        together.

        NOTE for existing installs: with True, a pre-existing per-project field of
        the same title is found and has its scope and options EXTENDED to cover all
        target projects, which merges taxonomies. Set False to keep them separate.
        """
        v = self.config.get("features.shared_option_fields")
        return True if v is None else bool(v)

    async def _ensure_option_field(self, title: str, names_by_project: dict) -> dict:
        """Create/extend the option field(s) for a concept, per features.shared_option_fields.

        Returns ``{project_code: {"field_id": int, "options": {name_lower: id}}}``.
        """
        if self._shared_option_fields():
            names = sorted({n for v in names_by_project.values() for n in v})
            result = await self._ensure_shared_multiselect(title, names)
            return {code: result for code in names_by_project if result}
        out = {}
        for code, names in names_by_project.items():
            r = await self._ensure_shared_multiselect(title, sorted(names), only_code=code)
            if r:
                out[code] = r
        return out

    async def _ensure_shared_multiselect(self, title: str, names: list,
                                         only_code: str = None) -> dict:
        """Create or extend a multiselect option field.

        ``only_code`` set  -> PER-PROJECT: scoped to that project alone, and an
        existing field is reused only if already scoped to it (a workspace-global
        field of the same name is never hijacked).
        ``only_code`` None -> SHARED: scoped to every target project, options being
        the union. Scope and options are only ever GROWN, never shrunk.

        Returns ``{"field_id": int, "options": {name_lower: option_id}}``, or {}.
        """
        codes = ([only_code] if only_code else
                 sorted({p["code"] for p in self.mappings.projects if p.get("code")}))
        if not names or not codes:
            return {}

        try:
            existing = await self.pools.qs(self.qase.get_case_custom_fields) or []
        except Exception as e:
            self.logger.log(f"[Fields] Could not list Qase custom fields: {e}", "warning")
            existing = []

        match = None
        for cf in existing:
            if (getattr(cf, "title", "") or "").strip().lower() != title.lower():
                continue
            if only_code:
                # Per-project: only reuse a field already scoped to THIS project.
                if bool(getattr(cf, "is_enabled_for_all_projects", False)):
                    continue
                if only_code not in [str(c) for c in (getattr(cf, "projects_codes", None) or [])]:
                    continue
            match = cf
            break

        if match is None:
            cf_id = await self.pools.qs(self.qase.create_custom_field, {
                "title": title, "entity": 0,
                "type": self.mappings.qase_fields_type["multiselect"],
                "is_required": False, "is_visible": True, "is_filterable": True,
                "is_enabled_for_all_projects": False, "projects_codes": codes,
                "value": [{"title": n} for n in names],
            })
            if not cf_id:
                self.logger.log(
                    f"[Fields] Could not create shared '{title}' field — those values "
                    f"will NOT be migrated", "error",
                )
                return {}
            self.logger.log(
                f"[Fields] Created shared '{title}' CF (id={cf_id}) with {len(names)} "
                f"option(s), scoped to {codes}"
            )
        else:
            cf_id = int(getattr(match, "id"))
            current = await self.pools.qs(self.qase.get_custom_field_options, cf_id) or []
            have = {o["title"].strip().lower() for o in current}
            missing = [n for n in names if n.lower() not in have]

            # Scope must include every target project or Qase rejects the whole case
            # with "The field name is not valid." Merge, never shrink.
            is_global = bool(getattr(match, "is_enabled_for_all_projects", False))
            have_codes = [str(c) for c in (getattr(match, "projects_codes", None) or [])]
            new_codes = [c for c in codes if c not in have_codes]

            if missing or (new_codes and not is_global):
                value = [{"id": o["id"], "title": o["title"]} for o in current]
                value += [{"title": t} for t in missing]
                payload = {"title": title, "value": value}
                payload.update(self._field_flags(match))
                if not is_global:
                    payload["projects_codes"] = have_codes + new_codes
                ok = bool(await self.pools.qs(self.qase.update_custom_field, cf_id, payload))
                self.logger.log(
                    f"[Fields] {'Extended' if ok else 'FAILED to extend'} shared '{title}' "
                    f"CF (id={cf_id}): +{len(missing)} option(s), scope +{new_codes or 'none'}",
                    "info" if ok else "error",
                )
            else:
                self.logger.log(
                    f"[Fields] Reusing shared '{title}' CF (id={cf_id}) — nothing to add"
                )

        option_list = await self.pools.qs(self.qase.get_custom_field_options, cf_id) or []
        options = {o["title"].strip().lower(): o["id"] for o in option_list}
        if not options:
            self.logger.log(
                f"[Fields] '{title}' (id={cf_id}) returned no options — those values "
                f"will be skipped", "warning",
            )
        return {"field_id": int(cf_id), "options": options}

    async def _ensure_fixversion_field(self):
        """SHARED multiselect CF holding ALL of a case's Jira fix versions.

        The case also gets ``milestone_id`` set (see Cases), but that is SINGULAR
        while cases routinely carry several fix versions (observed: up to 58) —
        so the milestone alone would silently drop all but one. This field keeps
        every value; the milestone gives native Qase filtering when unambiguous.
        """
        if not self._fixversion_custom_field():
            return
        title = "Fix Versions"
        by_project = {}
        for project in self.mappings.projects:
            code, zephyr_key = project.get("code"), project.get("zephyr_key")
            if not code or not zephyr_key:
                continue
            names = {(v.get("name") or "").strip()
                     for v in (await self.pools.source(self.zephyr.get_versions, zephyr_key) or [])
                     if (v.get("name") or "").strip()}
            if names:
                by_project[code] = names
        if not by_project:
            self.logger.log(f"[Fields] No Jira fix versions — skipping '{title}' field")
            return
        self.mappings.fixversion_field.update(await self._ensure_option_field(title, by_project))

    async def _ensure_affectedversion_field(self):
        """SHARED multiselect CF holding a case's Jira "Affects Version/s".

        Different fact from Fix Versions: the release a problem was FOUND in,
        not the one it is fixed in. Jira draws both from the same project
        version list, so the options come from the same lookup and cost no
        extra call. Off unless ``features.affectedversion_custom_field`` is set,
        since most projects do not populate the field at all.
        """
        if not bool(self.config.get("features.affectedversion_custom_field")):
            return
        title = "Affected Versions"
        by_project = {}
        for project in self.mappings.projects:
            code, zephyr_key = project.get("code"), project.get("zephyr_key")
            if not code or not zephyr_key:
                continue
            names = {(v.get("name") or "").strip()
                     for v in (await self.pools.source(self.zephyr.get_versions, zephyr_key) or [])
                     if (v.get("name") or "").strip()}
            if names:
                by_project[code] = names
        if not by_project:
            self.logger.log(f"[Fields] No Jira versions — skipping '{title}' field")
            return
        self.mappings.affectedversion_field.update(
            await self._ensure_option_field(title, by_project)
        )

    async def _ensure_jira_custom_fields(self):
        """Mirror arbitrary Jira custom fields onto Qase cases.

        Driven by ``cases.jira_custom_fields``: {jira_field_id: qase_field_title},
        e.g. {"customfield_10123": "Environment"}. Options are discovered from the
        values actually present on the migrated test issues, since a Jira custom
        field's allowed values are not reliably readable per project.
        """
        mapping = self.config.get("cases.jira_custom_fields") or {}
        if not mapping:
            return
        for jira_id, qase_title in mapping.items():
            jira_id = str(jira_id).strip()
            qase_title = str(qase_title).strip()
            if not jira_id or not qase_title:
                continue
            by_project = {}
            for project in self.mappings.projects:
                code, zephyr_key = project.get("code"), project.get("zephyr_key")
                if not code or not zephyr_key:
                    continue
                vals = await self.pools.source(
                    self.zephyr.get_distinct_custom_field_values, zephyr_key, jira_id
                ) or []
                if vals:
                    by_project[code] = set(vals)
            names = {n for v in by_project.values() for n in v}
            if not names:
                self.logger.log(
                    f"[Fields] Jira field {jira_id} has no values on the test issues — "
                    f"skipping '{qase_title}'"
                )
                continue
            per_code = await self._ensure_option_field(qase_title, by_project)
            if not per_code:
                continue
            self.logger.log(
                f"[Fields] '{qase_title}' <- Jira {jira_id} ({len(names)} distinct value(s))"
            )
            for code, result in per_code.items():
                self.mappings.jira_custom_fields.setdefault(code, {})[jira_id] = result

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
        """One SHARED multiselect custom field holding each test's Jira components
        — feature flag ``features.component_custom_field``.

        Shared across every target project rather than one field per project, so
        the workspace keeps a single component taxonomy. Options are the union of
        all migrated projects' Jira component names, accumulated across runs.
        """
        if not bool(self.config.get("features.component_custom_field")):
            return
        title = str(self.config.get("features.component_field_title") or "Feature Area").strip()

        fallback = str(self.config.get("features.component_field_fallback_option") or "").strip()
        by_project = {}
        for project in self.mappings.projects:
            code, zephyr_key = project.get("code"), project.get("zephyr_key")
            if not code or not zephyr_key:
                continue
            components = await self.pools.source(self.zephyr.get_component_names, zephyr_key) or {}
            names = {(n or "").strip() for n in components.values() if (n or "").strip()}
            if not names:
                continue
            # Placeholder option for tests carrying no Jira component at all
            if fallback and fallback.lower() not in {n.lower() for n in names}:
                names.add(fallback)
            by_project[code] = names

        if not by_project:
            self.logger.log(f"[Fields] No Jira components — skipping '{title}' field", "warning")
            return
        self.mappings.component_field.update(await self._ensure_option_field(title, by_project))

    async def _ensure_comments_field(self):
        """TEXT custom field 'Comments' on CASES.

        Qase has no comments API — no such route exists (verified live: every
        candidate path 404s, and the SDK ships no comments client) — so Jira
        issue comments can only be carried across inside a custom field.
        """
        if not bool(self.config.get("cases.comments_custom_field")):
            return
        self.mappings.comments_field_id = await self._ensure_text_case_field("Comments")

    async def _ensure_changehistory_field(self):
        """TEXT custom field 'Change History' on CASES.

        Qase builds its own Change history from edits made INSIDE Qase and
        offers no way to write to it, so Jira's history can only ride across in
        a custom field, exactly as Comments does.

        Useful for audit: it is the only place recording what was edited on a
        test script and, through its status transitions, who reviewed and
        approved it. Where the dedicated Jira approver fields are left empty,
        the changelog is the ONLY source for that.

        Costs no extra request — the changelog rides along on the issue search
        via ``expand=changelog``.
        """
        if not bool(self.config.get("cases.changehistory_custom_field")):
            return
        self.mappings.changehistory_field_id = await self._ensure_text_case_field(
            "Change History"
        )

    async def _ensure_text_case_field(self, title: str):
        """Create or reuse a TEXT custom field on CASES; returns its id or None."""
        codes = sorted({p["code"] for p in self.mappings.projects if p.get("code")})
        try:
            existing = await self.pools.qs(self.qase.get_case_custom_fields) or []
        except Exception as e:
            self.logger.log(f"[Fields] Could not list Qase custom fields: {e}", "warning")
            existing = []
        for cf in existing:
            if (getattr(cf, "title", "") or "").strip().lower() == title.lower():
                cf_id = int(getattr(cf, "id"))
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
                            f"(id={cf_id}) to {missing}", "info" if ok else "warning",
                        )
                self.logger.log(f"[Fields] Reusing '{title}' CF (id={cf_id})")
                return cf_id
        cf_id = await self.pools.qs(self.qase.create_custom_field, {
            "title": title, "entity": 0, "type": 2,  # 0=case, 2=text
            "is_required": False, "is_visible": True, "is_filterable": True,
            "is_enabled_for_all_projects": False, "projects_codes": codes,
        })
        if cf_id:
            self.logger.log(f"[Fields] Created '{title}' CF (id={cf_id})")
        else:
            self.logger.log(f"[Fields] Could not create '{title}' CF", "error")
        return cf_id

    async def _ensure_jira_run_field(self):
        """Create (or reuse) a TEXT custom field 'Jira Links' scoped to RUNS.

        Holds the Jira defects raised against a Zephyr cycle's executions. Qase
        exposes ``/external-issue/attach`` for CASES only, so there is no native
        way to link a run to a Jira issue; a run-entity custom field is the
        mechanism available.

        The lookup MUST be run-entity: a field's entity is part of its identity,
        so the case 'Jira Links' field is invisible here and reusing its id would
        make Qase silently drop the value.
        """
        if not bool(self.config.get("runs.jira_links_custom_field", True)):
            self.logger.log("[Fields] runs.jira_links_custom_field OFF — skipping run 'Jira Links' CF")
            return
        title = "Jira Links"
        codes = sorted({p["code"] for p in self.mappings.projects if p.get("code")})
        try:
            existing = await self.pools.qs(self.qase.get_run_custom_fields) or []
        except Exception as e:
            self.logger.log(f"[Fields] Could not list Qase RUN custom fields: {e}", "warning")
            existing = []
        for cf in existing:
            if (getattr(cf, "title", "") or "").strip().lower() == title.lower():
                cf_id = int(getattr(cf, "id"))
                self.mappings.jira_run_links_field_id = cf_id
                if not bool(getattr(cf, "is_enabled_for_all_projects", False)):
                    have = [str(c) for c in (getattr(cf, "projects_codes", None) or [])]
                    missing = [c for c in codes if c not in have]
                    if missing:
                        ok = await self.pools.qs(
                            self.qase.update_custom_field, cf_id,
                            {"title": title, "projects_codes": have + missing},
                        )
                        self.logger.log(
                            f"[Fields] {'Scoped' if ok else 'FAILED to scope'} run '{title}' CF "
                            f"(id={cf_id}) to {missing}", "info" if ok else "warning",
                        )
                self.logger.log(f"[Fields] Reusing run '{title}' CF (id={cf_id})")
                return
        cf_id = await self.pools.qs(self.qase.create_custom_field, {
            "title": title, "entity": 1, "type": 2,  # 1=run, 2=text
            "is_required": False, "is_visible": True, "is_filterable": True,
            "is_enabled_for_all_projects": False, "projects_codes": codes,
        })
        if cf_id:
            self.mappings.jira_run_links_field_id = cf_id
            self.logger.log(f"[Fields] Created run '{title}' CF (id={cf_id})")
        else:
            self.logger.log(f"[Fields] Could not create run '{title}' CF", "error")

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
            self.logger.log(f"[Fields] Could not create '{title}' CF", "error")

    async def _ensure_labels_field(self):
        """One SHARED multiselect custom field holding each test's Jira labels.

        Replaces Qase tags entirely (tags capped at 10 and silently dropped
        values). Shared across projects so the workspace has a single label
        taxonomy. Options must exist before any case is created, and Jira has no
        endpoint listing a project's labels, so they come from a labels-only issue
        scan; the option set is the union across migrated projects.
        """
        if not self._labels_custom_field():
            return
        title = "Labels"
        by_project = {}
        for project in self.mappings.projects:
            code, zephyr_key = project.get("code"), project.get("zephyr_key")
            if not code or not zephyr_key:
                continue
            found = await self.pools.source(self.zephyr.get_distinct_labels, zephyr_key) or []
            if found:
                by_project[code] = set(found)

        if not by_project:
            self.logger.log(f"[Fields] No Jira labels found — skipping '{title}' field")
            return
        self.mappings.labels_field.update(await self._ensure_option_field(title, by_project))
