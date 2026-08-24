from .stats import Stats


class Mappings:
    def __init__(self, source: str, default_user: int = 1, user_map: dict = None):
        self.suites = {}
        self.types = {}
        self.priorities = {}
        self.result_statuses = {}
        self.case_statuses = {}
        self.custom_fields = {}
        self.milestones = {}
        self.projects = []
        self.attachments_map = {}

        # Zephyr Scale project key -> Qase project code
        self.project_map = {}

        # Zephyr Scale testcase key -> Qase case id (per project code)
        self.zephyr_tc_id_to_qase_case_id: dict = {}

        self.refs_id = None
        self.group_id = None
        # "Jira Links" CF id — fallback home for Zephyr issue links when
        # native external-issue attach is unavailable (no Jira integration)
        self.jira_links_field_id = None

        # Jira fix version -> Qase milestone, per project:
        #   {project_code: {version_id(str): milestone_id(int)}}
        # Populated by the Milestones phase, consumed when creating runs so each
        # run is attached to the release it belongs to.
        self.version_to_milestone: dict = {}

        # features.component_custom_field: per-project multiselect CF holding the
        # Jira components of each test. One field PER project (scoped to it), so
        # each project keeps its own component taxonomy:
        #   {project_code: {"field_id": int, "options": {component_name_lower: option_id}}}
        self.component_field = {}

        # features.single_suite: {project_code: qase_suite_id} — the one suite all
        # cases of that project are filed under (replaces suite-per-component).
        self.single_suite = {}

        # features.required_field_defaults, resolved PER PROJECT:
        #   {project_code: {custom_field_id(str): value(str)}}
        # Fixed values stamped on every migrated case so the target workspace's
        # REQUIRED custom fields are satisfied (Zephyr has no equivalent data).
        # Per-project because a field is only valid for the projects it is scoped
        # to — sending one that is not enabled for the target project makes Qase
        # reject the whole case ("The field name is not valid.").
        self.required_field_defaults = {}

        self.qase_fields_type = {
            "number": 0,
            "string": 1,
            "text": 2,
            "selectbox": 3,
            "checkbox": 4,
            "radio": 5,
            "multiselect": 6,
            "url": 7,
            "user": 8,
            "datetime": 9,
        }

        self.default_user = default_user
        # Optional config-provided user mapping: Atlassian account id (string)
        # → Qase user id. Populated from ``users.map``; anything unmapped
        # falls back to ``users.default``. No Jira API involved.
        self.user_map = {str(k): int(v) for k, v in (user_map or {}).items()}

        # Authorship retention (users.resolve_from_jira): index of the users that
        # ALREADY exist in the Qase workspace — no user is ever created. Source
        # (Jira/Zephyr) users are matched into it by email, else by display name;
        # anything unmatched falls back to ``default_user``.
        self.qase_user_by_email = {}   # email_lower  → qase user id
        self.qase_user_by_name = {}    # name_lower   → qase user id
        # Atlassian account id → Qase user id, once resolved (cache)
        self.resolved_users = {}
        # Atlassian account id → {"email", "name"} learned from Jira issue fields
        # (reporter/assignee), so most executors need no extra Jira lookup.
        self.source_user_info = {}
        self.user_match_stats = {"email": 0, "name": 0, "unmatched": 0}

        # Qase environment registry: {project_code: {env_title_lower: qase_env_id}}
        self.environments = {}

        # Delta mode: {project_code: state dict from get_existing_project_state}
        # Present only for projects that pre-existed this run.
        self.existing_state = {}

        self.stats = Stats(source=source)

        self.qase_priority_keys_to_id: dict = {}
        self.qase_case_status_keys_to_id: dict = {}

    def register_qase_system_fields(self, fields: list) -> None:
        """Map Qase priority / case-status option titles and slugs → numeric ids for bulk import."""
        self.qase_priority_keys_to_id.clear()
        self.qase_case_status_keys_to_id.clear()
        for f in fields or []:
            if not isinstance(f, dict):
                continue
            slug = (f.get("slug") or "").strip().lower()
            title = (f.get("title") or "").strip().lower()
            options = f.get("options")
            if not isinstance(options, list) or not options:
                continue
            target = None
            if (
                slug == "priority"
                or title == "priority"
                or ("priority" in slug and "severity" not in slug)
            ):
                target = self.qase_priority_keys_to_id
            elif slug in ("status", "case-status", "state") or (
                "status" in slug and ("case" in slug or slug.endswith("status"))
            ):
                target = self.qase_case_status_keys_to_id
            elif title in ("status", "state") or (
                "status" in title and "run" not in title and "defect" not in title
            ):
                target = self.qase_case_status_keys_to_id
            if target is None:
                continue
            for opt in options:
                if not isinstance(opt, dict):
                    continue
                oid = opt.get("id")
                if oid is None:
                    continue
                oid = int(oid)
                for key in (opt.get("slug"), opt.get("title")):
                    if isinstance(key, str) and key.strip():
                        target[key.strip().lower()] = oid
                target[str(oid)] = oid

    def get_user_id(self, id) -> int:
        """Resolve a source user (Atlassian account id) to a Qase user id.

        Priority: explicit ``users.map`` config → a user resolved against the
        existing Qase workspace members (see ``resolve_source_user``) → the
        ``users.default`` fallback. Never invents a user.
        """
        if id is not None:
            key = str(id).strip()
            mapped = self.user_map.get(key)
            if mapped is not None:
                return mapped
            resolved = self.resolved_users.get(key)
            if resolved is not None:
                return resolved
        return self.default_user

    def register_qase_users(self, authors) -> None:
        """Index the users that already exist in the Qase workspace, by email and
        by display name. Accepts SDK model objects or plain dicts."""
        for a in authors or []:
            get = (lambda k: a.get(k)) if isinstance(a, dict) else (lambda k: getattr(a, k, None))
            uid = get("id")
            if uid is None:
                continue
            uid = int(uid)
            email = (get("email") or "").strip().lower()
            if email:
                self.qase_user_by_email.setdefault(email, uid)
            for name_key in ("name", "title"):
                name = (get(name_key) or "").strip().lower()
                if name:
                    self.qase_user_by_name.setdefault(name, uid)

    def note_source_user(self, account_id, email=None, display_name=None) -> None:
        """Remember a source user's email/name (learned from Jira issue fields) so
        executions referencing only an account id can still be resolved later."""
        if not account_id:
            return
        key = str(account_id).strip()
        if not key:
            return
        info = self.source_user_info.setdefault(key, {})
        if email and not info.get("email"):
            info["email"] = str(email).strip().lower()
        if display_name and not info.get("name"):
            info["name"] = str(display_name).strip()

    def resolve_source_user(self, account_id, email=None, display_name=None):
        """Match a source (Jira) user onto an EXISTING Qase user id.

        Email first (most reliable), then display name. Returns the Qase user id,
        or None when no existing Qase user matches — callers fall back to
        ``default_user``. Result is cached per account id. No user is created.
        """
        key = str(account_id).strip() if account_id else None
        if key and key in self.resolved_users:
            return self.resolved_users[key]

        known = self.source_user_info.get(key or "", {})
        email = (email or known.get("email") or "").strip().lower()
        display_name = (display_name or known.get("name") or "").strip()

        uid = None
        how = "unmatched"
        if email:
            uid = self.qase_user_by_email.get(email)
            if uid is not None:
                how = "email"
        if uid is None and display_name:
            uid = self.qase_user_by_name.get(display_name.lower())
            if uid is not None:
                how = "name"

        self.user_match_stats[how] = self.user_match_stats.get(how, 0) + 1
        if key and uid is not None:
            self.resolved_users[key] = uid
        return uid

    def register_zephyr_testcase_qase_case_id(
        self, project_code: str, zephyr_tc_key: str, qase_case_id: int
    ) -> None:
        """Record mapping from Zephyr Scale test case key to Qase case id."""
        if not project_code or not zephyr_tc_key or qase_case_id is None:
            return
        code = str(project_code).strip()
        self.zephyr_tc_id_to_qase_case_id.setdefault(code, {})[
            str(zephyr_tc_key).strip()
        ] = int(qase_case_id)
