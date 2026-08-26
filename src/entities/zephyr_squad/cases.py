import asyncio
import re

from qaseio.models import TestCasebulkCasesInner

from ...service.qase import QaseService
from ...service.zephyr_squad import ZephyrSquadService
from ...support.config_manager import ConfigManager
from ...support.html import strip_html as _strip_html
from ...support.logger import Logger
from ...support.mappings import Mappings
from ...support.pools import Pools

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
        semaphore = asyncio.Semaphore(16)
        self._att_cache = {}  # issue_id -> [qase_hash, ...]

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
        for st in sorted(raw_steps, key=lambda s: s.get("orderId") or 0):
            action = _strip_html(self._text(st.get("step"))) or "Step"
            expected = _strip_html(self._text(st.get("result")))
            data_field = _strip_html(self._text(st.get("data")))
            steps.append({
                "action": action[:1000],
                "expected_result": expected[:1000] if expected else None,
                "data": data_field[:1000] if data_field else None,
            })
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
