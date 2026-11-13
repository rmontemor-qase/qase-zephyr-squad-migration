import asyncio
import json
import os
import sys

from ..support import Logger, ConfigManager as Config


class Users:
    """Create Qase workspace members from a prepared user list, then map
    source account ids onto Qase user ids.

    Ported from qase-testrail-migration's ``src/entities/users.py``. The one
    structural difference: TestRail reads its users from the source API, while
    Zephyr Squad stores only an Atlassian account id on an execution. The list
    is therefore assembled ahead of time and read from a file here.

    Nothing is created without an explicit typed confirmation, and no customer
    system is touched: the only writes are to Qase over SCIM.
    """

    def __init__(self, qase_service, logger: Logger, mappings, config: Config, pools,
                 scim_service=None):
        self.qase = qase_service
        self.scim = scim_service
        self.logger = logger
        self.mappings = mappings
        self.config = config
        self.pools = pools
        self.map = {}          # source account id -> Qase user id
        self.active_ids = []   # Qase ids eligible for group membership
        self.source_users = []
        self.logger.divider()

    # ---- entry points ----------------------------------------------------
    def import_users(self, dry_run=False):
        return asyncio.run(self.import_users_async(dry_run=dry_run))

    async def import_users_async(self, dry_run=False):
        self.load_source_users()

        if self.scim is not None:
            await self.create_users(dry_run=dry_run)
            if self.config.get("groups.create") and not dry_run:
                await self.create_root_group()

        if not dry_run:
            await self.build_map()
        return self.mappings

    # ---- source ----------------------------------------------------------
    def load_source_users(self):
        """Read the prepared user list.

        Expected shape is ``{account_id: {name, email, active, type, projects}}``.
        ``users.projects`` narrows it to the people who touched a given set of
        projects, so one list can feed several Qase workspaces.
        """
        path = self.config.get("users.source_file")
        if not path:
            raise ValueError("users.source_file is not set; nothing to import")
        if not os.path.exists(path):
            raise FileNotFoundError(f"users.source_file not found: {path}")

        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)

        # Emails known to be no longer active, overriding the Jira flag (which
        # is unreliable for this, see _only_active).
        force_inactive = {str(e).strip().lower()
                          for e in (self.config.get("users.force_inactive") or [])}
        # Emails that get a full licence; everyone else gets users.user_type.
        # Qase accepts regular | read-only | billing.
        full_users = {str(e).strip().lower()
                      for e in (self.config.get("users.full_users") or [])}

        wanted = self.config.get("users.projects") or None
        if wanted:
            wanted = {str(p).strip().upper() for p in wanted}

        skipped_no_email = skipped_not_human = skipped_other_project = 0
        for account_id, u in raw.items():
            if (u.get("type") or "atlassian") != "atlassian":
                skipped_not_human += 1          # app/service accounts never get a seat
                continue
            email = (u.get("email") or "").strip().lower()
            if not email:
                skipped_no_email += 1
                continue
            if wanted:
                theirs = {str(p).upper() for p in (u.get("projects") or [])}
                if not theirs & wanted:
                    skipped_other_project += 1
                    continue
            self.source_users.append({
                "id": account_id,
                "email": email,
                "name": (u.get("name") or "").strip() or email,
                "is_active": bool(u.get("active", True)) and email not in force_inactive,
                # Per-user override; otherwise users.user_type applies to all.
                "user_type": ("regular" if email in full_users else u.get("user_type")),
            })

        self._enforce_domain_allowlist()

        self.logger.log(
            f"[Users] Loaded {len(self.source_users)} user(s) from {path}"
            + (f" filtered to projects {sorted(wanted)}" if wanted else "")
        )
        if skipped_not_human or skipped_no_email or skipped_other_project:
            self.logger.log(
                f"[Users] Skipped {skipped_not_human} non-human account(s), "
                f"{skipped_no_email} with no email, "
                f"{skipped_other_project} outside the requested projects"
            )

    def _enforce_domain_allowlist(self):
        """Hard stop if any loaded user is outside ``users.allowed_email_domains``.

        A sandbox config sets this to the probe domain, so pointing the runner
        at the real user list by mistake aborts before a single write instead of
        creating customer accounts that cannot be deleted.
        """
        allowed = self.config.get("users.allowed_email_domains")
        if not allowed:
            return
        allowed = {str(d).strip().lower().lstrip("@") for d in allowed}
        bad = [u for u in self.source_users
               if u["email"].rsplit("@", 1)[-1] not in allowed]
        if bad:
            sample = ", ".join(u["email"] for u in bad[:3])
            raise RuntimeError(
                f"users.allowed_email_domains is {sorted(allowed)}, but {len(bad)} of "
                f"{len(self.source_users)} loaded users are outside it (e.g. {sample}). "
                f"Refusing to write anything. Check users.source_file for this config."
            )

    # ---- creation --------------------------------------------------------
    def _only_active(self) -> bool:
        """Whether to skip users flagged inactive in the source.

        Defaults to False here, the opposite of the TestRail script. Jira's
        ``active`` flag means "no access to this Jira site", not "left the
        company", so it can be wrong for many of the accounts it calls
        deactivated. Skipping on that flag would drop live staff, and
        authorship is write-once at creation.
        """
        value = self.config.get("users.only_active")
        return False if value is None else bool(value)

    def _create_as_active(self) -> bool:
        """Whether to force every created member active, ignoring source status.

        Default False: the source status is preserved, so a user Jira flags as
        inactive is created inactive. Set true to create everyone active and
        deactivate the real leavers afterwards, which is the safer option if
        Qase's author endpoint turns out not to return inactive members, since
        authorship cannot be backfilled.
        """
        return bool(self.config.get("users.create_as_active"))

    async def create_users(self, dry_run=False):
        print("[Users] Loading existing members from Qase (SCIM)...", flush=True)
        self.logger.log("[Users] Loading existing members from Qase using SCIM")
        existing = await self.pools.qs_gen_all(self.scim.get_all_users)

        by_email = {}
        for qase_user in existing:
            name = (qase_user.get("userName") or "").strip().lower()
            if name:
                by_email.setdefault(name, qase_user.get("id"))

        to_create, skipped_inactive = [], []
        for user in self.source_users:
            existing_id = by_email.get(user["email"])
            if existing_id is not None:
                self.map[user["id"]] = existing_id
                if user["is_active"]:
                    self.active_ids.append(existing_id)
                continue
            if not user["is_active"] and self._only_active():
                skipped_inactive.append(user)
                continue
            to_create.append(user)

        self._display_summary(to_create, existing, skipped_inactive, dry_run)

        if dry_run:
            self.logger.log("[Users] Dry run: nothing was created")
            return
        if not self.config.get("users.create"):
            self.logger.log("[Users] users.create is off; no user created")
            return
        if not to_create:
            self.logger.log("[Users] Every user already exists in this workspace")
            return

        sys.stdout.flush()
        answer = input(f"\n[Users] Type 'yes' to create these {len(to_create)} user(s): ").strip().lower()
        if answer != "yes":
            self.logger.log("[Users] Cancelled; no user created")
            return

        # A POST for an existing userName is an idempotent no-op in Qase: it
        # returns 200 with the EXISTING member's id and changes nothing (measured
        # on a throwaway workspace). There is no flag distinguishing that from a
        # real creation, and when the member listing was short the returned id is
        # one we never saw, so a per-id check cannot detect it. The only honest
        # measure is the workspace's own membership count before and after.
        before_count = len(existing)
        attempted = len(to_create)
        failed = 0
        for user in to_create:
            try:
                create_active = user["is_active"] or self._create_as_active()
                user_id = await self.pools.qs_task(
                    self.scim.create_user, user["email"],
                    *self._split_name(user["name"]),
                    user.get("user_type") or self.config.get("users.user_type"),
                    create_active,
                )
                self.map[user["id"]] = user_id
                self.active_ids.append(user_id)
                self.logger.log(f"[Users] {user['email']} -> {user_id}")
            except Exception as e:
                failed += 1
                self.logger.log(f"[Users] FAILED to create {user['email']}: {e}", "error")

        after = await self.pools.qs_gen_all(self.scim.get_all_users)
        created = len(after) - before_count
        adopted = attempted - failed - created
        self.logger.log(
            f"[Users] Attempted {attempted}: created {created}, already existed {adopted}, "
            f"failed {failed}. Workspace membership {before_count} -> {len(after)}")
        if adopted > 0:
            self.logger.log(
                f"[Users] {adopted} of the {attempted} already existed and were NOT created. "
                f"Qase returns 200 with the existing id for a duplicate userName. On a first "
                f"run this means the member listing was short, so check pagination.", "warning")
        if failed:
            self.logger.log("[Users] Re-run to retry the failures; existing users are skipped", "warning")

    @staticmethod
    def _split_name(full_name):
        parts = (full_name or "").split()
        if len(parts) >= 2:
            return parts[0], " ".join(parts[1:])
        return (full_name or ""), ""

    def _display_summary(self, to_create, existing, skipped_inactive, dry_run):
        def out(msg):
            print(msg, flush=True)
            self.logger.log(msg.strip())

        out("")
        out("=" * 78)
        out("USER IMPORT SUMMARY" + ("  (DRY RUN, nothing will be written)" if dry_run else ""))
        out("=" * 78)
        out(f"Qase workspace      : {self.config.get('qase.scim_host')}")
        out(f"Source list         : {self.config.get('users.source_file')}")
        projects = self.config.get("users.projects")
        if projects:
            out(f"Filtered to projects: {', '.join(sorted(projects))}")
        out("")
        out(f"In the source list                 : {len(self.source_users)}")
        out(f"Already members of this workspace  : {len(self.source_users) - len(to_create) - len(skipped_inactive)}")
        out(f"Existing members in the workspace  : {len(existing)}")
        out(f"WOULD BE CREATED                   : {len(to_create)}")
        if skipped_inactive:
            out(f"Skipped as inactive                : {len(skipped_inactive)}")
        out("-" * 78)
        for i, u in enumerate(sorted(to_create, key=lambda x: x["email"]), 1):
            flag = "" if u["is_active"] else "   [inactive in Jira]"
            out(f"  {i:>3}. {u['name'][:34]:<34} {u['email']}{flag}")
        out("=" * 78)

    # ---- mapping ---------------------------------------------------------
    async def build_map(self):
        """Map every source account id onto a Qase user id.

        Order: an explicit ``users.map`` override, then an email match against
        the workspace, then ``users.default``.
        """
        self.logger.log("[Users] Building users map")
        qase_users = await self.pools.qs_gen_all(self.qase.get_all_users)

        by_email = {}
        for qase_user in qase_users:
            d = qase_user.to_dict() if hasattr(qase_user, "to_dict") else qase_user
            email = str(d.get("email") or "").strip().lower()
            if email:
                by_email.setdefault(email, d.get("id"))

        overrides = {str(k).lower(): v for k, v in (self.config.get("users.map") or {}).items()}

        matched = defaulted = 0
        for user in self.source_users:
            email = user["email"]
            key = user["id"]
            if email in overrides or key in overrides:
                target = overrides.get(email, overrides.get(key))
                resolved = by_email.get(str(target).lower()) if "@" in str(target) else target
                if resolved is None:
                    self.logger.log(
                        f"[Users] users.map sends {email} to {target}, which is not a Qase "
                        f"user. Using the default user instead.", "warning")
                    resolved = self.mappings.default_user
                    defaulted += 1
                else:
                    matched += 1
                self.mappings.resolved_users[key] = resolved
            elif email in by_email:
                self.mappings.resolved_users[key] = by_email[email]
                matched += 1
            else:
                self.mappings.resolved_users[key] = self.mappings.default_user
                defaulted += 1
                self.logger.log(f"[Users] {email} not found in Qase, using the default user", "warning")

        self.logger.log(
            f"[Users] Mapped {matched} of {len(self.source_users)}; "
            f"{defaulted} fell back to the default user"
        )
        if defaulted:
            self.logger.log(
                f"[Users] {defaulted} user(s) will show as the migration account. Authorship is "
                f"write-once at creation, so fix this BEFORE migrating projects.", "warning")

    async def create_root_group(self):
        group_name = self.config.get("groups.name") or "Zephyr Migration"
        self.logger.log(f"[Users] Creating group {group_name}")
        group_id = await self.pools.qs_task(self.scim.create_group, group_name)
        for user_id in self.active_ids:
            await self.pools.qs_task(self.scim.add_user_to_group, group_id, user_id)
        self.logger.log(f"[Users] Added {len(self.active_ids)} member(s) to {group_name}")
