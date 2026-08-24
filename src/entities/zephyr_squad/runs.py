import asyncio

from ...service.qase import QaseService
from ...service.zephyr_squad import ZephyrSquadService
from ...support.config_manager import ConfigManager
from ...support.html import strip_html as _strip_html
from ...support.logger import Logger
from ...support.mappings import Mappings
from ...support.pools import Pools


class Runs:
    """Import Zephyr Squad executions → Qase runs + results.

    Issue-driven, GET-only: for every migrated test case (numeric Jira issue id
    in the case map) fetch its executions via ``/executions?issueId=&projectId=``
    (each carries cycleId, cycleName, status, executedOn), group them by cycle,
    and create one Qase run per cycle with a bulk result per execution.

    Result status comes from ``mappings.result_statuses`` (Zephyr exec-status id →
    Qase slug, built in the Fields step). Case-level results only; step-level
    results are deferred (stepresult/search is unavailable on the tenant).
    """

    def __init__(
        self,
        qase_service: QaseService,
        source_service: ZephyrSquadService,
        logger: Logger,
        mappings: Mappings,
        config: ConfigManager,
        project: dict,
        pools: Pools,
    ):
        self.qase = qase_service
        self.zephyr = source_service
        self.logger = logger
        self.mappings = mappings
        self.config = config
        self.project = project
        self.pools = pools

    def import_runs(self) -> Mappings:
        return asyncio.run(self._import_runs_async())

    async def _resolve_executors(self, code: str, cycles: dict):
        """Map the Atlassian account ids that executed tests onto EXISTING Qase users.

        Executions expose only ``executedByAccountId``, so display name / email come
        either from what the Cases step already learned (Jira reporter/assignee) or
        from a read-only Jira user lookup for the remainder. No user is created;
        anything unmatched falls back to ``users.default``.
        """
        _ru = self.config.get("users.resolve_from_jira")
        if _ru is not None and not bool(_ru):
            return
        account_ids = {
            str(r["created_by"]).strip()
            for cyc in cycles.values()
            for r in cyc["results"]
            if r.get("created_by")
        }
        pending = [
            a for a in account_ids
            if a not in self.mappings.user_map and a not in self.mappings.resolved_users
        ]
        if not pending:
            return
        self.logger.log(
            f"[{code}][Runs] Resolving {len(pending)} distinct executor(s) to Qase users"
        )
        # Resolve from already-known info first (free), then look the rest up in Jira.
        need_lookup = []
        for aid in pending:
            if aid in self.mappings.source_user_info:
                self.mappings.resolve_source_user(aid)
                if aid not in self.mappings.resolved_users:
                    need_lookup.append(aid)
            else:
                need_lookup.append(aid)

        semaphore = asyncio.Semaphore(6)

        async def lookup(aid):
            async with semaphore:
                try:
                    user = await self.pools.source(self.zephyr.get_jira_user, aid)
                except Exception:
                    user = None
                if user:
                    self.mappings.note_source_user(
                        aid, user.get("emailAddress"), user.get("displayName")
                    )
                    self.mappings.resolve_source_user(
                        aid, user.get("emailAddress"), user.get("displayName")
                    )

        if need_lookup:
            await asyncio.gather(*[lookup(a) for a in need_lookup])

        matched = sum(1 for a in account_ids if a in self.mappings.resolved_users)
        self.logger.log(
            f"[{code}][Runs] Executor mapping: {matched}/{len(account_ids)} matched to existing "
            f"Qase users (by email/name); {len(account_ids) - matched} → default user "
            f"(id={self.mappings.default_user})"
        )

    @staticmethod
    def _exec_comment(ex: dict) -> str:
        """Tester note typed on a Zephyr execution → plain-text result comment.

        The execution payload carries the note under ``comment`` (some tenants
        also expose ``htmlComment``); it rides along in the ``/executions`` fetch
        we already make, so no extra request. Light HTML is stripped for parity
        with step text.
        """
        raw = ex.get("comment") or ex.get("htmlComment") or ""
        if not isinstance(raw, str):
            raw = str(raw)
        return _strip_html(raw).strip()

    @staticmethod
    def _sec(ms) -> int:
        try:
            ms = int(ms)
        except (TypeError, ValueError):
            return 0
        return int(ms / 1000) if ms > 1e11 else int(ms)

    async def _import_runs_async(self) -> Mappings:
        code = self.project["code"]
        project_id = self.project.get("zephyr_project_id")
        cases_map = self.mappings.zephyr_tc_id_to_qase_case_id.get(code) or {}
        # Only numeric keys are Jira issue ids (the map also holds string keys)
        issue_ids = [k for k in cases_map if str(k).isdigit()]
        if not issue_ids:
            self.logger.log(f"[{code}][Runs] No migrated cases to attach executions to — skipping")
            return self.mappings

        total_issues = len(issue_ids)
        self.logger.log(f"[{code}][Runs] Fetching executions for {total_issues} test(s)")
        cycles = {}  # cycle_id → {"name", "results":[], "ts":[]}
        semaphore = asyncio.Semaphore(16)
        # Progress logging: the fetch is otherwise a silent black box until all
        # executions are in (runs group by cycle across issues, so nothing can be
        # created until the full fetch completes). Log every _log_every tests.
        done = 0
        _log_every = max(25, total_issues // 20)  # ~20 progress lines

        async def fetch(issue_id):
            nonlocal done
            async with semaphore:
                try:
                    execs = await self.pools.source(
                        self.zephyr.get_executions_by_issue, issue_id, project_id
                    )
                except Exception as e:
                    # Fail LOUD: this drops one test's executions entirely, so it
                    # is data loss, not a degradation. As a warning it never
                    # reached the migration report and the run still claimed
                    # success. Record it so the report lists exactly what was lost.
                    self.logger.log(
                        f"[{code}][Runs] executions fetch failed for {issue_id}: {e}", "error"
                    )
                    self.mappings.stats.add_issue(
                        code, "run",
                        f"issue {issue_id}: executions fetch FAILED — this test's "
                        f"run results were NOT migrated ({e})",
                        "error",
                    )
                    execs = None
                for item in execs or []:
                    ex = item.get("execution", item) if isinstance(item, dict) else {}
                    cid = ex.get("cycleId")
                    if cid is None:
                        continue
                    cyc = cycles.setdefault(
                        cid, {"name": ex.get("cycleName") or f"Cycle {cid}",
                              "results": [], "ts": [], "version_id": None}
                    )
                    # Jira fix version of this cycle -> Qase milestone (release
                    # traceability). -1 is Zephyr's "Unscheduled", i.e. no version.
                    if cyc.get("version_id") is None:
                        vid = ex.get("versionId")
                        if vid is not None and str(vid) != "-1":
                            cyc["version_id"] = str(vid)
                    sec = self._sec(ex.get("executedOn"))
                    if sec:
                        cyc["ts"].append(sec)
                    status = ex.get("status") or {}
                    cyc["results"].append({
                        "test_id": str(ex.get("issueId")),
                        "status_id": str(status.get("id")),
                        "created_on": sec,
                        "elapsed": 0,
                        "comment": self._exec_comment(ex),
                        "created_by": ex.get("executedByAccountId") or ex.get("executedBy"),
                        "attachments": [],
                    })
                done += 1
                if done % _log_every == 0 or done == total_issues:
                    self.logger.log(
                        f"[{code}][Runs] fetched executions for {done}/{total_issues} test(s) — "
                        f"{len(cycles)} cycle(s) so far"
                    )

        await asyncio.gather(*[fetch(i) for i in issue_ids])

        if not cycles:
            self.logger.log(f"[{code}][Runs] No executions found — nothing to migrate")
            return self.mappings

        self.logger.log(f"[{code}][Runs] {len(cycles)} cycle(s) with executions")
        self.mappings.stats.add_entity_count(code, "runs", "zephyr-squad", len(cycles))

        # Milestone coverage: how many cycles carry a Jira fix version we can map.
        # Reported up front so a mapping problem is visible before any run is
        # created, rather than being inferred from the target afterwards.
        ms_map = self.mappings.version_to_milestone.get(code) or {}
        with_version = [c for c in cycles.values() if c.get("version_id")]
        mapped = [c for c in with_version if ms_map.get(c.get("version_id"))]
        unmapped = [c for c in with_version if not ms_map.get(c.get("version_id"))]
        self.logger.log(
            f"[{code}][Runs] milestones: {len(mapped)}/{len(cycles)} cycle(s) will be "
            f"attached ({len(cycles) - len(with_version)} unscheduled, "
            f"{len(unmapped)} with a version but no milestone)"
        )
        if unmapped:
            missing = sorted({c.get("version_id") for c in unmapped})
            self.logger.log(
                f"[{code}][Runs] fix version(s) with no milestone: {missing[:10]} — "
                f"those runs lose release traceability", "warning",
            )

        # Resolve who executed each test onto existing Qase users BEFORE creating
        # runs, so result authorship is retained (executions carry only an
        # Atlassian account id).
        await self._resolve_executors(code, cycles)

        # Delta mode: skip cycles whose run title already exists in the target
        existing_run_titles = (self.mappings.existing_state.get(code) or {}).get("run_titles") or set()

        # Create runs CONCURRENTLY (each cycle: create → results → complete is a
        # sequential chain, but many cycles run in parallel; the Qase throttled
        # pool paces the actual API rate). Previously serial — the main runs-phase
        # bottleneck on projects with many cycles.
        created = 0
        complete_runs = self.config.get("runs.complete")
        complete_runs = True if complete_runs is None else bool(complete_runs)
        # Each cycle costs ~3 Qase calls (create + results + complete), so a large
        # project issues thousands of requests; too much parallelism here is what
        # triggered a wave of Qase 429s. Configurable, conservative by default.
        run_sem = asyncio.Semaphore(int(self.config.get("runs.concurrency") or 6))

        async def make_run(cyc):
            nonlocal created
            title = cyc["name"]
            if title in existing_run_titles:
                return
            ts = cyc["ts"] or [0]
            created_on, completed_on = min(ts), max(ts)
            run = {
                "name": title, "description": "",
                "created_on": created_on, "completed_on": completed_on,
                "author_id": self.mappings.default_user, "is_completed": True,
            }
            # Attach the run to the milestone for its cycle's Jira fix version.
            # None for Unscheduled cycles or when the version has no milestone.
            milestone_id = (self.mappings.version_to_milestone.get(code) or {}).get(
                cyc.get("version_id")
            )
            async with run_sem:
                try:
                    qase_run_id = await self.pools.qs(
                        self.qase.create_run, run, code, [], milestone_id
                    )
                except Exception as e:
                    self.logger.log(f"[{code}][Runs] create_run failed for {title!r}: {e}", "error")
                    return
                if not qase_run_id:
                    self.mappings.stats.add_issue(code, "run", f"{title!r}: run not created", "error")
                    return
                try:
                    await self.pools.qs(
                        self.qase.send_bulk_results,
                        {"created_on": created_on}, cyc["results"], qase_run_id, code,
                        self.mappings, cases_map, self.mappings.result_statuses,
                    )
                    if complete_runs:
                        await self.pools.qs(self.qase.complete_run, code, qase_run_id)
                    created += 1
                except Exception as e:
                    self.logger.log(f"[{code}][Runs] results/complete failed for {title!r}: {e}", "error")

        await asyncio.gather(*[make_run(cyc) for cyc in cycles.values()])

        self.mappings.stats.add_entity_count(code, "runs", "qase", created)
        self.logger.log(f"[{code}][Runs] Created {created}/{len(cycles)} run(s) in Qase")
        return self.mappings
