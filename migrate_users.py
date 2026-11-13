"""Create Qase workspace members from a prepared user list, over SCIM.

Runs on its own, ahead of any project migration, because Qase writes authorship
at the moment a record is created and it cannot be backfilled: every person has
to exist in the target workspace BEFORE their project runs.

Usage:
    python migrate_users.py config.json --dry-run   # show, write nothing
    python migrate_users.py config.json             # ask, then create

Touches Qase only. Nothing here reads or writes Jira or Zephyr.
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from src.support.config_manager import ConfigManager
from src.support.logger import Logger
from src.support.mappings import Mappings
from src.support.pools import Pools
from src.service.qase import QaseService
from src.service.qase_scim import QaseScimService
from src.entities.users import Users

_DRY = "--dry-run" in sys.argv[1:]
_args = [a for a in sys.argv[1:] if not a.startswith("--")]
_config_path = _args[0] if _args else os.environ.get("QASE_CONFIG_FILE") or "./config.json"

if not os.path.exists(_config_path):
    print(f"Config file not found: {_config_path}")
    sys.exit(1)

config = ConfigManager(config_file=_config_path)
config.load_config()

for required in ("qase.scim_host", "qase.scim_token", "users.source_file"):
    if not config.get(required):
        print(f"Missing required config key: {required}")
        sys.exit(1)

logger = Logger(debug=bool(config.get("debug")), prefix="users")
logger.log(f"[Users] Config {_config_path}{' (DRY RUN)' if _DRY else ''}")

qase = QaseService(config, logger)
scim = QaseScimService(config, logger)
mappings = Mappings(
    "zephyr_squad",
    int(config.get("users.default") or 1),
    config.get("users.map") or {},
)

pools = Pools(
    qase_pool=ThreadPoolExecutor(max_workers=int(config.get("qase.concurrency") or 4)),
    source_pool=ThreadPoolExecutor(max_workers=1),
)

try:
    Users(qase, logger, mappings, config, pools, scim_service=scim).import_users(dry_run=_DRY)
finally:
    pools.qase_pool.shutdown(wait=True)
    pools.source_pool.shutdown(wait=True)
    logger.log("[Users] Thread pools shut down")
