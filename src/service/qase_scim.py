from ..api.qase_scim import QaseScimClient, ScimError

# Qase SCIM accepts these for userType. Omitting it creates a `regular` user.
# `read-only` shows in the Qase UI as "Collaborator"; the literal string
# "collaborator" is rejected.
USER_TYPES = ("regular", "read-only", "billing")

_NAME_MAX = 60   # givenName / familyName are capped at 60 characters


class QaseScimService:
    """Create and list Qase workspace members over SCIM.

    Creating a member is a write to Qase and consumes a seat, so every caller
    is expected to have confirmed first.
    """

    def __init__(self, config, logger):
        self.config = config
        self.logger = logger
        self.client = QaseScimClient(
            base_url=config.get("qase.scim_host"),
            token=config.get("qase.scim_token"),
            logger=logger,
            ssl=True if config.get("qase.ssl") is None else bool(config.get("qase.ssl")),
        )

    def create_user(self, email, first_name, last_name, user_type=None, is_active=True):
        payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": email,
            "name": {
                "givenName": (first_name or "")[:_NAME_MAX],
                "familyName": (last_name or "")[:_NAME_MAX],
            },
            "active": bool(is_active),
        }
        if user_type:
            if user_type not in USER_TYPES:
                raise ValueError(f"users.user_type must be one of {USER_TYPES}, got {user_type!r}")
            payload["userType"] = user_type
        return self.client.create_user(payload)["id"]

    def get_all_users(self, limit=100):
        """Yield pages of existing members. startIndex is 1-based per the spec."""
        start = 1
        while True:
            users = (self.client.get_users(limit, start) or {}).get("Resources") or []
            yield users
            if len(users) < limit:
                return
            start += limit

    def find_user(self, email):
        return ((self.client.find_user(email) or {}).get("Resources") or [None])[0]

    def deactivate_user(self, user_id):
        """Releases the seat and keeps the account and its history."""
        self.client.deactivate_user(user_id)

    def create_group_with_members(self, prefix, key, member_ids):
        """Create a group named ``<prefix><key>`` holding ``member_ids``.

        Used for per-project access: Qase scopes a project to a group, never to
        a user list, and only at creation time.
        """
        payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
            "displayName": f"{prefix}{key}",
            "members": [{"value": str(i), "type": "User"} for i in member_ids],
        }
        return self.client.create_group(payload)["id"]

    def create_group(self, group_name):
        payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
            "displayName": group_name,
        }
        return self.client.create_group(payload)["id"]

    def add_user_to_group(self, group_id, user_id):
        self.client.add_user_to_group(group_id, user_id)
