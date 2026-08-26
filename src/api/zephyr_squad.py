import hashlib
import http.client
import threading
import time
import urllib.parse

import jwt
import requests

from ..exceptions.api import APIError

_DEFAULT_BASE_URL = "https://prod-api.zephyr4jiracloud.com/connect"
_JWT_EXPIRE_SEC = 3600  # ZAPI recommends short-lived, per-request tokens


class ZephyrSquadApiClient:
    """READ-ONLY client for the Zephyr Squad Cloud REST API (ZAPI Cloud v1.0/2.0).

    Base URL: ``https://prod-api.zephyr4jiracloud.com/connect``
    Endpoints live under ``/public/rest/api/{version}`` (mostly 1.0; a few 2.0).
    Auth: per-request Atlassian-Connect JWT (see :meth:`_generate_jwt`) sent as
    ``zapiAccessKey`` + ``Authorization: JWT <token>``. VERIFIED against the live
    tenant (serverinfo → 200).

    DATA MODEL (classic Squad, confirmed live):
      * A "test" is a **Jira issue** (issuetype "Test", id 10200 on this tenant).
        Its title/description/priority/components come from **Jira**; its **steps**
        come from ZAPI ``teststep/{issueId}``.
      * ZAPI owns cycles, executions, step results, statuses, folders, attachments.
      * There is **no** project-list or version-list endpoint — project ids and
        versions come from Jira, or are bootstrapped from ZQL search results (each
        execution carries projectId / versionId / cycleId / issueId).

    SAFETY: GET requests only. No POST/PUT/PATCH/DELETE method exists, so this
    client cannot create/modify/delete anything in the customer's Zephyr (CLAUDE.md
    Rules 1/2).
    """

    def __init__(
        self,
        account_id: str,
        access_key: str,
        secret_key: str,
        logger,
        base_url: str = _DEFAULT_BASE_URL,
        max_retries: int = 7,
        backoff_factor: float = 2.0,
        connect_timeout: float = 30.0,
        read_timeout: float = 60.0,
        page_size: int = 100,
        max_requests_per_min: float = 30.0,
    ):
        if not (account_id and access_key and secret_key):
            raise APIError(
                "Zephyr Squad requires account_id, access_key and secret_key "
                "(ZAPI Cloud JWT auth). Check the 'zephyr' block in config.json."
            )
        self.account_id = account_id
        self.access_key = access_key
        self.secret_key = secret_key
        self.logger = logger
        self.base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.request_timeout = (float(connect_timeout), float(read_timeout))
        self.page_size = int(page_size)
        # Global request pacer — ZAPI Cloud enforces a strict rate limit (~1000/hr).
        # Spacing request STARTS at a steady interval avoids the 429 thundering-herd
        # (many concurrent requests all rejected + backing off) that made runs crawl.
        self._min_interval = (60.0 / max_requests_per_min) if max_requests_per_min and max_requests_per_min > 0 else 0.0
        self._rate_lock = threading.Lock()
        self._next_slot = 0.0

        # Retry visibility. A first-attempt retry used to log NOTHING, so a long
        # stall left no trace and was indistinguishable from slow I/O. Every retry
        # is now logged and counted; call retry_summary() at the end of a phase.
        self._retry_lock = threading.Lock()
        self.retry_stats = {
            "non_json_200": 0,
            "http_429": 0,
            "http_5xx": 0,
            "transport": 0,
        }
        self.retry_wait_sec = 0.0

    def _throttle(self):
        """Block until this request's paced slot (thread-safe, minimal lock hold)."""
        if self._min_interval <= 0:
            return
        with self._rate_lock:
            now = time.monotonic()
            slot = self._next_slot if self._next_slot > now else now
            self._next_slot = slot + self._min_interval
        wait = slot - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _note_retry(self, kind: str, wait: float) -> None:
        """Count one retry and the time it cost (thread-safe)."""
        with self._retry_lock:
            self.retry_stats[kind] = self.retry_stats.get(kind, 0) + 1
            self.retry_wait_sec += float(wait or 0.0)

    def retry_summary(self) -> str:
        """One-line cumulative retry report, or '' when nothing was retried.

        Lets a phase state plainly whether it was slowed by ZAPI misbehaviour
        rather than leaving the operator to infer it from wall-clock time.
        """
        with self._retry_lock:
            stats = dict(self.retry_stats)
            waited = self.retry_wait_sec
        total = sum(stats.values())
        if not total:
            return ""
        parts = ", ".join(f"{k}={v}" for k, v in stats.items() if v)
        return f"{total} retry/retries ({parts}); ~{waited / 60.0:.1f} min spent backing off"

    # ------------------------------------------------------------------
    # JWT signing (Atlassian-Connect canonical request → qsh) — VERIFIED
    # ------------------------------------------------------------------

    @staticmethod
    def _canonical_query(params: dict) -> str:
        """Canonical, percent-encoded query string for the qsh (Atlassian Connect):
        drop ``jwt``, encode key+value (RFC 3986), sort by key, join with ``&``."""
        if not params:
            return ""
        parts = []
        for key in sorted(params):
            if key == "jwt":
                continue
            value = params[key]
            if isinstance(value, (list, tuple)):
                enc_val = ",".join(urllib.parse.quote(str(v), safe="") for v in value)
            else:
                enc_val = urllib.parse.quote(str(value), safe="")
            parts.append(f"{urllib.parse.quote(str(key), safe='')}={enc_val}")
        return "&".join(parts)

    def _generate_jwt(self, method: str, relative_path: str, params: dict) -> str:
        """Mint a per-request HS256 JWT. ``relative_path`` includes the
        ``/public/rest/api/{version}`` prefix and excludes host + ``/connect``."""
        canonical = f"{method.upper()}&{relative_path}&{self._canonical_query(params)}"
        qsh = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        now = int(time.time())
        payload = {
            "sub": self.account_id,
            "qsh": qsh,
            "iss": self.access_key,
            "iat": now,
            "exp": now + _JWT_EXPIRE_SEC,
        }
        token = jwt.encode(payload, self.secret_key, algorithm="HS256")
        return token.decode("utf-8") if isinstance(token, bytes) else token

    def _headers(self, method: str, relative_path: str, params: dict) -> dict:
        return {
            "zapiAccessKey": self.access_key,
            "Authorization": f"JWT {self._generate_jwt(method, relative_path, params)}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Low-level request helper (GET only)
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict = None, version: str = "1.0"):
        """GET ``{base}/public/rest/api/{version}/{path}`` → parsed JSON.

        ``path`` is relative to the version prefix (e.g. ``"cycles/search"`` or
        ``"teststep/10001"``). Signs a fresh JWT bound to this method/path/query.
        """
        params = params or {}
        relative_path = f"/public/rest/api/{version}/{path.lstrip('/')}"
        url = f"{self.base_url}{relative_path}"
        for attempt in range(self.max_retries + 1):
            try:
                self._throttle()
                response = requests.get(
                    url,
                    headers=self._headers("GET", relative_path, params),
                    params=params,
                    timeout=self.request_timeout,
                )
                if response.status_code == 200:
                    try:
                        return response.json()
                    except (ValueError, requests.exceptions.JSONDecodeError):
                        # ZAPI intermittently answers a 200 with a NON-JSON body
                        # under sustained concurrent load (observed heavily on
                        # large projects: ~70% of /executions calls on an 8k-case
                        # project). This is transient, so treat it like 429/5xx and
                        # retry. Raising here would hit `except APIError: raise`
                        # below and fail permanently on the first occurrence —
                        # which silently discarded one test's executions per hit.
                        if attempt == self.max_retries:
                            raise APIError(
                                f"Non-JSON response from {path!r} after "
                                f"{self.max_retries + 1} attempt(s) "
                                f"(len={len(response.content)}, "
                                f"content-type={response.headers.get('Content-Type')!r}): "
                                f"{response.text[:200]!r}"
                            )
                        _wait = min(self.backoff_factor * (2 ** attempt), 30.0)
                        self._note_retry("non_json_200", _wait)
                        if self.logger:
                            # Log EVERY attempt, first one included. Staying quiet
                            # on attempt 0 hid a 20-minute stall on a real run.
                            self.logger.log(
                                f"[ZAPI] non-JSON 200 on {path!r}; retrying in "
                                f"{_wait:.0f}s (attempt {attempt + 1}/"
                                f"{self.max_retries + 1})",
                                "warning",
                            )
                        time.sleep(_wait)
                        continue
                if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
                    raise APIError(
                        f"HTTP {response.status_code} (non-retryable) for {path!r}: "
                        f"{response.text[:300]}"
                    )
                # Cap the backoff so a 429 can't trigger a ~128s sleep (which
                # previously stalled the runs phase). Enough retries still outlast
                # a rate-limit window without huge per-sleep overshoot.
                _wait = min(self.backoff_factor * (2 ** attempt), 30.0)
                self._note_retry(
                    "http_429" if response.status_code == 429 else "http_5xx", _wait
                )
                if self.logger:
                    # Vendor-confirmed ceiling is 300 req/min, and it trips on
                    # bursts rather than volume — so log every 429 AND every 5xx
                    # retry, not just persistent ones.
                    self.logger.log(
                        f"[ZAPI] HTTP {response.status_code} on {path!r}; backing off "
                        f"{_wait:.0f}s (attempt {attempt + 1}/{self.max_retries + 1})",
                        "warning",
                    )
                time.sleep(_wait)
            except APIError:
                raise
            except (
                requests.exceptions.Timeout,
                http.client.RemoteDisconnected,
                ConnectionResetError,
                requests.exceptions.ConnectionError,
            ) as _exc:
                _wait = min(self.backoff_factor * (2 ** attempt), 30.0)
                self._note_retry("transport", _wait)
                if self.logger:
                    self.logger.log(
                        f"[ZAPI] transport error on {path!r} "
                        f"({type(_exc).__name__}); retrying in {_wait:.0f}s "
                        f"(attempt {attempt + 1}/{self.max_retries + 1})",
                        "warning",
                    )
                time.sleep(_wait)
            if attempt == self.max_retries:
                raise APIError(f"Max retries reached for {path!r}")

    def _get_optional(self, path: str, params: dict = None, version: str = "1.0"):
        """Like :meth:`_get` but returns ``None`` on any 4xx instead of raising."""
        try:
            return self._get(path, params, version)
        except APIError:
            return None

    @staticmethod
    def _as_list(data, *keys):
        """Normalise a paginated/wrapped ZAPI body to a list, trying common keys."""
        if data is None:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in keys:
                v = data.get(k)
                if isinstance(v, list):
                    return v
        return []

    # ------------------------------------------------------------------
    # Server / config / statuses  (no params)
    # ------------------------------------------------------------------

    def get_server_info(self) -> dict:
        return self._get("serverinfo") or {}

    def get_general_information(self) -> dict:
        """Returns the tenant's Zephyr test ``issueType`` ({id, name, ...})."""
        return self._get("config/generalinformation") or {}

    def get_execution_statuses(self) -> list:
        return self._as_list(self._get_optional("execution/statuses"), "values")

    def get_step_execution_statuses(self) -> list:
        return self._as_list(self._get_optional("teststep/statuses"), "values")

    # ------------------------------------------------------------------
    # NOTE: /zql/search is a POST endpoint on ZAPI Cloud, so it is intentionally
    # NOT implemented here — this client is strictly GET-only (read-only safety
    # guarantee). Project ids and versions come from Jira; executions are read via
    # get_executions_by_cycle / get_executions_by_issue (both GET).
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Cycles  (GET /cycles/search, /cycle/{id})
    # ------------------------------------------------------------------

    def get_cycles(self, project_id, version_id) -> list:
        """List test cycles for a project + version (``cycles/search``)."""
        data = self._get("cycles/search", {"projectId": project_id, "versionId": version_id})
        return self._as_list(data, "searchObjectList", "cycles", "values")

    def get_cycle(self, cycle_id, project_id, version_id) -> dict:
        return self._get(
            f"cycle/{cycle_id}", {"projectId": project_id, "versionId": version_id}
        ) or {}

    # ------------------------------------------------------------------
    # Executions  (GET /executions/search/cycle/{cycleId}, /zql/search, ...)
    # ------------------------------------------------------------------

    def get_executions_by_cycle(self, cycle_id, project_id, version_id):
        """Yield pages of executions in a cycle (offset pagination)."""
        offset = 0
        while True:
            params = {
                "projectId": project_id,
                "versionId": version_id,
                "offset": offset,
                "maxRecords": self.page_size,
            }
            data = self._get(f"executions/search/cycle/{cycle_id}", params)
            batch = self._as_list(data, "searchObjectList", "executions", "values")
            if batch:
                yield batch
            total = data.get("totalCount") if isinstance(data, dict) else None
            offset += len(batch)
            if not batch or (total is not None and offset >= int(total)):
                break

    def get_executions_by_issue(self, issue_id, project_id) -> list:
        """Return ALL executions for a test (numeric Jira issue id), across cycles.

        ``GET /executions?issueId=&projectId=&offset=&size=`` (offset pagination).
        Each item is ``{"execution": {id(uuid), cycleId, cycleName, versionId,
        status{name,id}, executedOn(ms), executedBy, orderId, ...}, ...}``.
        cycleName is inline, so run titles need no separate cycle fetch.
        (NB: /executions/search/issue/{key} returns only status defs — not this;
        and cycles/search / cycle/{id} / stepresult/search return "Invalid Json"
        on this tenant, so extraction is issue-driven via THIS endpoint.)
        """
        out = []
        offset = 0
        size = min(self.page_size, 50)  # /executions caps size at 50 (errorCode 122)
        while True:
            data = self._get(
                "executions",
                {"issueId": issue_id, "projectId": project_id, "offset": offset, "size": size},
            ) or {}
            batch = data.get("executions") or []
            out.extend(batch)
            total = data.get("totalCount")
            offset += len(batch)
            if not batch or (total is not None and offset >= int(total)):
                break
        return out

    def get_execution(self, execution_id, issue_id, project_id) -> dict:
        return self._get_optional(
            f"execution/{execution_id}", {"issueId": issue_id, "projectId": project_id}
        ) or {}

    # ------------------------------------------------------------------
    # Folders  (GET /folders)  — folders exist WITHIN a cycle in Squad Cloud
    # ------------------------------------------------------------------

    def get_folders(self, project_id, version_id, cycle_id) -> list:
        data = self._get_optional(
            "folders", {"projectId": project_id, "versionId": version_id, "cycleId": cycle_id}
        )
        return self._as_list(data, "searchObjectList", "folders", "values")

    # ------------------------------------------------------------------
    # Test steps  (GET /teststep/{issueId})  — issueId = Jira issue id
    # ------------------------------------------------------------------

    def get_test_steps(self, issue_id, project_id=None) -> list:
        params = {"projectId": project_id} if project_id is not None else None
        data = self._get_optional(f"teststep/{issue_id}", params)
        # Known shapes: {"stepBeanCollection":[...]} or {"testSteps":[...]} or list
        return self._as_list(data, "stepBeanCollection", "testSteps", "steps", "values")

    # ------------------------------------------------------------------
    # Step results  (GET /stepresult/search?executionId=)
    # ------------------------------------------------------------------

    def get_step_results(self, execution_id) -> list:
        data = self._get_optional("stepresult/search", {"executionId": execution_id})
        return self._as_list(data, "stepResults", "searchObjectList", "values")

    # ------------------------------------------------------------------
    # Attachments  (sanctioned read endpoints — no browser hack for Squad)
    # ------------------------------------------------------------------

    def get_execution_attachments(self, project_id, issue_id, entity_id) -> list:
        return self._attachments("execution", project_id, issue_id, entity_id)

    def get_teststep_attachments(self, project_id, issue_id, entity_id) -> list:
        return self._attachments("teststep", project_id, issue_id, entity_id)

    def get_stepresult_attachments(self, project_id, issue_id, entity_id) -> list:
        return self._attachments("stepresult", project_id, issue_id, entity_id)

    def _attachments(self, kind: str, project_id, issue_id, entity_id) -> list:
        """``GET /attachment/search/{kind}`` → normalised
        ``[{"filename", "attachment_id", "raw"}]``."""
        data = self._get_optional(
            f"attachment/search/{kind}",
            {"projectId": project_id, "issueId": issue_id, "entityId": entity_id},
        )
        raw_list = self._as_list(data, "data", "attachments", "values")
        out = []
        for a in raw_list:
            if not isinstance(a, dict):
                continue
            aid = a.get("id") or a.get("attachmentId") or a.get("fileId")
            name = a.get("fileName") or a.get("name") or a.get("filename") or "attachment"
            out.append({"filename": name, "attachment_id": aid, "raw": a})
        return out

    def download_attachment_bytes(self, attachment_id, project_id=None, issue_id=None) -> bytes:
        """Download an attachment binary (GET /attachment/{id}, JWT-signed)."""
        if not attachment_id:
            return None
        params = {}
        if project_id is not None:
            params["projectId"] = project_id
        if issue_id is not None:
            params["issueId"] = issue_id
        relative_path = f"/public/rest/api/1.0/attachment/{attachment_id}"
        url = f"{self.base_url}{relative_path}"
        try:
            self._throttle()
            resp = requests.get(
                url,
                headers=self._headers("GET", relative_path, params),
                params=params,
                timeout=(30.0, 120.0),
                allow_redirects=True,
            )
            if resp.status_code == 200 and resp.content:
                return resp.content
        except Exception:
            pass
        return None
