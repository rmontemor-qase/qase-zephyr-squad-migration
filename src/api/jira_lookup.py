import http.client
import time

import requests

from ..exceptions.api import APIError


class JiraLookupClient:
    """READ-ONLY Jira REST client.

    In the classic Zephyr Squad model a "test" IS a Jira issue (issuetype =
    Test), so Jira is a **first-class source** here — not just optional name
    enrichment as in the Scale/Essential importers. This client:

      * enumerates test issues via a read-only JQL search (``search_test_issues``),
        returning their fields (summary, description, priority, components,
        labels, and the numeric issue id used for the ZAPI ``/teststep`` lookup);
      * resolves project / component / version display names;
      * resolves numeric issue ids → keys for linking.

    SAFETY: GET-only. No method issues a POST/PUT/PATCH/DELETE, so it cannot
    modify anything in the customer's Jira (project CLAUDE.md, Rule 1/2).
    """

    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        logger,
        max_retries: int = 3,
        backoff_factor: float = 2.0,
        api_version: str = "2",
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.auth = (email, api_token)
        self.logger = logger
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        # api/2 returns description as a wiki-markup string (simpler to migrate);
        # api/3 returns it as an ADF document. Default to 2 for descriptions.
        self.api_version = str(api_version or "2")

    def _get(self, path: str, params: dict = None):
        url = f"{self.base_url}/{path.lstrip('/')}"
        for attempt in range(self.max_retries + 1):
            try:
                response = requests.get(
                    url,
                    auth=self.auth,
                    params=params or {},
                    headers={"Accept": "application/json"},
                    timeout=(30.0, 60.0),
                )
                if 200 <= response.status_code < 300:
                    try:
                        return response.json()
                    except (ValueError, requests.exceptions.JSONDecodeError) as e:
                        raise APIError(f"Non-JSON response from Jira {path!r}") from e
                # Fast-fail on deterministic client errors (any 4xx except 408/429)
                if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
                    raise APIError(
                        f"HTTP {response.status_code} (non-retryable) for Jira {path!r}: "
                        f"{response.text[:300]}"
                    )
                time.sleep(self.backoff_factor * (2 ** attempt))
            except APIError:
                raise
            except (
                requests.exceptions.Timeout,
                http.client.RemoteDisconnected,
                ConnectionResetError,
                requests.exceptions.ConnectionError,
            ):
                time.sleep(self.backoff_factor * (2 ** attempt))
            if attempt == self.max_retries:
                raise APIError(f"Max retries reached for Jira {path!r}")

    def get_myself(self) -> dict:
        return self._get(f"rest/api/{self.api_version}/myself")

    def get_project(self, project_key: str) -> dict:
        return self._get(f"rest/api/{self.api_version}/project/{project_key}")

    def get_component_names(self, project_key: str) -> dict:
        """{component_id (str) → name}"""
        components = self._get(f"rest/api/{self.api_version}/project/{project_key}/components") or []
        return {
            str(c["id"]): (c.get("name") or "").strip()
            for c in components
            if c.get("id") is not None
        }

    def get_version_names(self, project_key: str) -> dict:
        """{version_id (str) → name}"""
        versions = self._get(f"rest/api/{self.api_version}/project/{project_key}/versions") or []
        return {
            str(v["id"]): (v.get("name") or "").strip()
            for v in versions
            if v.get("id") is not None
        }

    def get_versions(self, project_key: str) -> list:
        """Full fix-version records, for Jira version → Qase milestone mapping.

        Returns ``[{"id", "name", "description", "release_date", "released"}]``.
        ``release_date`` is Jira's ``releaseDate`` (YYYY-MM-DD) or None, and maps
        onto the Qase milestone due date.
        """
        versions = self._get(f"rest/api/{self.api_version}/project/{project_key}/versions") or []
        out = []
        for v in versions:
            if v.get("id") is None:
                continue
            out.append({
                "id": str(v["id"]),
                "name": (v.get("name") or "").strip(),
                "description": (v.get("description") or "").strip(),
                "release_date": v.get("releaseDate") or None,
                "released": bool(v.get("released")),
            })
        return out

    # ------------------------------------------------------------------
    # Test-issue enumeration (classic Squad tests = Jira issues)
    # ------------------------------------------------------------------

    _TEST_ISSUE_FIELDS = [
        "summary",
        "description",
        "priority",
        "components",
        "labels",
        "issuetype",
        "status",
        "assignee",
        "reporter",
        "issuelinks",  # test → story/bug/etc. traceability (excl. test-to-test)
        "fixVersions",  # -> case milestone_id + the "Fix Versions" custom field
    ]

    @staticmethod
    def _jql_in_clause(field: str, values) -> str:
        """``AND <field> IN ("a", "b")`` for a str or list; ``""`` when empty.

        Values are quoted, and any embedded double quote is dropped rather than
        escaped — these come from config, and a broken JQL string would silently
        change WHICH issues migrate.
        """
        if not values:
            return ""
        if isinstance(values, str):
            values = [values]
        cleaned = [str(v).replace('"', "").strip() for v in values]
        cleaned = [v for v in cleaned if v]
        if not cleaned:
            return ""
        joined = ", ".join(f'"{v}"' for v in cleaned)
        return f" AND {field} IN ({joined})"

    def search_test_issues(
        self,
        project_key: str,
        issue_type: str = "Test",
        fields: list = None,
        page_size: int = 100,
        statuses=None,
    ):
        """Yield pages of test issues for a project via the enhanced JQL search.

        Uses ``GET /rest/api/{v}/search/jql`` (token-paginated: ``nextPageToken``
        + ``isLast`` — the classic ``/search`` startAt endpoint was removed on
        Jira Cloud in 2025). Read-only.

        JQL: ``project = "<KEY>" AND issuetype = "<issue_type>"`` plus, when
        ``statuses`` is given, ``AND status IN (...)`` — used to migrate only the
        live tests and skip closed/obsolete ones. NOTE: JQL matches a status by its
        canonical (English) name even when the API renders localized display names.
        Each yielded item is a raw Jira issue dict (``{id, key, fields: {...}}``).
        """
        field_list = fields or self._TEST_ISSUE_FIELDS
        jql = (
            f'project = "{project_key}" AND issuetype = "{issue_type}"'
            f"{self._jql_in_clause('status', statuses)} ORDER BY key ASC"
        )
        self.logger.log(f"[Jira] Test-issue JQL: {jql}")
        next_token = None
        while True:
            params = {
                "jql": jql,
                "fields": ",".join(field_list),
                "maxResults": page_size,
            }
            if next_token:
                params["nextPageToken"] = next_token
            data = self._get(f"rest/api/{self.api_version}/search/jql", params) or {}
            issues = data.get("issues") or []
            if issues:
                yield issues
            next_token = data.get("nextPageToken")
            if data.get("isLast") or not next_token or not issues:
                break

    def get_user(self, account_id) -> dict:
        """Look up a Jira user by Atlassian account id (READ-ONLY).

        Returns ``{"accountId", "emailAddress", "displayName"}`` — ``emailAddress``
        is often ``None`` on Jira Cloud (profile-privacy), so callers must be able
        to fall back to ``displayName``. Used to retain authorship by matching
        source users onto users that already exist in Qase. ``{}`` on failure.
        """
        if not account_id:
            return {}
        try:
            data = self._get(
                f"rest/api/{self.api_version}/user", {"accountId": str(account_id)}
            ) or {}
        except APIError:
            return {}
        return {
            "accountId": data.get("accountId") or str(account_id),
            "emailAddress": data.get("emailAddress"),
            "displayName": data.get("displayName"),
        }

    def get_issue_attachments(self, issue_id_or_key) -> list:
        """Return Jira-native attachments on an issue: ``[{filename, url, ...}]``.

        These are genuine Jira issue attachments (downloadable with the Jira
        token), distinct from Zephyr-internal step/execution attachments.
        """
        data = self._get(
            f"rest/api/{self.api_version}/issue/{issue_id_or_key}",
            {"fields": "attachment"},
        ) or {}
        atts = ((data.get("fields") or {}).get("attachment")) or []
        out = []
        for a in atts:
            out.append({
                "filename": a.get("filename") or "attachment",
                "url": a.get("content"),
                "mime": a.get("mimeType"),
                "size": a.get("size"),
            })
        return out

    def download_attachment_bytes(self, url: str) -> bytes:
        """Download a Jira attachment binary (GET, Basic auth). None on failure."""
        if not url:
            return None
        try:
            resp = requests.get(url, auth=self.auth, timeout=(30.0, 120.0), allow_redirects=True)
            if resp.status_code == 200 and resp.content:
                return resp.content
        except Exception:
            pass
        return None

    def get_issue_keys(self, issue_ids: list) -> dict:
        """Resolve numeric Jira issue ids → issue keys: ``{"11228": "ZEM-1"}``.

        Read-only, batched JQL search (``id IN (...)``, 100 ids per call,
        key field only). Ids the token cannot see are simply absent from the
        result — callers must handle missing entries.
        """
        ids = [str(i) for i in issue_ids if str(i).strip().isdigit()]
        resolved: dict = {}
        for start in range(0, len(ids), 100):
            chunk = ids[start:start + 100]
            try:
                data = self._get(
                    "rest/api/3/search/jql",
                    # fields=key is required: without it /search/jql returns
                    # bare {id} objects with no key (verified live)
                    {"jql": f"id IN ({','.join(chunk)})", "fields": "key", "maxResults": 100},
                ) or {}
                for issue in data.get("issues", []):
                    iid, key = issue.get("id"), issue.get("key")
                    if iid and key:
                        resolved[str(iid)] = key
            except APIError:
                # `id IN (...)` 400s if ANY id is deleted or not visible —
                # fall back to per-issue lookups so one dead link doesn't
                # sink the whole chunk.
                for iid in chunk:
                    try:
                        issue = self._get(f"rest/api/3/issue/{iid}", {"fields": "key"}) or {}
                        if issue.get("key"):
                            resolved[iid] = issue["key"]
                    except APIError:
                        continue
        return resolved
