from ..api.jira_lookup import JiraLookupClient
from ..api.zephyr_squad import ZephyrSquadApiClient
from ..support.config_manager import ConfigManager
from ..support.logger import Logger

_DEFAULT_BASE_URL = "https://prod-api.zephyr4jiracloud.com/connect"


class ZephyrSquadService:
    """Service wrapper around the ZAPI v1.0 client + a first-class Jira read client.

    Classic Zephyr Squad splits a "test" across two systems:
      * **Jira** owns the test issue (summary, description, priority, components,
        labels) — enumerated via ``get_test_issues``.
      * **ZAPI** owns test **steps**, **cycles**, **executions**, **step results**,
        statuses and **attachments** — via the ZephyrSquadApiClient.

    Both clients are READ-ONLY (GET only). Jira credentials are REQUIRED for a
    full migration (without them, test issues cannot be enumerated); the service
    still constructs so read-only preflight can report the gap clearly.
    """

    def __init__(self, config: ConfigManager, logger: Logger):
        self.config = config
        self.logger = logger

        account_id = str(config.get("zephyr.account_id") or "").strip()
        access_key = str(config.get("zephyr.access_key") or "").strip()
        secret_key = str(config.get("zephyr.secret_key") or "").strip()
        base_url = str(config.get("zephyr.base_url") or _DEFAULT_BASE_URL).strip()
        page_size = int(config.get("zephyr.page_size") or 100)

        self.client = ZephyrSquadApiClient(
            account_id=account_id,
            access_key=access_key,
            secret_key=secret_key,
            logger=logger,
            base_url=base_url,
            page_size=page_size,
            max_requests_per_min=float(config.get("zephyr.max_requests_per_min") or 30),
        )

        jira_base = str(config.get("jira.base_url") or "").strip()
        jira_email = str(config.get("jira.email") or "").strip()
        jira_token = str(config.get("jira.api_token") or "").strip()
        self.jira = None
        if jira_base and jira_email and jira_token:
            self.jira = JiraLookupClient(jira_base, jira_email, jira_token, logger)
            self.logger.log("[ZephyrSquad] Jira read client ENABLED")
        else:
            self.logger.log(
                "[ZephyrSquad] Jira read client DISABLED (no jira.* config) — test "
                "cases CANNOT be enumerated (classic Squad tests are Jira issues). "
                "Set jira.base_url / jira.email / jira.api_token in config.json.",
                "warning",
            )

        self.issue_type = str(config.get("cases.jira_issue_type") or "Test").strip() or "Test"
        # Optional Jira status filter, e.g. ["Testing"] — migrate only the live
        # tests. Empty/absent = every status (previous behaviour).
        _statuses = config.get("cases.jira_statuses")
        if isinstance(_statuses, str):
            _statuses = [_statuses]
        self.issue_statuses = [
            str(s).strip() for s in (_statuses or []) if str(s).strip()
        ]
        if self.issue_statuses:
            self.logger.log(
                f"[ZephyrSquad] Test-issue status filter: {self.issue_statuses} "
                f"(only these Jira statuses will be migrated)"
            )

        # Per-project caches for Jira lookups
        self._component_names: dict = {}
        self._version_names: dict = {}
        self._issue_keys: dict = {}

    # ------------------------------------------------------------------
    # Projects & versions  (ZAPI)
    # ------------------------------------------------------------------

    def get_projects(self) -> list:
        self.logger.log("[ZephyrSquad] Fetching projects (ZAPI /util/project-list)")
        return self.client.get_projects()

    # Versions come from Jira (ZAPI Cloud has no version-list endpoint) —
    # see get_version_names below.

    # ------------------------------------------------------------------
    # Cycles & executions  (ZAPI, GET-only) — used by the deferred runs step
    # ------------------------------------------------------------------

    def get_cycles(self, project_id, version_id) -> list:
        return self.client.get_cycles(project_id, version_id)

    def get_cycle(self, cycle_id, project_id, version_id) -> dict:
        return self.client.get_cycle(cycle_id, project_id, version_id)

    def get_executions_by_cycle(self, cycle_id, project_id, version_id):
        yield from self.client.get_executions_by_cycle(cycle_id, project_id, version_id)

    def get_executions_by_issue(self, issue_id, project_id) -> list:
        return self.client.get_executions_by_issue(issue_id, project_id)

    # ------------------------------------------------------------------
    # Test steps & step results  (ZAPI)  — keyed by Jira issue id
    # ------------------------------------------------------------------

    def get_test_steps(self, issue_id, project_id=None) -> list:
        return self.client.get_test_steps(issue_id, project_id)

    def get_step_results(self, execution_id, issue_id=None) -> list:
        """Per-step outcomes of an execution. ``issue_id`` is required by ZAPI —
        omitting it returns ``Missing parameter: issueId``."""
        return self.client.get_step_results(execution_id, issue_id)

    def get_execution_statuses(self) -> list:
        return self.client.get_execution_statuses()

    def get_step_execution_statuses(self) -> list:
        return self.client.get_step_execution_statuses()

    # ------------------------------------------------------------------
    # Attachments  (ZAPI, sanctioned GET read endpoints) — deferred phase
    # ------------------------------------------------------------------

    def get_step_result_attachments(self, step_result_id) -> list:
        """Files attached to ONE step during execution (one call per step result)."""
        return self.client.get_step_result_attachments(step_result_id)

    def get_test_step_attachments(self, test_step_id) -> list:
        """Files attached to a test STEP definition."""
        return self.client.get_test_step_attachments(test_step_id)

    def get_execution_attachments(self, project_id, issue_id, entity_id) -> list:
        return self.client.get_execution_attachments(project_id, issue_id, entity_id)

    def get_teststep_attachments(self, project_id, issue_id, entity_id) -> list:
        return self.client.get_teststep_attachments(project_id, issue_id, entity_id)

    def download_attachment_bytes(self, attachment_id, project_id=None, issue_id=None) -> bytes:
        return self.client.download_attachment_bytes(attachment_id, project_id, issue_id)

    # ------------------------------------------------------------------
    # Jira (first-class source: test issues + name resolution)
    # ------------------------------------------------------------------

    def get_test_issues(self, project_key: str):
        """Yield pages of Jira test issues.

        Scope comes from config: ``cases.jira_issue_type`` (default ``Test``) and
        the optional ``cases.jira_statuses`` — e.g. ``["Testing"]`` to migrate only
        live tests and skip closed/obsolete ones.
        """
        if not self.jira:
            self.logger.log(
                "[ZephyrSquad] Cannot fetch test issues — Jira client disabled", "error"
            )
            return
        # Also request any Jira custom fields the config asks us to mirror, or the
        # case builder would never see them (the default projection omits them).
        extra = [str(k).strip() for k in (self.config.get("cases.jira_custom_fields") or {})
                 if str(k).strip()]
        # cases.comments_custom_field: comments ride along on this same issue read,
        # so mirroring them costs NO extra API calls — but the default projection
        # omits them, so they have to be asked for explicitly.
        if bool(self.config.get("cases.comments_custom_field")):
            extra.append("comment")
        fields = None
        if extra:
            fields = list(self.jira._TEST_ISSUE_FIELDS) + extra
        yield from self.jira.search_test_issues(
            project_key, issue_type=self.issue_type, statuses=self.issue_statuses,
            fields=fields,
        )

    def get_issue_attachments(self, issue_id_or_key) -> list:
        if not self.jira:
            return []
        return self.jira.get_issue_attachments(issue_id_or_key)

    def download_jira_attachment_bytes(self, url: str) -> bytes:
        if not self.jira:
            return None
        return self.jira.download_attachment_bytes(url)

    def get_jira_project(self, project_key: str) -> dict:
        """Full Jira project dict ({id, key, name, ...}) — the source of the
        numeric projectId and display name (ZAPI Cloud has no project-list)."""
        if not self.jira:
            return {}
        try:
            return self.jira.get_project(project_key) or {}
        except Exception as e:
            self.logger.log(
                f"[ZephyrSquad] Jira project lookup failed for {project_key}: {e}", "warning"
            )
            return {}

    def get_jira_project_name(self, project_key: str) -> str:
        return (self.get_jira_project(project_key).get("name") or "").strip()

    def get_jira_user(self, account_id) -> dict:
        """{accountId, emailAddress, displayName} for an Atlassian account id.

        READ-ONLY; used to retain authorship by matching source users onto users
        that already exist in Qase. ``{}`` when Jira is disabled or lookup fails.
        """
        if not self.jira or not account_id:
            return {}
        try:
            return self.jira.get_user(account_id) or {}
        except Exception as e:
            self.logger.log(
                f"[ZephyrSquad] Jira user lookup failed for {account_id}: {e}", "warning"
            )
            return {}

    def get_component_names(self, project_key: str) -> dict:
        """{component_id → name} for the project; empty when Jira is disabled."""
        if not self.jira:
            return {}
        if project_key not in self._component_names:
            try:
                self._component_names[project_key] = self.jira.get_component_names(project_key)
            except Exception as e:
                self.logger.log(
                    f"[ZephyrSquad] Jira component lookup failed for {project_key}: {e}",
                    "warning",
                )
                self._component_names[project_key] = {}
        return self._component_names[project_key]

    def get_version_names(self, project_key: str) -> dict:
        if not self.jira:
            return {}
        if project_key not in self._version_names:
            try:
                self._version_names[project_key] = self.jira.get_version_names(project_key)
            except Exception as e:
                self.logger.log(
                    f"[ZephyrSquad] Jira version lookup failed for {project_key}: {e}",
                    "warning",
                )
                self._version_names[project_key] = {}
        return self._version_names[project_key]

    def get_distinct_labels(self, project_key: str) -> list:
        """Every distinct Jira label used by this project's test issues, sorted.

        Needed because the Labels custom field is a MULTISELECT: its options must
        exist before any case is created, but labels (unlike components) have no
        Jira endpoint listing them, so the issues have to be scanned. Cheap: a
        labels-only JQL projection against Jira, which is not ZAPI rate-limited.
        """
        if not self.jira:
            return []
        seen = set()
        pages = 0
        try:
            # Same scope as the cases phase (issue type + optional status filter),
            # so the option set matches exactly the tests that get migrated.
            for batch in self.jira.search_test_issues(
                project_key,
                issue_type=self.issue_type,
                fields=["labels"],
                statuses=self.issue_statuses,
            ):
                pages += 1
                for issue in batch:
                    for lab in ((issue.get("fields") or {}).get("labels") or []):
                        lab = str(lab).strip()
                        if lab:
                            seen.add(lab)
        except Exception as e:
            self.logger.log(
                f"[ZephyrSquad] Label scan failed for {project_key}: {e}"[:300], "warning"
            )
            return []
        self.logger.log(
            f"[ZephyrSquad] {project_key}: {len(seen)} distinct Jira label(s) "
            f"across {pages} page(s)"
        )
        return sorted(seen)

    def get_distinct_custom_field_values(self, project_key: str, jira_field_id: str) -> list:
        """Distinct values of a Jira CUSTOM field across this project's test issues.

        Needed because a multiselect Qase field must have its options created before
        any case is written, and Jira's allowed-values for a custom field are not
        reliably readable per project. Cheap: one projected JQL scan against Jira,
        which is not ZAPI rate-limited.

        Handles the shapes Jira returns for custom fields: a scalar, an option dict
        ({"value": ...}), or a list of either.
        """
        if not self.jira:
            return []

        def flatten(raw):
            if raw is None:
                return []
            if isinstance(raw, list):
                out = []
                for item in raw:
                    out.extend(flatten(item))
                return out
            if isinstance(raw, dict):
                for k in ("value", "name", "displayName"):
                    if raw.get(k):
                        return [str(raw[k]).strip()]
                return []
            return [str(raw).strip()]

        seen, pages = set(), 0
        try:
            for batch in self.jira.search_test_issues(
                project_key,
                issue_type=self.issue_type,
                fields=[jira_field_id],
                statuses=self.issue_statuses,
            ):
                pages += 1
                for issue in batch:
                    for v in flatten((issue.get("fields") or {}).get(jira_field_id)):
                        if v:
                            seen.add(v)
        except Exception as e:
            self.logger.log(
                f"[ZephyrSquad] Custom-field scan failed for {project_key}/"
                f"{jira_field_id}: {e}"[:300], "warning",
            )
            return []
        self.logger.log(
            f"[ZephyrSquad] {project_key}: {len(seen)} distinct value(s) for "
            f"{jira_field_id} across {pages} page(s)"
        )
        return sorted(seen)

    def get_versions(self, project_key: str) -> list:
        """Full Jira fix-version records (id/name/description/release_date/released).

        Used by the Milestones phase; ZAPI Cloud has no version endpoint, so this
        comes from Jira like the names above. Returns [] when Jira is unconfigured.
        """
        if not self.jira:
            self.logger.log(
                "[ZephyrSquad] Cannot read fix versions — Jira read client is disabled",
                "warning",
            )
            return []
        try:
            return self.jira.get_versions(project_key)
        except Exception as e:
            self.logger.log(
                f"[ZephyrSquad] Jira version lookup failed for {project_key}: {e}",
                "warning",
            )
            return []

    def resolve_issue_keys(self, issue_ids: list) -> dict:
        """Numeric Jira issue ids → keys, cached. Empty when Jira is disabled."""
        if not self.jira:
            return {}
        pending = [str(i) for i in issue_ids if str(i) not in self._issue_keys]
        if pending:
            try:
                self._issue_keys.update(self.jira.get_issue_keys(pending))
            except Exception as e:
                self.logger.log(f"[ZephyrSquad] Issue key resolution failed: {e}", "warning")
        return {str(i): self._issue_keys[str(i)]
                for i in issue_ids if str(i) in self._issue_keys}
