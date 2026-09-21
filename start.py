import os
import sys

from src.support.config_manager import ConfigManager
from src.support.logger import Logger
from src.importer import Importer

# Usage: python start.py [config.json] [--dry-run] [--no-cache] [--clear-cache]
#   --dry-run      read everything from Zephyr Squad and report exactly what
#                  would be created, without writing anything to Qase
#   --no-cache     ignore cache.enabled for this run (always hit the source)
#   --clear-cache  delete the source cache before running. Use this between
#                  engagements and between re-migrations of the same project:
#                  a kept cache faithfully replays the OLD source data.
_FLAGS = {"--dry-run", "--no-cache", "--clear-cache"}
_args = [a for a in sys.argv[1:] if a not in _FLAGS]
_dry_run = "--dry-run" in sys.argv[1:] or bool(str(os.environ.get("QASE_DRY_RUN") or "").strip())
_no_cache = "--no-cache" in sys.argv[1:]
_clear_cache = "--clear-cache" in sys.argv[1:]

# Config file: first positional arg, or QASE_CONFIG_FILE / MIGRATION_CONFIG, else ./config.json
_config_path = (
    (_args[0] if _args else None)
    or os.environ.get("QASE_CONFIG_FILE")
    or os.environ.get("MIGRATION_CONFIG")
    or "./config.json"
)

if not os.path.exists(_config_path):
    print(f"❌ Config file not found: {_config_path}")
    print("   Copy config.example.json to config.json and fill in your tokens,")
    print("   then run `python preflight.py` to validate before migrating.")
    sys.exit(1)

config = ConfigManager(config_file=_config_path)
config.load_config()

# Fail fast on an empty/unparseable config instead of 401ing mid-run.
# Zephyr Squad Cloud uses ZAPI JWT auth (account_id + access_key + secret_key);
# Jira is required because classic Squad tests are Jira issues.
_required = [
    "qase.api_token",
    "zephyr.account_id", "zephyr.access_key", "zephyr.secret_key",
    "jira.base_url", "jira.email", "jira.api_token",
]
_missing = [key for key in _required if not str(config.get(key) or "").strip()]
if _missing:
    print(f"❌ Config is missing required key(s): {', '.join(_missing)}")
    print("   Run `python preflight.py` for a full validation report.")
    sys.exit(1)

# Cache overrides, applied after the config is loaded so a flag always wins.
if _no_cache and config.get("cache.enabled"):
    config._set_config("cache.enabled", False)
    print("ℹ️  --no-cache: source response cache disabled for this run")
if _clear_cache:
    from src.support.source_cache import SourceCache

    _cache_path = SourceCache.resolve_path(config.get("cache.dir") or "./cache")
    if _cache_path:
        _gone = SourceCache.wipe(_cache_path)
        print(f"ℹ️  --clear-cache: removed {_gone} cache file(s) at {_cache_path}")

prefix = config.get("prefix") or "zephyr-squad"

logger = Logger(config.get("debug"), prefix=prefix)

importer = Importer(config, logger, dry_run=_dry_run)
importer.start()
