import asyncio
import re

from qaseio.models import TestCasebulkCasesInner

from ...service.qase import QaseService
from ...service.zephyr_squad import ZephyrSquadService
from ...support.config_manager import ConfigManager
from ...support.html import strip_html as _strip_html
from ...support.logger import Logger


def _one_line(value, cap: int = 120) -> str:
    """One-line, capped rendering of a changelog value ("empty" when blank)."""
    text = " ".join(str(value or "").split())
    if not text:
        return "empty"
    return text[:cap] + ("…" if len(text) > cap else "")
from ...support.mappings import Mappings
from ...support.pools import Pools
from datetime import datetime, timezone

# Jira priority name → Qase priority slug (Qase options: high / medium / low)
_PRIORITY_MAP = {
    "highest": "high", "urgent": "high", "high": "high",
    "medium": "medium", "normal": "medium",
    "low": "low", "lowest": "low",
}


class Cases:
    """Import Zephyr Squad test cases → Qase.

    Classic Squad model: a test IS a Jira issue (issuetype "Test"). Metadata
    (summary/description/priority/components/labels) comes from Jira; STEPS come
    from ZAPI ``teststep/{issueId}``. Suites map to Jira components. All reads are
    GET-only; Qase writes honour dry-run.
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

    def import_cases(self, project: dict):
        self._warned_priorities = set()
        self._warned_components = set()
        self._warned_no_labels_field = False
        self._warned_labels = set()
        self._max_tags_seen = 0
        self._current_code = project.get("code")
        _pid = self.config.get("cases.preserve_ids")
        self._preserve_ids = True if _pid is None else bool(_pid)
        _ru = self.config.get("users.resolve_from_jira")
        self._resolve_users = True if _ru is None else bool(_ru)
        asyncio.run(self._import_cases_async(project))

    @staticmethod
    def _text(value) -> str:
        """Jira field text → plain string. api/2 gives wiki-markup strings; guard
        against ADF dicts (api/3) with a best-effort text extraction."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):  # ADF document — pull text nodes
            out = []

            def walk(node):
                if isinstance(node, dict):
                    if node.get("type") == "text" and node.get("text"):
                        out.append(node["text"])
                    for c in node.get("content") or []:
                        walk(c)
                elif isinstance(node, list):
                    for c in node:
                        walk(c)

            walk(value)
            return " ".join(out)
        return str(value)

    async def _import_cases_async(self, project: dict):
        code = project["code"]
        zephyr_key = project["zephyr_key"]
        project_id = project.get("zephyr_project_id")

        priority_map = self.mappings.qase_priority_keys_to_id
        status_map = self.mappings.qase_case_status_keys_to_id
        suite_map = self.mappings.suites.get(code) or {}

        # 1 — enumerate Jira test issues (paginated)
        self.logger.log(f"[{code}][Cases] Fetching test issues (issuetype={self.zephyr.issue_type}) from Jira")
        issues = []
        for page in self.zephyr.get_test_issues(zephyr_key):
            issues.extend(page)
        if not issues:
            self.logger.log(f"[{code}][Cases] No test issues found")
            return
        # DEV: cases.limit caps how many test issues are imported (fast validation
        # runs). 0/absent = no limit (full migration).
        _limit = self.config.get("cases.limit")
        if _limit and int(_limit) > 0 and len(issues) > int(_limit):
            issues = issues[: int(_limit)]
            self.logger.log(
                f"[{code}][Cases] cases.limit={int(_limit)} — importing only the first "
                f"{len(issues)} test issue(s) (DEV/validation)",
                "warning",
            )
        total = len(issues)
        self.logger.log(f"[{code}][Cases] Found {total} test issue(s)")
        self.mappings.stats.add_entity_count(code, "cases", "zephyr-squad", total)

        # 2 — process in BATCHES so cases appear progressively: per chunk, fetch
        # steps (+ attachments), build, and bulk-create. Total time is unchanged
        # (the ZAPI rate limit is the floor), but cases land after each batch
        # instead of only at the very end.
        batch_size = int(self.config.get("cases.batch_size") or 50)
        attachments_enabled = bool(self.config.get("zephyr.attachments.enabled"))
        # Files attached to a test's step DEFINITIONS (Zephyr teststep attachments).
        # Earlier versions never fetched these and dropped them silently. Qase
        # honours step files at case create, so they go into the bulk payload with
        # the step.
        _ts = self.config.get("zephyr.attachments.teststep")
        teststep_files = attachments_enabled and (True if _ts is None else bool(_ts))
        self._step_att = {}  # (issue_id, step_id) -> [qase_hash, ...]
        step_file_counts = {"ok": 0, "lost": 0}
        self.logger.log(f"[{code}][Cases] test-step files: {'ON' if teststep_files else 'OFF'}")
        semaphore = asyncio.Semaphore(16)
        self._att_cache = {}  # issue_id -> [qase_hash, ...]
        # issue_id -> {filename: qase_url}. Jira comments embed screenshots as
        # wiki markup (!image-x.png|width=..!), which Qase renders as literal
        # text. The file itself IS migrated, so keeping its URL lets the comment
        # point at the copy we uploaded instead of a dead Jira reference.
        self._att_urls = {}

        async def fetch_steps(issue, cache):
            iid = issue.get("id")
            async with semaphore:
                try:
                    cache[iid] = await self.pools.source(
                        self.zephyr.get_test_steps, iid, project_id
                    ) or []
                except Exception as e:
                    cache[iid] = []
                    self.logger.log(
                        f"[{code}][Cases] steps fetch failed for {issue.get('key')}: {e}", "warning"
                    )
                # Share with the Runs phase: it needs the same steps to order and
                # label step results, and would otherwise re-fetch every test.
                # Keyed by STRING id in both places — a type mismatch would only
                # cause a cache miss (and a re-fetch), never a wrong-test hit.
                if cache[iid]:
                    self.mappings.step_defs.setdefault(code, {})[str(iid)] = cache[iid]

        async def fetch_step_files(issue, cache):
            iid, ikey = issue.get("id"), issue.get("key")
            for st in cache.get(iid) or []:
                for a in st.get("attachments") or []:
                    if not isinstance(a, dict) or str(a.get("entityType") or "teststep").lower() != "teststep":
                        continue
                    aid, name = a.get("id"), (a.get("name") or a.get("fileName") or "attachment")
                    async with semaphore:
                        try:
                            raw = await self.pools.source(
                                self.zephyr.download_attachment_bytes, aid, project_id, iid
                            )
                            if not raw:
                                raise ValueError("empty download")
                            res = await self.pools.qs(self.qase.upload_attachment, code, (name, raw))
                            h = (res or {}).get("hash")
                            if not h:
                                raise ValueError("no hash returned by Qase")
                        except Exception as e:
                            step_file_counts["lost"] += 1
                            # Same recovery context as the Runs phase, so a later pass can re-fetch it
                            self.logger.log(
                                f"[{code}][Cases] test-step attachment {name!r} NOT migrated: {e} "
                                f"[recover: attachmentId={aid} issueId={iid} projectId={project_id} "
                                f"stepId={st.get('id')} key={ikey}]", "warning"
                            )
                            continue
                    step_file_counts["ok"] += 1
                    self._step_att.setdefault((str(iid), str(st.get("id"))), []).append(h)

        async def fetch_att(issue):
            iid, ikey = issue.get("id"), issue.get("key")
            async with semaphore:
                try:
                    atts = await self.pools.source(self.zephyr.get_issue_attachments, ikey)
                except Exception as e:
                    self.logger.log(f"[{code}][Cases] attachment list failed for {ikey}: {e}", "warning")
                    return
                hashes = []
                for a in (atts or []):
                    url, name = a.get("url"), (a.get("filename") or "attachment")
                    if not url:
                        continue
                    raw = await self.pools.source(self.zephyr.download_jira_attachment_bytes, url)
                    if raw is None:
                        self.logger.log(f"[{code}][Cases] could not download {name!r} for {ikey}", "warning")
                        continue
                    res = await self.pools.qs(self.qase.upload_attachment, code, (name, raw))
                    if res and res.get("hash"):
                        hashes.append(res["hash"])
                    if res and res.get("url"):
                        self._att_urls.setdefault(iid, {})[name] = res["url"]
                if hashes:
                    self._att_cache[iid] = hashes

        n_batches = (total + batch_size - 1) // batch_size
        self.logger.log(f"[{code}][Cases] Importing {total} case(s) in {n_batches} batch(es) of {batch_size}")
        total_created = 0
        for bi in range(n_batches):
            batch = issues[bi * batch_size:(bi + 1) * batch_size]
            steps_cache = {}
            await asyncio.gather(*[fetch_steps(i, steps_cache) for i in batch])
            if attachments_enabled:
                await asyncio.gather(*[fetch_att(i) for i in batch])
            if teststep_files:
                await asyncio.gather(*[fetch_step_files(i, steps_cache) for i in batch])

            built = []  # (key, issue_id, obj)
            for issue in batch:
                obj = self._build_qase_case(issue, suite_map, steps_cache, priority_map, status_map)
                if obj is not None:
                    built.append((issue.get("key") or "", issue.get("id"), obj))
            if not built:
                continue

            keys = [k for k, _, _ in built]
            ids = [i for _, i, _ in built]
            objs = [o for _, _, o in built]
            # preserve_ids: Qase case id == the id we assign (key suffix), so register
            # the mapping without the expensive per-case hydration round-trip.
            preserved = [getattr(o, "id", None) for o in objs]

            # Delta mode: skip cases the target already holds. get_existing_project_state
            # collected these ids from the start but NOTHING consumed them, so a delta
            # re-run recreated every case. The mapping registration below is the part
            # that must not be skipped: the Runs phase resolves results to cases through
            # zephyr_tc_id_to_qase_case_id, so a skipped case still needs its id recorded
            # or every result for it is silently stranded.
            _existing = (self.mappings.existing_state.get(code) or {}).get("case_ids") or set()
            if _existing:
                _keep, _skipped = [], 0
                for i, q in enumerate(preserved):
                    if q is not None and int(q) in _existing:
                        if keys[i]:
                            self.mappings.register_zephyr_testcase_qase_case_id(code, keys[i], int(q))
                        if ids[i] is not None:
                            self.mappings.register_zephyr_testcase_qase_case_id(code, str(ids[i]), int(q))
                        _skipped += 1
                    else:
                        _keep.append(i)
                if _skipped:
                    self.logger.log(
                        f"[{code}][Cases] delta: {_skipped} case(s) already in Qase — "
                        f"id mapped, not recreated"
                    )
                keys = [keys[i] for i in _keep]
                ids = [ids[i] for i in _keep]
                objs = [objs[i] for i in _keep]
                preserved = [preserved[i] for i in _keep]
                if not objs:
                    continue

            _, qase_ids = self.qase.create_cases(code, objs)
            for i in range(len(objs)):
                qid = preserved[i]
                if qid is None and i < len(qase_ids):
                    qid = qase_ids[i]  # fallback (preserve_ids off) — needs hydration on
                if qid is not None:
                    if keys[i]:
                        self.mappings.register_zephyr_testcase_qase_case_id(code, keys[i], qid)
                    if ids[i] is not None:
                        self.mappings.register_zephyr_testcase_qase_case_id(code, str(ids[i]), qid)
                    total_created += 1
                else:
                    self.mappings.stats.add_issue(
                        code, "case", f"{keys[i] or f'row {i}'}: FAILED to create in Qase", "error"
                    )
            self.logger.log(
                f"[{code}][Cases] Batch {bi + 1}/{n_batches} done — {total_created}/{total} case(s) in Qase"
            )

        self.mappings.stats.add_entity_count(code, "cases", "qase", total_created)
        self.logger.log(f"[{code}][Cases] Created {total_created}/{total} case(s) in Qase")
        if teststep_files:
            self.logger.log(
                f"[{code}][Cases] test-step files: {step_file_counts['ok']} migrated, "
                f"{step_file_counts['lost']} NOT migrated",
                "warning" if step_file_counts["lost"] else "info",
            )
        self._log_zapi_retries(code, "Cases")

    def _build_qase_case(self, issue, suite_map, steps_cache, priority_map, status_map):
        fields = issue.get("fields") or {}
        key = issue.get("key") or ""
        issue_id = issue.get("id")

        title = (self._text(fields.get("summary")).strip() or key or "Untitled")[:255]
        description = self._text(fields.get("description")).strip()

        # Priority (Jira {name, id})
        pr = fields.get("priority") or {}
        pr_name = (pr.get("name") if isinstance(pr, dict) else str(pr) or "").strip().lower()
        slug = _PRIORITY_MAP.get(pr_name)
        if slug is None:
            override = (self.config.get("cases.priority_map") or {}).get(pr_name)
            slug = str(override).lower() if override else "medium"
            if not override and pr_name and pr_name not in self._warned_priorities:
                self._warned_priorities.add(pr_name)
                self.logger.log(f"[Cases] Unmapped Jira priority {pr_name!r} → 'medium'", "warning")
        priority_id = priority_map.get(slug) or priority_map.get("medium")
        status_id = status_map.get("actual")

        # Suite: one-per-component by default; with features.single_suite EVERY case
        # is filed under the project's single migration suite instead.
        components = fields.get("components") or []
        suite_id = None
        comp_names = []
        for c in components:
            cid = str(c.get("id")) if isinstance(c, dict) else None
            cname = (c.get("name") if isinstance(c, dict) else "") or ""
            if cname:
                comp_names.append(cname)
            if suite_id is None and cid in suite_map:
                suite_id = suite_map[cid]
        single = self.mappings.single_suite.get(self._current_code)
        if single is not None:
            suite_id = single

        steps = self._build_steps(steps_cache.get(issue_id) or [])

        data = {
            "title": title,
            "description": description or None,
            "steps": steps or None,
        }
        if suite_id is not None:
            data["suite_id"] = suite_id
        if priority_id is not None:
            data["priority"] = priority_id
        if status_id is not None:
            data["status"] = status_id

        # preserve id from the Jira key numeric suffix (e.g. PROJ-123 → 123)
        if self._preserve_ids and key:
            m = re.search(r"-(\d+)$", key)
            if m:
                data["id"] = int(m.group(1))

        # Per-project component field ("Feature Area"): ALL of the issue's Jira
        # components, as a comma-separated list of multiselect option ids (the
        # format Qase requires). Multi-component tests keep every component —
        # which a suite (one folder per case) could not represent.
        comp_cf = self.mappings.component_field.get(self._current_code) or {}
        comp_fid, comp_opts = comp_cf.get("field_id"), comp_cf.get("options") or {}
        if comp_fid and comp_opts:
            option_ids = []
            for name in comp_names:
                oid = comp_opts.get(name.strip().lower())
                if oid is not None and oid not in option_ids:
                    option_ids.append(oid)
            if not option_ids:
                if comp_names:
                    self._warn_missing_components(comp_names)
                # No component (or none recognised) → explicit placeholder option
                fallback = str(
                    self.config.get("features.component_field_fallback_option") or ""
                ).strip().lower()
                oid = comp_opts.get(fallback) if fallback else None
                if oid is not None:
                    option_ids = [oid]
            if option_ids:
                data.setdefault("custom_field", {})[str(comp_fid)] = ",".join(
                    str(o) for o in option_ids
                )

        # Jira labels -> the per-project "Labels" MULTISELECT CF, NOT Qase tags.
        # Same encoding as components: a comma-separated list of option ids, which
        # is the format Qase requires for multiselect. No tags are sent at all.
        labels = [str(t).strip() for t in (fields.get("labels") or []) if t]
        lab_cf = self.mappings.labels_field.get(self._current_code) or {}
        lab_fid, lab_opts = lab_cf.get("field_id"), lab_cf.get("options") or {}
        if labels and lab_fid and lab_opts:
            option_ids = []
            for name in labels:
                oid = lab_opts.get(name.strip().lower())
                if oid is not None and oid not in option_ids:
                    option_ids.append(oid)
                elif oid is None:
                    self._warn_missing_label(name)
            if option_ids:
                data.setdefault("custom_field", {})[str(lab_fid)] = ",".join(
                    str(o) for o in option_ids
                )
        elif labels and lab_fid is None and not self._labels_cf_enabled():
            # features.labels_custom_field is OFF -> fall back to native Qase tags,
            # which is the historical behaviour.
            tags = list(labels)
            if not comp_fid:
                tags += comp_names
            if tags:
                if len(tags) > 10:
                    # Qase caps a case at 10 tags and silently drops the rest. Say so:
                    # this quietly lost labels and component names before.
                    self._warn_tag_overflow(len(tags))
                data["tags"] = tags[:10]
        elif labels and not self._warned_no_labels_field:
            self._warned_no_labels_field = True
            self.logger.log(
                f"[{self._current_code}][Cases] 'Labels' CF unavailable — Jira labels "
                f"are NOT being migrated", "error",
            )

        # Authorship: the Jira reporter (falling back to assignee) matched onto a
        # user that already exists in Qase; unmatched → users.default.
        author_id = self._resolve_author(fields)
        if author_id is not None:
            data["author_id"] = author_id

        # Keep the date the test was really written. Without this every migrated
        # case is stamped with the migration date, which makes "when did we write
        # this test" unanswerable. Qase accepts created_at/updated_at only at
        # CREATION, so it cannot be backfilled. Epoch is rejected; a string works.
        created_at = self._jira_ts(fields.get("created"))
        if created_at:
            data["created_at"] = created_at
        updated_at = self._jira_ts(fields.get("updated"))
        if updated_at:
            data["updated_at"] = updated_at

        # Jira issue attachments uploaded to Qase (when enabled)
        att = (getattr(self, "_att_cache", {}) or {}).get(issue_id)
        if att:
            data["attachments"] = att

        # Jira issue links (traceability) → 'Jira Links' custom field. Retains the
        # stories/bugs/epics/etc. this test links to; DROPS test-to-test clone/
        # duplicate links (they point at abandoned Zephyr test issues). Qase has no
        # native refs field.
        fid = getattr(self.mappings, "jira_links_field_id", None)
        if fid:
            jira_base = str(self.config.get("jira.base_url") or "").rstrip("/")
            lines = []
            for rel, linked_key, _itype in self._linked_issues(fields):
                url = f"{jira_base}/browse/{linked_key}" if jira_base else linked_key
                lines.append(f"{rel} {linked_key} — {url}")
            if lines:
                data.setdefault("custom_field", {})[str(fid)] = "\n".join(lines)
            # Remember it: the Runs phase appends the defects raised while
            # EXECUTING this test, which are a Zephyr-side relationship and so
            # are absent from Jira's own issuelinks.
            self.mappings.case_jira_links.setdefault(self._current_code, {})[
                str(issue_id)
            ] = "\n".join(lines)

        # Jira fix versions -> (a) the case's native milestone_id, and (b) the
        # "Fix Versions" multiselect CF holding ALL of them.
        # milestone_id is SINGULAR while cases routinely carry several fix versions
        # (observed: up to 58 on a single case), so it is only set when there is
        # unambiguous; the CF always carries the complete set so nothing is lost.
        fix_versions = fields.get("fixVersions") or []
        fv_names, fv_ids = [], []
        for v in fix_versions:
            if not isinstance(v, dict):
                continue
            nm = (v.get("name") or "").strip()
            if nm:
                fv_names.append(nm)
            if v.get("id") is not None:
                fv_ids.append(str(v["id"]))

        # features.case_milestone (default OFF): set the case's native milestone_id
        # when — and only when — there is exactly one fix version. It is a singular
        # field while cases routinely carry several (up to 58 observed), so it can
        # otherwise only hold a partial truth. The "Fix Versions" field below always
        # carries the complete set. Runs get their milestone regardless; that is
        # where release traceability lives.
        _cm = self.config.get("features.case_milestone")
        if (False if _cm is None else bool(_cm)) and len(fv_ids) == 1:
            ms_id = (self.mappings.version_to_milestone.get(self._current_code) or {}).get(
                fv_ids[0]
            )
            if ms_id:
                data["milestone_id"] = int(ms_id)

        fv_cf = self.mappings.fixversion_field.get(self._current_code) or {}
        fv_fid, fv_opts = fv_cf.get("field_id"), fv_cf.get("options") or {}
        if fv_names and fv_fid and fv_opts:
            option_ids = []
            for name in fv_names:
                oid = fv_opts.get(name.strip().lower())
                if oid is not None and oid not in option_ids:
                    option_ids.append(oid)
            if option_ids:
                data.setdefault("custom_field", {})[str(fv_fid)] = ",".join(
                    str(o) for o in option_ids
                )

        # Jira "Affects Version/s" -> the "Affected Versions" multiselect CF.
        # Same option list as Fix Versions and the same shape, but a different
        # fact: the release the problem was found in.
        av_cf = self.mappings.affectedversion_field.get(self._current_code) or {}
        av_fid, av_opts = av_cf.get("field_id"), av_cf.get("options") or {}
        if av_fid and av_opts:
            av_ids = []
            for v in (fields.get("versions") or []):
                if not isinstance(v, dict):
                    continue
                oid = av_opts.get((v.get("name") or "").strip().lower())
                if oid is not None and oid not in av_ids:
                    av_ids.append(oid)
            if av_ids:
                data.setdefault("custom_field", {})[str(av_fid)] = ",".join(
                    str(o) for o in av_ids
                )

        # Arbitrary Jira custom fields mirrored onto the case (cases.jira_custom_fields)
        for jira_id, cf in (
            self.mappings.jira_custom_fields.get(self._current_code) or {}
        ).items():
            fid, opts = cf.get("field_id"), cf.get("options") or {}
            if not fid or not opts:
                continue
            vals = self._flatten_jira_value(fields.get(jira_id))
            option_ids = []
            for v in vals:
                oid = opts.get(v.strip().lower())
                if oid is not None and oid not in option_ids:
                    option_ids.append(oid)
            if option_ids:
                data.setdefault("custom_field", {})[str(fid)] = ",".join(
                    str(o) for o in option_ids
                )

        # Jira issue comments → 'Comments' CF (cases.comments_custom_field).
        # They arrive on the issue read we already make, so this costs no extra
        # API call.
        cmt_fid = self.mappings.comments_field_id
        if cmt_fid:
            text = self._format_comments(
                fields, int(self.config.get("cases.comments_limit") or 0),
                (getattr(self, "_att_urls", {}) or {}).get(issue_id) or {},
            )
            if text:
                data.setdefault("custom_field", {})[str(cmt_fid)] = text[:10000]

        # Jira issue changelog -> 'Change History' CF
        # (cases.changehistory_custom_field). Rides along on the issue read via
        # expand=changelog, so it costs no extra API call.
        ch_fid = self.mappings.changehistory_field_id
        if ch_fid:
            hist = self._format_changelog(
                issue, int(self.config.get("cases.changehistory_limit") or 0)
            )
            if hist:
                data.setdefault("custom_field", {})[str(ch_fid)] = hist[:10000]

        # Fixed values for the target's REQUIRED custom fields (Zephyr has no
        # equivalent data). Per project, since a field only exists in the projects
        # it is scoped to. setdefault: never override a value we actually migrated.
        for fid, value in (
            (self.mappings.required_field_defaults or {}).get(self._current_code) or {}
        ).items():
            data.setdefault("custom_field", {}).setdefault(str(fid), value)

        data["is_flaky"] = 0
        return TestCasebulkCasesInner(**data)

    @staticmethod
    def _flatten_jira_value(raw) -> list:
        """Jira custom-field value -> list of display strings.

        Custom fields come back as a scalar, an option dict ({"value": ...}), or a
        list of either, so every shape has to be handled or values are dropped.
        """
        if raw is None:
            return []
        if isinstance(raw, list):
            out = []
            for item in raw:
                out.extend(Cases._flatten_jira_value(item))
            return out
        if isinstance(raw, dict):
            for k in ("value", "name", "displayName"):
                if raw.get(k):
                    return [str(raw[k]).strip()]
            return []
        return [str(raw).strip()]

    def _labels_cf_enabled(self) -> bool:
        v = self.config.get("features.labels_custom_field")
        return True if v is None else bool(v)

    def _warn_tag_overflow(self, n: int) -> None:
        """Warn once per project when a case exceeds Qase's 10-tag cap.

        The cap truncates silently, so labels and component names simply vanished
        with no record. Reported once with the worst count seen."""
        if n > getattr(self, "_max_tags_seen", 0):
            self._max_tags_seen = n
            self.logger.log(
                f"[{self._current_code}][Cases] a case carries {n} tag(s) but Qase "
                f"keeps only 10 — the rest are dropped. Consider "
                f"features.labels_custom_field to migrate labels without a cap.",
                "warning",
            )

    def _warn_missing_label(self, name: str) -> None:
        """Warn once per label that has no option in the Labels field, e.g. a label
        added in Jira between the option scan and the case import. Without this the
        label would be dropped silently."""
        key = (name or "").strip().lower()
        if key and key not in self._warned_labels:
            self._warned_labels.add(key)
            self.logger.log(
                f"[{self._current_code}][Cases] Jira label {name!r} has no matching "
                f"option in the 'Labels' field — not migrated for affected case(s)",
                "warning",
            )

    def _warn_missing_components(self, comp_names: list) -> None:
        """Warn once per component name that has no option in the component field
        (e.g. a component added in Jira after the field was created)."""
        for name in comp_names:
            key = (name or "").strip().lower()
            if key and key not in self._warned_components:
                self._warned_components.add(key)
                self.logger.log(
                    f"[{self._current_code}][Cases] Jira component {name!r} has no matching "
                    f"option in the component field — not migrated for affected case(s)",
                    "warning",
                )

    @staticmethod
    def _jira_ts(value):
        """Jira timestamp -> 'YYYY-MM-DD HH:MM:SS' UTC, or None.

        Jira returns e.g. '2019-03-14T09:12:00.000+0000'. Qase rejects epoch
        seconds and accepts a string, so normalise rather than pass it through.
        """
        if not value:
            return None
        text = str(value).strip()
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                    "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                    "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(text, fmt)
            except ValueError:
                continue
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        return None

    def _resolve_author(self, fields: dict):
        """Qase user id for the case author, from the Jira reporter (else assignee).

        Matches onto users that ALREADY exist in Qase (by email, else display
        name); no user is ever created. Anything unmatched falls back to
        ``users.default`` — the SAME fallback the runs step applies to results, so
        authorship is consistent. (Omitting it instead would let Qase silently
        attribute the case to whoever's API token ran the migration.)
        """
        if not self._resolve_users:
            return self.mappings.default_user
        for key in ("reporter", "assignee"):
            person = fields.get(key)
            if not isinstance(person, dict):
                continue
            account_id = person.get("accountId")
            email = person.get("emailAddress")
            name = person.get("displayName")
            # Remember for the runs step: executions carry only an account id.
            self.mappings.note_source_user(account_id, email, name)
            uid = self.mappings.resolve_source_user(account_id, email, name)
            if uid is not None:
                return uid
        return self.mappings.default_user

    @staticmethod
    def _format_comments(fields: dict, limit: int = 0, att_urls: dict = None) -> str:
        """Jira issue comments → plain text for the 'Comments' custom field.

        Qase has no comments API, so this is the only route. Rendered oldest
        first as ``[YYYY-MM-DD] Author: body`` so the thread reads in order.

        ``limit`` (cases.comments_limit, 0 = all) keeps the newest N when a case
        carries a long thread — a custom field is a single text blob, so an
        unbounded thread makes the case hard to read.
        """
        block = fields.get("comment") or {}
        raw = block.get("comments") if isinstance(block, dict) else None
        if not isinstance(raw, list) or not raw:
            return ""
        if limit and len(raw) > limit:
            raw = raw[-limit:]
        lines = []
        for cm in raw:
            if not isinstance(cm, dict):
                continue
            body = _strip_html(str(cm.get("body") or "")).strip()
            body = Cases._render_jira_images(body, att_urls)
            if not body:
                continue
            who = ((cm.get("author") or {}).get("displayName") or "").strip()
            when = str(cm.get("created") or "")[:10]
            head = " ".join(x for x in (f"[{when}]" if when else "", who) if x).strip()
            lines.append(f"{head}: {body}" if head else body)
        return "\n\n".join(lines)

    _JIRA_IMAGE = re.compile(
        r"!([^!|\r\n]+?\.(?:png|jpe?g|gif|bmp|webp|svg))(?:\|[^!\r\n]*)?!", re.I
    )

    @classmethod
    def _render_jira_images(cls, text: str, att_urls: dict = None) -> str:
        """Jira's embedded-image markup → something Qase can actually show.

        A Jira comment embeds a screenshot as ``!image-x.png|width=673,height=379!``.
        Qase has no Jira renderer, so that arrives as literal text and the
        customer sees a broken reference where a screenshot should be.

        The file itself is not lost: when ``zephyr.attachments.enabled`` is on we
        already download it and upload it to the case, so a Qase-hosted URL
        exists under the SAME filename. Rewrite the markup to point there, as
        markdown, which degrades to a readable filename plus link if the field
        is not rendered. With no URL (attachments off, or the file failed to
        upload) fall back to a plain marker so the reader at least knows a
        screenshot belongs there rather than seeing Jira syntax.
        """
        if not text:
            return text
        urls = att_urls or {}

        def _sub(m):
            name = (m.group(1) or "").strip()
            url = urls.get(name)
            return f"![{name}]({url})" if url else f"[screenshot: {name}]"

        return cls._JIRA_IMAGE.sub(_sub, text)

    @staticmethod
    def _format_changelog(issue: dict, limit: int = 0) -> str:
        """Jira issue changelog → plain text for the 'Change History' field.

        Rendered oldest first as ``[YYYY-MM-DD] Author: field: from → to`` so
        the history reads in order, matching how Comments are rendered.

        Two things make this worth carrying despite Qase having no native home
        for it. It is the only record of what was edited on a test script, and
        its STATUS transitions are the only place the reviewer and approver
        appear when the dedicated Jira approver fields are left empty: then
        "In Review → Approved" and who made that move is the sole evidence of
        sign-off.

        Long values (a rewritten description, say) are truncated per side so a
        single edit cannot consume the whole field.
        """
        hist = (issue.get("changelog") or {}).get("histories") or []
        if not isinstance(hist, list) or not hist:
            return ""
        rows = []
        for h in hist:
            if not isinstance(h, dict):
                continue
            who = ((h.get("author") or {}).get("displayName") or "").strip()
            when = str(h.get("created") or "")[:10]
            for it in (h.get("items") or []):
                if not isinstance(it, dict):
                    continue
                field = (it.get("field") or "").strip() or "field"
                # Collapse newlines: a rewritten description would otherwise
                # sprawl across the field and break the one-line-per-change
                # format that makes the history readable at all.
                before = _one_line(it.get("fromString"))
                after = _one_line(it.get("toString"))
                if before == after:
                    continue
                head = " ".join(x for x in (f"[{when}]" if when else "", who) if x).strip()
                line = f"{field}: {before} → {after}"
                rows.append((str(h.get("created") or ""), f"{head}: {line}" if head else line))
        if not rows:
            return ""
        rows.sort(key=lambda r: r[0])          # oldest first
        if limit and len(rows) > limit:
            rows = rows[-limit:]               # keep the most recent N
        return "\n".join(r[1] for r in rows)

    @staticmethod
    def _linked_issues(fields: dict) -> list:
        """Extract (relationship, linked_key, issuetype) from a Jira issue's
        ``issuelinks``, EXCLUDING links whose target is another Test issue
        (clone/duplicate links to abandoned Zephyr tests)."""
        out = []
        for link in (fields.get("issuelinks") or []):
            ltype = link.get("type") or {}
            outward, inward = link.get("outwardIssue"), link.get("inwardIssue")
            if outward:
                linked, rel = outward, (ltype.get("outward") or "relates to")
            elif inward:
                linked, rel = inward, (ltype.get("inward") or "relates to")
            else:
                continue
            lk = linked.get("key")
            itype = (((linked.get("fields") or {}).get("issuetype") or {}).get("name") or "").strip()
            if not lk or itype.lower() == "test":
                continue
            out.append((rel, lk, itype))
        return out

    def _build_steps(self, raw_steps: list) -> list:
        """Zephyr Squad teststep objects → Qase step dicts.

        Confirmed shape (Zephyr Squad Cloud): ``{orderId, step, result, ...}`` where
        ``step`` is the action and ``result`` the expected result (both may carry
        light HTML). Ordered by ``orderId``.
        """
        steps = []
        step_att = getattr(self, "_step_att", None) or {}
        # cases.step_text_limit: chars kept per step field. Default 0 = no cut.
        # The tool used to cut at 1,000, but Qase stores at least 100,000 per field,
        # so the cut dropped real text. Set a positive number only to reproduce the
        # old behaviour, e.g. 1000 when comparing against it.
        cfg = getattr(self, "config", None)
        _lim = cfg.get("cases.step_text_limit") if cfg is not None else None
        lim = 0 if _lim is None else int(_lim)
        cut = (lambda t: t) if lim <= 0 else (lambda t: t[:lim])
        for st in sorted(raw_steps, key=lambda s: s.get("orderId") or 0):
            action = _strip_html(self._text(st.get("step"))) or "Step"
            expected = _strip_html(self._text(st.get("result")))
            data_field = _strip_html(self._text(st.get("data")))
            step = {
                "action": cut(action),
                "expected_result": cut(expected) if expected else None,
                "data": cut(data_field) if data_field else None,
            }
            files = step_att.get((str(st.get("issueId")), str(st.get("id"))))
            if files:
                step["attachments"] = files
            steps.append(step)
        return steps

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
