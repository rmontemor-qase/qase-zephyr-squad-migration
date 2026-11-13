import json
import time

import requests


class ScimError(Exception):
    pass


class QaseScimClient:
    """SCIM v2 client for Qase (https://docs.qase.io/en/articles/8556234-scim).

    SCIM is the only Qase API that can create a workspace member, and it is
    Enterprise-only. Base URL is the host you sign in to, e.g.
    ``app.qase.io`` (US) or ``{your-domain}.qase.io``.
    """

    def __init__(self, base_url, token, logger=None, max_retries=3, backoff_factor=1.0, ssl=True):
        base_url = (base_url or "").strip().rstrip("/")
        for prefix in ("https://", "http://"):
            if base_url.startswith(prefix):
                base_url = base_url[len(prefix):]
        # HTTP redirects to HTTPS and some clients mishandle that, so always https.
        self.__url = f"{'https' if ssl else 'http'}://{base_url}/scim/v2/"
        # Per the docs: Accept scim+json, send plain json.
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/scim+json",
            "Content-Type": "application/json",
        }
        self.logger = logger
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

    # ---- verbs -----------------------------------------------------------
    def get(self, uri):
        return self.send_request(requests.get, uri)

    def post(self, uri, payload):
        return self.send_request(requests.post, uri, payload)

    def patch(self, uri, payload):
        return self.send_request(requests.patch, uri, payload)

    def delete(self, uri):
        return self.send_request(requests.delete, uri)

    def send_request(self, request_method, uri, payload=None):
        url = self.__url + uri
        body = json.dumps(payload) if payload is not None else None
        last = None
        for attempt in range(self.max_retries + 1):
            response = request_method(url, headers=self.headers, data=body, timeout=(30, 60))
            if response.status_code in (200, 201, 204):
                return self.process_response(response)
            last = self._describe(response)
            # 429 and 5xx are worth retrying; every other 4xx is deterministic.
            if 400 <= response.status_code < 500 and response.status_code != 429:
                raise ScimError(f"{request_method.__name__.upper()} {uri} -> {last}")
            if attempt == self.max_retries:
                break
            time.sleep(self.backoff_factor * (2 ** attempt))
        raise ScimError(f"{request_method.__name__.upper()} {uri} -> {last}")

    @staticmethod
    def _describe(response):
        """SCIM errors carry status/scimType/detail; fall back to raw text."""
        try:
            body = response.json()
            detail = body.get("detail") or body.get("scimType") or ""
            return f"HTTP {response.status_code} {detail}"[:300]
        except ValueError:
            return f"HTTP {response.status_code}: {response.text[:200]}"

    @staticmethod
    def process_response(response):
        if response.status_code == 204 or not (response.text or "").strip():
            return {}
        try:
            return response.json()
        except ValueError:
            raise ScimError("Failed to parse SCIM JSON response")

    # ---- users -----------------------------------------------------------
    def create_user(self, payload):
        return self.post("Users", payload)

    def get_users(self, limit=100, start_index=1):
        # startIndex is 1-based (RFC 7644 s3.4.2.4), not a 0-based offset.
        return self.get(f"Users?count={limit}&startIndex={start_index}")

    def find_user(self, email):
        """Exact lookup by userName, so we can check one account without listing all."""
        return self.get(f'Users?filter=userName eq "{email}"')

    def deactivate_user(self, user_id):
        """DELETE deactivates and releases the seat. It does NOT erase the account
        or its history, which is why migrated authorship survives it."""
        return self.delete(f"Users/{user_id}")

    def set_active(self, user_id, active):
        payload = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "Replace", "path": "active", "value": bool(active)}],
        }
        return self.patch(f"Users/{user_id}", payload)

    # ---- groups ----------------------------------------------------------
    def create_group(self, payload):
        return self.post("Groups", payload)

    def add_user_to_group(self, group_id, user_id):
        payload = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {"op": "Add", "path": "members", "value": [{"value": user_id, "type": "User"}]}
            ],
        }
        return self.patch(f"Groups/{group_id}", payload)
