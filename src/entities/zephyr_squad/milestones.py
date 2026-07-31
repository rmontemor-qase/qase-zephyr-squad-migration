from ...service.qase import QaseService
from ...service.zephyr_squad import ZephyrSquadService
from ...support.config_manager import ConfigManager
from ...support.logger import Logger
from ...support.mappings import Mappings
from ...support.pools import Pools


class Milestones:
    """DEFERRED — milestones (Jira versions → Qase milestones) are out of scope
    for the current phase. Intentional no-op stub.
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
        self.logger = logger
        self.mappings = mappings

    def import_milestones(self, project: dict) -> Mappings:
        self.logger.log(f"[{project.get('code')}][Milestones] Skipped — deferred in this phase")
        return self.mappings
