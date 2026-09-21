import asyncio
import re

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

    async def _step_defs(self, code: str, issue_id, project_id) -> dict:
        """``{stepId: {"order", "action", "data", "expected_result"}}`` for a test.

        Cached per issue: a test's steps are the same across all its executions,
        so this is one ZAPI call per TEST, not per execution.
        """
        cache = self._stepdef_cache
        key = str(issue_id)
        if key in cache:
            return cache[key]
        # The Cases phase already fetched these; reuse rather than re-fetch.
        # A miss just costs the call it would have cost anyway.
        raw = (self.mappings.step_defs.get(code) or {}).get(key)
        if raw is None:
            self._stepdef_misses[0] += 1
            try:
                raw = await self.pools.source(
                    self.zephyr.get_test_steps, issue_id, project_id
                ) or []
            except Exception as e:
                self.logger.log(
                    f"[{code}][Runs] step definitions unavailable for issue "
                    f"{issue_id}: {e}", "warning"
                )
                raw = []
        else:
            self._stepdef_hits[0] += 1
        defs = {}
        for st in raw:
            if not isinstance(st, dict) or st.get("id") is None:
                continue
            defs[str(st["id"])] = {
                "order": st.get("orderId") or 0,
                "action": (_strip_html(str(st.get("step") or "")).strip() or "Step")[:1000],
                "data": (_strip_html(str(st.get("data") or "")).strip() or None),
                "expected_result": (_strip_html(str(st.get("result") or "")).strip() or None),
            }
        cache[key] = defs
        return defs

    async def _step_results(self, code: str, execution_id, issue_id, project_id) -> list:
        """Per-step outcomes of one execution → ``custom_step_results``.

        Shape is what ``QaseService.prepare_result_steps`` expects:
        ``{"status_id", "actual", "position", "action", "data",
        "expected_result"}``.

        Two things make the join by ``stepId`` essential rather than cosmetic:

        1. ``stepresult/search`` returns step results in ARBITRARY order
           (observed ``[4, 7, 3, 2, 8, 1, 5, 6]`` on an 8-step test). Positioning
           them by array index therefore pins each status and comment onto the
           WRONG step — silently, and invisibly whenever every step shares a
           status. Sorting by the definition's ``orderId`` is what makes the
           result truthful.
        2. A step result carries no step text at all, so without the definition
           the run view has nothing to render but the comment.

        Step status ids are the same set as execution statuses, so
        ``mappings.result_statuses`` maps both. Never raises: a failure here
        degrades the result to status-only rather than losing it.
        """
        try:
            raw = await self.pools.source(
                self.zephyr.get_step_results, execution_id, issue_id
            ) or []
        except Exception as e:
            self.logger.log(
                f"[{code}][Runs] step results unavailable for execution "
                f"{execution_id}: {e}", "warning"
            )
            return []
        if not raw:
            return []
        defs = await self._step_defs(code, issue_id, project_id)
        rows = []
        for sr in raw:
            if not isinstance(sr, dict):
                continue
            d = defs.get(str(sr.get("stepId"))) or {}
            status = sr.get("status") or {}
            step_atts = []
            if self._want_step_atts and sr.get("id"):
                step_atts = await self._step_result_attachments(
                    code, project_id, issue_id, sr["id"]
                )
            rows.append({
                "_order": d.get("order") or 0,
                "status_id": str(status.get("id")),
                "actual": _strip_html(str(sr.get("comment") or "")).strip(),
                "action": d.get("action"),
                "data": d.get("data"),
                "expected_result": d.get("expected_result"),
                "attachments": step_atts or None,
            })
        if not defs:
            # No definitions: keep ZAPI's order rather than inventing one, and
            # say so — the positions cannot be trusted.
            self.logger.log(
                f"[{code}][Runs] no step definitions for issue {issue_id}; step "
                f"order for execution {execution_id} may not match the case",
                "warning",
            )
        else:
            rows.sort(key=lambda r: r["_order"])
        steps = []
        for i, r in enumerate(rows, 1):
            r.pop("_order", None)
            r["position"] = i
            steps.append({k: v for k, v in r.items() if v is not None})
        return steps

    async def _upload_attachments(self, code: str, found: list, project_id, issue_id,
                                  what: str) -> list:
        """Download Zephyr attachments and re-upload them to Qase → hashes.

        Never raises: a failed file is reported and skipped so the surrounding
        result still migrates.
        """
        hashes = []
        for att in found or []:
            aid, name = att.get("attachment_id"), att.get("filename") or "attachment"
            if not aid:
                continue
            try:
                raw = await self.pools.source(
                    self.zephyr.download_attachment_bytes, aid, project_id, issue_id
                )
                if not raw:
                    raise ValueError("empty download")
                up = await self.pools.qs(self.qase.upload_attachment, code, (name, raw))
                h = (up or {}).get("hash")
                if not h:
                    raise ValueError("no hash returned by Qase")
                hashes.append(h)
            except Exception as e:
                # Record EVERYTHING a later recovery pass needs. The attachment
                # object itself carries no issueId, and the download endpoint
                # requires one, so without these ids a dropped file cannot be
                # re-fetched without an expensive cycle-wide scan.
                self.logger.log(
                    f"[{code}][Runs] {what} attachment {name!r} NOT migrated: {e} "
                    f"[recover: attachmentId={aid} issueId={issue_id} "
                    f"projectId={project_id}]", "warning"
                )
                self.mappings.stats.add_issue(
                    code, "run",
                    f"{what}: attachment {name!r} was not migrated ({e}) "
                    f"[attachmentId={aid} issueId={issue_id}]", "warning",
                )
        return hashes

    async def _step_result_attachments(self, code: str, project_id, issue_id,
                                       step_result_id) -> list:
        """Files a tester attached to ONE step during execution → Qase hashes.

        Costs one ZAPI call per STEP RESULT — the endpoint takes no batch (an
        execution id is rejected, and multiple ids return only the first), so on
        a large project this dominates the runs phase. Hence its own switch,
        ``runs.step_result_attachments``.
        """
        try:
            found = await self.pools.source(
                self.zephyr.get_step_result_attachments, step_result_id
            ) or []
        except Exception as e:
            self.logger.log(
                f"[{code}][Runs] step-result attachment lookup failed for "
                f"{step_result_id}: {e}", "warning"
            )
            return []
        if not found:
            return []
        return await self._upload_attachments(
            code, found, project_id, issue_id, f"step result {step_result_id}"
        )

    async def _execution_attachments(self, code: str, project_id, issue_id, execution_id) -> list:
        """Zephyr execution attachments → Qase attachment hashes.

        Downloads each file from ZAPI and re-uploads it to Qase. Both the
        download and the search need projectId AND issueId. Never raises: a
        failed file is reported and skipped, the result still migrates.
        """
        try:
            found = await self.pools.source(
                self.zephyr.get_execution_attachments, project_id, issue_id, execution_id
            ) or []
        except Exception as e:
            self.logger.log(
                f"[{code}][Runs] attachment lookup failed for execution "
                f"{execution_id}: {e}", "warning"
            )
            return []
        hashes = []
        for att in found:
            aid, name = att.get("attachment_id"), att.get("filename") or "attachment"
            if not aid:
                continue
            try:
                raw = await self.pools.source(
                    self.zephyr.download_attachment_bytes, aid, project_id, issue_id
                )
                if not raw:
                    raise ValueError("empty download")
                up = await self.pools.qs(self.qase.upload_attachment, code, (name, raw))
                h = (up or {}).get("hash")
                if h:
                    hashes.append(h)
                else:
                    raise ValueError("no hash returned by Qase")
            except Exception as e:
                # Record the SAME recovery context the step-level path logs
                # (see _upload_attachments). Without it an execution-level loss
                # leaves only a filename, and the download endpoint REQUIRES
                # issueId — so the file cannot be re-fetched afterwards without
                # scanning the whole project for whichever execution owns it.
                # That gap cost real time on two separate migrations.
                self.logger.log(
                    f"[{code}][Runs] attachment {name!r} NOT migrated: {e} "
                    f"[recover: attachmentId={aid} issueId={issue_id} "
                    f"projectId={project_id} executionId={execution_id}]", "warning"
                )
                self.mappings.stats.add_issue(
                    code, "run",
                    f"execution {execution_id}: attachment {name!r} was not migrated ({e})",
                    "warning",
                )
        return hashes

    _JIRA_KEY = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")

    @classmethod
    def _exec_defects(cls, ex: dict) -> list:
        """Jira defect keys linked to one execution.

        Zephyr attaches defects in two places and this tenant uses the second:
        ``defects`` on the execution itself, and ``stepDefects`` for defects
        raised against an individual step. Both are read — a defect raised on a
        step is still a defect for that run. Returns ``[(key, summary)]``.
        """
        out = []
        for bucket in ("defects", "stepDefects"):
            for d in (ex.get(bucket) or []):
                if not isinstance(d, dict):
                    continue
                key = (d.get("key") or "").strip()
                # Only PROJ-123 shaped keys. Zephyr also stores references that
                # are not Jira keys (a bare numeric id was observed), which would
                # render as a dead /browse/<id> link — skip rather than publish
                # broken traceability.
                if key and cls._JIRA_KEY.match(key):
                    out.append((key, (d.get("summary") or "").strip()))
        return out

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
        # Each of these costs ONE extra ZAPI call per execution (attachments cost
        # one more per file, plus a Qase upload), so both are switchable. On a
        # project with thousands of executions that is the difference between a
        # one-hour and a three-hour run.
        _want_steps = bool(self.config.get("runs.step_results"))
        _want_atts = bool(self.config.get("zephyr.attachments.enabled"))
        # One ZAPI call per STEP RESULT — on a big project this is the single
        # largest cost in the whole migration, so it is separately switchable.
        self._want_step_atts = bool(_want_atts) and bool(
            self.config.get("runs.step_result_attachments")
        )
        # [step results, execution attachments, defect links, step attachments]
        nonlocal_steps = [0, 0, 0, 0]
        # stepId -> definition, per issue (one ZAPI call per TEST, not per execution)
        self._stepdef_cache = {}
        self._stepdef_hits = [0]
        self._stepdef_misses = [0]
        self.logger.log(
            f"[{code}][Runs] step results: {'ON' if _want_steps else 'OFF'} | "
            f"execution attachments: {'ON' if _want_atts else 'OFF'} | "
            f"step-result attachments: {'ON' if self._want_step_atts else 'OFF'}"
        )

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
                              "results": [], "ts": [], "version_id": None,
                              "defects": {}}
                    )
                    # Jira defects raised anywhere in this cycle, de-duplicated
                    # by key (the same defect commonly spans several executions).
                    for dkey, dsummary in self._exec_defects(ex):
                        cyc["defects"].setdefault(dkey, dsummary)
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
                    exec_id = ex.get("id")
                    step_results = []
                    if _want_steps and exec_id:
                        step_results = await self._step_results(
                            code, exec_id, issue_id, project_id
                        )
                        nonlocal_steps[0] += len(step_results)
                        nonlocal_steps[3] += sum(
                            len(s.get("attachments") or []) for s in step_results
                        )
                    att_hashes = []
                    if _want_atts and exec_id:
                        att_hashes = await self._execution_attachments(
                            code, project_id, issue_id, exec_id
                        )
                        nonlocal_steps[1] += len(att_hashes)
                    cyc["results"].append({
                        "test_id": str(ex.get("issueId")),
                        "status_id": str(status.get("id")),
                        "created_on": sec,
                        "elapsed": 0,
                        "comment": self._exec_comment(ex),
                        "created_by": ex.get("executedByAccountId") or ex.get("executedBy"),
                        "attachments": att_hashes,
                        "custom_step_results": step_results,
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

        self.logger.log(
            f"[{code}][Runs] {nonlocal_steps[0]} step result(s), "
            f"{nonlocal_steps[1]} execution attachment(s) and "
            f"{nonlocal_steps[3]} step-result attachment(s) collected"
        )
        self.logger.log(
            f"[{code}][Runs] step definitions: {self._stepdef_hits[0]} reused from "
            f"the Cases phase, {self._stepdef_misses[0]} re-fetched"
        )
        _cyc_with_defects = sum(1 for c in cycles.values() if c.get("defects"))
        _all_defects = {k for c in cycles.values() for k in (c.get("defects") or {})}
        self.logger.log(
            f"[{code}][Runs] Jira defects: {len(_all_defects)} distinct across "
            f"{_cyc_with_defects}/{len(cycles)} cycle(s)"
            + ("" if self.mappings.jira_run_links_field_id
               else "  — NO run 'Jira Links' field, these will NOT be written")
        )
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
            # Jira defects for this cycle → run-level "Jira Links" custom field.
            # Same shape as the case-level field: "KEY — url" per line.
            fid = self.mappings.jira_run_links_field_id
            defects = cyc.get("defects") or {}
            if fid and defects:
                base = (self.config.get("jira.base_url") or "").rstrip("/")
                lines = []
                for dkey in sorted(defects):
                    summary = defects[dkey]
                    label = f"{dkey} — {summary}" if summary else dkey
                    lines.append(f"{label} — {base}/browse/{dkey}" if base else label)
                run["custom_field"] = {str(fid): "\n".join(lines)}
                nonlocal_steps[2] += len(defects)
            # Attach the run to the milestone for its cycle's Jira fix version.
            # None for Unscheduled cycles or when the version has no milestone.
            milestone_id = (self.mappings.version_to_milestone.get(code) or {}).get(
                cyc.get("version_id")
            )
            # Qase computes a run's verdict from the cases PLANNED in it. Created
            # with an empty case list the run has nothing to judge against, so it
            # reports "passed" no matter what its results say — and adding results
            # afterwards never re-computes it. That is why, on one migrated project,
            # all 243 runs showed passed while 173 of them held failed results. Pass the cycle's case
            # ids up front so the verdict is real.
            run_case_ids = sorted({
                cases_map[r["test_id"]]
                for r in (cyc.get("results") or [])
                if isinstance(r, dict) and r.get("test_id") in cases_map
            })
            async with run_sem:
                try:
                    qase_run_id = await self.pools.qs(
                        self.qase.create_run, run, code, run_case_ids, milestone_id
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
        self._log_zapi_retries(code, "Runs")
        return self.mappings

    def _log_zapi_retries(self, code: str, phase: str) -> None:
        """Report cumulative ZAPI retries so a slow phase is attributable.

        Wall-clock alone cannot distinguish "lots of data" from "ZAPI kept
        answering 200 with a non-JSON body and we backed off". Silence here
        means the phase ran clean.
        """
        client = getattr(self.zephyr, "client", None)
        summary = client.retry_summary() if client is not None else ""
        if summary:
            self.logger.log(f"[{code}][{phase}] ZAPI retries: {summary}", "warning")
