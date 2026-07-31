import asyncio

from ...service.qase import QaseService
from ...service.zephyr_squad import ZephyrSquadService
from ...support.config_manager import ConfigManager
from ...support.logger import Logger
from ...support.mappings import Mappings
from ...support.pools import Pools


class Suites:
    """Import Qase suites from Jira COMPONENTS.

    Classic Zephyr Squad has no test-case folder tree; tests are Jira issues.
    Per the mapping decision, we create one Qase suite per Jira component of the
    project and record ``mappings.suites[code][str(component_id)] = qase_suite_id``.
    Test cases without a component land at the project root (suite_id = None).
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

    def import_suites(self, project: dict) -> Mappings:
        return asyncio.run(self._import_suites_async(project))

    async def _import_suites_async(self, project: dict) -> Mappings:
        code = project["code"]
        zephyr_key = project["zephyr_key"]

        # features.single_suite: file EVERY case under one suite instead of one
        # suite per Jira component (components are retained on the case itself via
        # the per-project component custom field — see Fields).
        if bool(self.config.get("features.single_suite")):
            title = str(
                self.config.get("features.single_suite_title") or "Migrated from Zephyr"
            ).strip() or "Migrated from Zephyr"
            self.mappings.suites.setdefault(code, {})
            existing_suites = (self.mappings.existing_state.get(code) or {}).get("suites") or {}
            suite_id = existing_suites.get((title.lower(), None))
            if suite_id is None:
                try:
                    suite_id = await self.pools.qs(self.qase.create_suite, code, title, "", None)
                    self.mappings.stats.add_entity_count(code, "suites", "qase", 1)
                    self.logger.log(f"[{code}][Suites] Created single suite: {title} (id={suite_id})")
                except Exception as e:
                    self.logger.log(
                        f"[{code}][Suites] Failed to create suite {title!r}: {e} — "
                        f"cases will go to project root", "error",
                    )
                    return self.mappings
            else:
                self.logger.log(f"[{code}][Suites] Reusing existing suite: {title} (id={suite_id})")
            self.mappings.single_suite[code] = suite_id
            self.mappings.stats.add_entity_count(code, "suites", "zephyr-squad", 1)
            return self.mappings

        self.logger.log(f"[{code}][Suites] Fetching Jira components for {zephyr_key}")
        components = await self.pools.source(self.zephyr.get_component_names, zephyr_key)
        if not components:
            self.logger.log(f"[{code}][Suites] No components found — cases will go to project root")
            self.mappings.suites.setdefault(code, {})
            return self.mappings

        self.logger.log(f"[{code}][Suites] Found {len(components)} component(s)")
        self.mappings.stats.add_entity_count(code, "suites", "zephyr-squad", len(components))
        self.mappings.suites.setdefault(code, {})

        # Delta mode: reuse suites already present in the target (match by title, root parent)
        existing_suites = (self.mappings.existing_state.get(code) or {}).get("suites") or {}

        created = 0
        skipped = 0
        for comp_id, comp_name in components.items():
            title = (comp_name or "").strip() or f"Component {comp_id}"
            qase_id = existing_suites.get((title.lower(), None))
            if qase_id is not None:
                self.mappings.suites[code][str(comp_id)] = qase_id
                skipped += 1
                continue
            try:
                qase_id = await self.pools.qs(self.qase.create_suite, code, title, "", None)
                self.mappings.suites[code][str(comp_id)] = qase_id
                created += 1
                self.logger.log(f"[{code}][Suites] Created suite: {title} (id={qase_id})")
            except Exception as e:
                self.logger.log(f"[{code}][Suites] Failed to create suite {title!r}: {e}", "error")

        self.mappings.stats.add_entity_count(code, "suites", "qase", created)
        self.logger.log(
            f"[{code}][Suites] Created {created}/{len(components)} suite(s)"
            + (f" (reused {skipped} existing)" if skipped else "")
        )
        return self.mappings
