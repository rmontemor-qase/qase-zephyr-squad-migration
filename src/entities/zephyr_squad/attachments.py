from ...service.qase import QaseService
from ...service.zephyr_squad import ZephyrSquadService
from ...support.config_manager import ConfigManager
from ...support.logger import Logger
from ...support.mappings import Mappings
from ...support.pools import Pools


class Attachments:
    """DEFERRED — attachments are out of scope for the current phase
    (zephyr.attachments.enabled defaults to false). Intentional no-op stub;
    Squad exposes sanctioned GET attachment endpoints, to be wired in later.
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

    def import_all_attachments(self) -> Mappings:
        self.logger.log("[Attachments] Skipped — attachments deferred in this phase")
        return self.mappings
