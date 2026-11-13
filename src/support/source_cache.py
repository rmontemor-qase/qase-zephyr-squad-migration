"""Opt-in, read-through cache for SOURCE (Zephyr + Jira) GET responses.

WHY IT EXISTS
Qase's write API is fast and reliable. The painful side is reading: ZAPI Cloud
paces at roughly 1000 requests/hour, and classic Squad forces a per-issue sweep
because ``executions/search/cycle`` returns totalCount=0 for folder-nested
cycles. A large project therefore costs hours of GETs, and a run killed by a
dropped connection or a rate-limit wall used to start those hours again from
zero. With the cache on, a re-run replays what it already fetched from disk and
reaches the point it died in minutes instead of hours.

WHAT IT HOLDS — READ THIS BEFORE ENABLING IT
Verbatim customer API responses: test case content, issue keys, account ids,
email addresses, internal URLs. This is raw customer data at rest.

  * It lives ONLY under ``cache/`` (gitignored) and is NEVER committed. This
    repo is PUBLIC. A committed cache is a customer data disclosure that
    ``git rm`` does not undo: the blob stays readable forever via
    ``git show <sha>:<path>``, and orphaned commits are served by hash until
    support purges them.
  * It is OFF unless config turns it on. A cache that defaults to on will
    eventually serve someone stale source data without them choosing that, so
    it is opted into per engagement, per run.
  * It serves STALE source data until cleared. Clear it between engagements AND
    between re-migrations of the same project, or the re-run faithfully
    reproduces last week's Zephyr instead of today's.
  * Delete it when an engagement closes.

WHAT REACHES IT
Successful GETs only, and that is structural rather than a rule this module
enforces: both clients' ``_get`` return exclusively on a 200 whose body parsed
as JSON. Every other outcome — 4xx fast-fail, 429/5xx retry, non-JSON 200
retry — raises instead of returning, so it never reaches a cache write.
Attachment downloads issue their own ``requests.get`` and never pass through
``_get`` at all. No write is cached, because there are no source writes.

A CACHE FAILURE MUST NEVER FAIL A MIGRATION. Every read and write here is
wrapped: on any sqlite error the caller silently falls through to the real
request. Losing the speed-up is acceptable, losing the run is not.
"""

import hashlib
import json
import os
import sqlite3
import threading

# Distinct from a cached None / [] / {} / 0, every one of which is a legitimate
# response worth serving from cache. Testing truthiness instead of identity is
# what makes an "empty" endpoint re-fetch on every run forever.
MISS = object()


class SourceCache:
    """SQLite-backed response cache keyed by request identity, not by URL.

    The URL is deliberately NOT part of the key. ZephyrSquadApiClient signs a
    fresh JWT per request bound to method, path and query, so the final URL and
    headers differ on every call; keying on either gives a 100% miss rate that
    looks exactly like a working cache. The key is the relative path, the sorted
    params and the API version, namespaced per client.
    """

    FILENAME = "source_cache.db"

    _SCHEMA = (
        "CREATE TABLE IF NOT EXISTS responses ("
        "  request_hash  TEXT PRIMARY KEY,"
        "  response_data TEXT NOT NULL,"
        "  timestamp     DATETIME DEFAULT CURRENT_TIMESTAMP"
        ")"
    )

    # ------------------------------------------------------------------
    # Path safety (Rule 0: the cache lives in cache/ and nowhere else)
    # ------------------------------------------------------------------

    @classmethod
    def resolve_path(cls, configured_dir, logger=None):
        """Absolute db path inside the repo's ``cache/``, or None if unsafe.

        Anything that resolves outside ``<repo>/cache`` is REFUSED rather than
        silently relocated: an absolute path in config, a ``..`` escape, or a
        path next to config.json would put a verbatim customer-data dump
        somewhere that is not gitignored.
        """
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        allowed_root = os.path.join(repo_root, "cache")
        target = os.path.abspath(os.path.join(repo_root, str(configured_dir or "./cache")))
        if target != allowed_root and not target.startswith(allowed_root + os.sep):
            if logger:
                logger.log(
                    f"[Cache] REFUSED: cache.dir {configured_dir!r} resolves to "
                    f"{target!r}, outside {allowed_root!r}. The cache holds raw "
                    f"customer API responses and must stay in the gitignored "
                    f"cache/ directory. Cache DISABLED for this run.",
                    "error",
                )
            return None
        return os.path.join(target, cls.FILENAME)

    @staticmethod
    def wipe(path, logger=None) -> int:
        """Delete the db and its WAL sidecars. Returns how many files went."""
        removed = 0
        for suffix in ("", "-journal", "-wal", "-shm"):
            candidate = f"{path}{suffix}"
            if os.path.exists(candidate):
                try:
                    os.remove(candidate)
                    removed += 1
                except OSError as e:
                    if logger:
                        logger.log(f"[Cache] could not remove {candidate}: {e}", "warning")
        return removed

    # ------------------------------------------------------------------

    def __init__(self, path: str, logger=None, verbose: bool = False):
        self.path = path
        self.logger = logger
        self.verbose = verbose
        # One connection per THREAD. The source pool runs 6 workers and a single
        # shared sqlite3 connection is not safe across them; a new connection
        # per call would be safe but wasteful at this call volume.
        self._local = threading.local()
        self._counters = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.stores = 0

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.execute(self._SCHEMA)
            # WAL lets the 6 reader threads proceed while one writes.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.commit()
        finally:
            conn.close()

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0)
            self._local.conn = conn
        return conn

    @staticmethod
    def key(namespace: str, path: str, params, version=None) -> str:
        identity = json.dumps(
            [
                str(namespace),
                str(version or ""),
                str(path or "").lstrip("/"),
                sorted((str(k), str(v)) for k, v in (params or {}).items()),
            ],
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def get(self, namespace: str, path: str, params=None, version=None, default=MISS,
            max_age_sec=None):
        """Cached payload, or ``default`` on a miss. Never raises.

        The caller supplies the sentinel so the API clients need not import
        this module at all when the cache is off — importing it would drag
        sqlite3 in on every run, and "disabled" is meant to be a genuine
        no-op.
        """
        try:
            # max_age_sec is the safety net against the failure a read cache
            # invites: a run served stale Zephyr answers that still reports
            # success. Even with reads enabled, an entry older than the
            # caller's window is treated as a miss and refetched.
            if max_age_sec is None:
                row = self._conn().execute(
                    "SELECT response_data FROM responses WHERE request_hash = ?",
                    (self.key(namespace, path, params, version),),
                ).fetchone()
            else:
                row = self._conn().execute(
                    "SELECT response_data FROM responses WHERE request_hash = ? "
                    "AND timestamp >= datetime('now', ?)",
                    (self.key(namespace, path, params, version),
                     f"-{int(max_age_sec)} seconds"),
                ).fetchone()
        except (sqlite3.Error, ValueError):
            return default
        if row is None:
            with self._counters:
                self.misses += 1
            if self.verbose and self.logger:
                self.logger.log(f"[Cache] miss {namespace}:{path}", "debug")
            return default
        try:
            payload = json.loads(row[0])
        except (ValueError, TypeError):
            return default
        with self._counters:
            self.hits += 1
        if self.verbose and self.logger:
            # The payload itself is NEVER logged, at any level: it is customer data.
            self.logger.log(f"[Cache] hit {namespace}:{path}", "debug")
        return payload

    def set(self, namespace: str, path: str, params, version, payload) -> None:
        """Store a successful response. Never raises."""
        try:
            blob = json.dumps(payload)
        except (TypeError, ValueError):
            return
        try:
            conn = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO responses (request_hash, response_data) VALUES (?, ?)",
                (self.key(namespace, path, params, version), blob),
            )
            conn.commit()
            with self._counters:
                self.stores += 1
        except (sqlite3.Error, ValueError):
            return

    def row_count(self) -> int:
        try:
            return int(self._conn().execute("SELECT COUNT(*) FROM responses").fetchone()[0])
        except (sqlite3.Error, TypeError, ValueError):
            return -1

    def summary(self) -> str:
        total = self.hits + self.misses
        rate = (self.hits * 100.0 / total) if total else 0.0
        return (
            f"[Cache] {self.hits} hit(s), {self.misses} miss(es) "
            f"({rate:.1f}% hit rate), {self.stores} stored, "
            f"{self.row_count()} row(s) on disk at {self.path}"
        )
