import asyncio
from datetime import datetime, timezone

from ...service.qase import QaseService
from ...service.zephyr_squad import ZephyrSquadService
from ...support.config_manager import ConfigManager
from ...support.logger import Logger
from ...support.mappings import Mappings
from ...support.pools import Pools


class Milestones:
    """Jira fix versions → Qase milestones.

    Zephyr cycles carry the Jira ``versionId`` they belong to, so mapping versions
    onto milestones is what preserves release traceability: each migrated run is
    attached to the milestone for its cycle's version (see the Runs phase).

    Versions come from Jira, not ZAPI (ZAPI Cloud has no version-list endpoint).
    Existing milestones are reused by title, so a re-run does not duplicate them.
    Cycles with ``versionId = -1`` (Zephyr's "Unscheduled") have no version and
    therefore get no milestone — that is faithful, not a gap.
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
        self.source = source_service
        self.logger = logger
        self.mappings = mappings
        self.config = config
        self.pools = pools

    def import_milestones(self, project: dict) -> Mappings:
        return asyncio.run(self._import_async(project))

    @staticmethod
    def _due_ts(release_date):
        """Jira releaseDate ('YYYY-MM-DD') → Unix seconds, which is what Qase's
        MilestoneCreate.due_date requires. Passing the raw string is rejected with
        a pydantic int_type validation error, so every dated version would fail."""
        if not release_date:
            return None
        try:
            dt = datetime.strptime(str(release_date).strip()[:10], "%Y-%m-%d")
            return int(dt.replace(tzinfo=timezone.utc).timestamp())
        except (ValueError, TypeError):
            return None

    async def _import_async(self, project: dict) -> Mappings:
        code = project.get("code")
        jira_key = project.get("zephyr_key") or code
        if not code:
            return self.mappings

        versions = self.source.get_versions(jira_key)
        if not versions:
            self.logger.log(
                f"[{code}][Milestones] No Jira fix versions found for {jira_key} — "
                f"runs will not be attached to milestones"
            )
            self.mappings.version_to_milestone.setdefault(code, {})
            return self.mappings

        self.logger.log(f"[{code}][Milestones] {len(versions)} Jira fix version(s) found")

        # Reuse by title so re-runs are idempotent.
        try:
            existing = await self.pools.qs(self.qase.get_milestones, code) or {}
        except Exception as e:
            self.logger.log(
                f"[{code}][Milestones] Could not list existing milestones: {e}"[:300],
                "warning",
            )
            existing = {}
        if existing:
            self.logger.log(f"[{code}][Milestones] {len(existing)} already in Qase")

        mapping = self.mappings.version_to_milestone.setdefault(code, {})
        created = reused = failed = 0

        for v in versions:
            title = v.get("name") or ""
            if not title:
                continue
            key = title.strip().lower()

            if key in existing:
                mapping[str(v["id"])] = existing[key]
                reused += 1
                continue

            # Qase due_date <- Jira releaseDate. Many versions have no date; that is
            # fine, the milestone is still created and still carries traceability.
            try:
                mid = await self.pools.qs(
                    self.qase.create_milestone,
                    code,
                    title,
                    v.get("description") or "",
                    None,
                    self._due_ts(v.get("release_date")),
                )
            except Exception as e:
                mid = None
                self.logger.log(
                    f"[{code}][Milestones] create failed for {title!r}: {e}"[:300], "error"
                )

            if mid:
                mapping[str(v["id"])] = int(mid)
                existing[key] = int(mid)
                created += 1
            else:
                failed += 1
                self.mappings.stats.add_issue(
                    code, "milestone",
                    f"{title}: milestone NOT created — runs for this fix version will "
                    f"not be linked to a milestone",
                    "error",
                )

        self.logger.log(
            f"[{code}][Milestones] {created} created, {reused} reused, {failed} failed — "
            f"{len(mapping)}/{len(versions)} version(s) mapped"
        )
        self.mappings.stats.add_entity_count(code, "milestones", "zephyr-squad", len(mapping))
        return self.mappings
