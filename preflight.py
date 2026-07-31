"""Preflight — READ-ONLY validation + response-shape discovery (ZAPI Cloud + Jira).

Run:  python preflight.py [config.json]

ONLY performs GET requests against the customer's Zephyr Squad (ZAPI Cloud) and
Jira, and against your Qase workspace. Never creates/edits/deletes anything
(CLAUDE.md, Rules 1/2/7).

Flow:
  1. Config parses; required keys present.
  2. Zephyr Squad JWT auth (GET /serverinfo), test issue type, execution statuses.
  3. Qase auth (GET /v1/project).
  4. Jira auth (GET /myself) — required to resolve the numeric projectId (ZAPI Cloud
     has no project-list endpoint) and to enumerate test issues.
  5. Shape discovery: resolve projectId + versions from Jira, then sample one cycle,
     execution, test steps, step results and attachments (all GET). Sampled shapes
     are written to gitignored ``preflight_shapes.json`` for the transform layer.

Exit 0 = all green; 1 = a failure.
"""

import json
import os
import sys

import requests

from src.support.config_manager import ConfigManager
from src.support.logger import Logger
from src.api.zephyr_squad import ZephyrSquadApiClient
from src.api.jira_lookup import JiraLookupClient
from src.exceptions.api import APIError

_PLACEHOLDER_MARKERS = ("<", ">", "REPLACE_ME", "your-", "YOUR_", "changeme", "xxxx")
_SHAPES_FILE = "preflight_shapes.json"

_results = []
_shapes = {}


def _report(name, ok, detail=""):
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))
    _results.append(ok)


def _warn(name, detail=""):
    print(f"  ⚠️  {name}" + (f" — {detail}" if detail else ""))


def _looks_placeholder(value):
    return any(m in value for m in _PLACEHOLDER_MARKERS)


def _shape_of(obj):
    if isinstance(obj, dict):
        return {"type": "dict", "keys": sorted(obj.keys())}
    if isinstance(obj, list):
        head = obj[0] if obj else None
        return {"type": "list", "len": len(obj),
                "element_keys": sorted(head.keys()) if isinstance(head, dict) else type(head).__name__}
    return {"type": type(obj).__name__}


def _record_shape(label, obj, sample_first=True):
    shape = _shape_of(obj)
    print(f"    · {label}: {json.dumps(shape)}")
    sample = obj[0] if (isinstance(obj, list) and obj and sample_first) else obj
    _shapes[label] = {"shape": shape, "sample": sample}
    return sample


def _first(d, *names):
    for n in names:
        if isinstance(d, dict) and d.get(n) is not None:
            return d.get(n)
    return None


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "./config.json"

    print("\n— Config —")
    if not os.path.exists(config_path):
        _report(f"Config file {config_path}", False, "not found — copy config.example.json")
        return _finish()
    try:
        with open(config_path) as f:
            json.load(f)
    except json.JSONDecodeError as e:
        _report(f"Config file {config_path}", False, f"invalid JSON: {e}")
        return _finish()
    _report(f"Config file {config_path}", True, "parses OK")

    config = ConfigManager(config_file=config_path)
    config.load_config()

    required = {
        "qase.api_token": "Qase API token",
        "zephyr.account_id": "Atlassian account id",
        "zephyr.access_key": "ZAPI Access Key",
        "zephyr.secret_key": "ZAPI Secret Key",
    }
    config_ok = True
    for key, label in required.items():
        value = str(config.get(key) or "").strip()
        if not value or _looks_placeholder(value):
            _report(f"{key} ({label})", False, "missing/placeholder")
            config_ok = False
        else:
            _report(f"{key} ({label})", True)

    projects = [str(p).strip() for p in (config.get("projects.import") or []) if str(p).strip()]
    real_keys = [k for k in projects if not _looks_placeholder(k)]
    if real_keys:
        _report("projects.import", True, f"{real_keys}")
    else:
        _warn("projects.import has no real key yet", "set it to the customer's Jira project key(s)")

    issue_type = str(config.get("cases.jira_issue_type") or "Test").strip() or "Test"

    jira_vals = {k: str(config.get(k) or "").strip()
                 for k in ("jira.base_url", "jira.email", "jira.api_token")}
    jira_configured = all(v and not _looks_placeholder(v) for v in jira_vals.values())
    if jira_configured:
        _report("Jira config", True, "all three jira.* set")
    else:
        _warn("Jira not fully configured",
              "needed to resolve projectId + enumerate tests — set jira.base_url/email/api_token")

    if not config_ok:
        return _finish()

    logger = Logger(False, prefix="preflight")

    # ---------------- Zephyr Squad (ZAPI Cloud) ----------------
    print("\n— Zephyr Squad (ZAPI Cloud) —")
    client = ZephyrSquadApiClient(
        account_id=str(config.get("zephyr.account_id")),
        access_key=str(config.get("zephyr.access_key")),
        secret_key=str(config.get("zephyr.secret_key")),
        logger=logger, base_url=str(config.get("zephyr.base_url") or "") or None, max_retries=1,
    )
    try:
        info = client.get_server_info()
        addon = (info.get("addonServerInfo") or {}).get("version")
        jira_base = (info.get("jiraServerInfo") or {}).get("baseUri")
        _report("JWT auth · GET /serverinfo", True, f"Zephyr addon {addon}, Jira {jira_base}")
    except APIError as e:
        _report("JWT auth · GET /serverinfo", False, str(e)[:200])
        return _finish()

    try:
        it = (client.get_general_information() or {}).get("issueType") or {}
        _report("Test issue type", True, f"{it.get('name')!r} (id={it.get('id')})")
    except APIError as e:
        _warn("config/generalinformation failed", str(e)[:160])

    try:
        _record_shape("zapi.execution_statuses", client.get_execution_statuses(), sample_first=False)
        _report("execution/statuses", True, "captured")
    except Exception as e:
        _warn("execution/statuses failed", str(e)[:160])

    # ---------------- Jira ----------------
    print("\n— Jira —")
    jira = None
    if not jira_configured:
        _warn("Jira checks skipped", "add jira.* to resolve projectId and sample data")
    else:
        jira = JiraLookupClient(jira_vals["jira.base_url"], jira_vals["jira.email"],
                                jira_vals["jira.api_token"], logger, max_retries=1)
        try:
            me = jira.get_myself()
            _report("Jira auth · GET /myself", True, f"{me.get('displayName')} ({me.get('accountId')})")
        except APIError as e:
            _report("Jira auth · GET /myself", False, str(e)[:200])
            jira = None

    # ---------------- Shape discovery (Jira → projectId → ZAPI samples) ----------------
    if jira and real_keys:
        key = real_keys[0]
        print(f"\n— Shape discovery (project {key}; read-only) —")
        project_id = None
        try:
            proj = jira.get_project(key)
            project_id = proj.get("id")
            _report(f"Jira project {key}", True, f"id={project_id}, name={proj.get('name')!r}")
        except APIError as e:
            _report(f"Jira project {key}", False, str(e)[:160])

        # A test issue → steps
        issue_id = None
        try:
            first_page = next(jira.search_test_issues(key, issue_type=issue_type, page_size=1), [])
            issue = _record_shape("jira.test_issue", first_page)
            if isinstance(issue, dict):
                issue_id = issue.get("id")
        except Exception as e:
            _warn("jira test issue sample failed", str(e)[:160])
        if issue_id is not None:
            try:
                _record_shape("zapi.teststeps", client.get_test_steps(issue_id, project_id))
            except Exception as e:
                _warn("teststep failed", str(e)[:160])

        # Versions (from Jira) → cycles → executions → step results → attachments
        version_ids = []
        try:
            version_ids = list(jira.get_version_names(key).keys())
        except Exception as e:
            _warn("jira versions lookup failed", str(e)[:160])
        version_ids = version_ids + ["-1"]  # -1 = Unscheduled (much data lives here)

        first_cycle = None
        used_version = None
        if project_id is not None:
            for vid in version_ids:
                try:
                    cycles = client.get_cycles(project_id, vid)
                except Exception as e:
                    _warn(f"cycles/search (version {vid}) failed", str(e)[:120])
                    continue
                if cycles:
                    _record_shape("zapi.cycles", cycles)
                    first_cycle = cycles[0]
                    used_version = vid
                    break
            if first_cycle is None:
                _warn("No cycles found", f"across versions {version_ids} for {key}")

        first_exec = None
        if first_cycle and project_id is not None:
            cycle_id = _first(first_cycle, "id", "cycleId")
            try:
                for pg in client.get_executions_by_cycle(cycle_id, project_id, used_version):
                    first_exec = _record_shape("zapi.executions_by_cycle", pg)
                    break
            except Exception as e:
                _warn("executions/search/cycle failed", str(e)[:160])

        if isinstance(first_exec, dict):
            ex = first_exec.get("execution", first_exec)
            execution_id = _first(ex, "id", "executionId")
            ex_issue_id = _first(ex, "issueId", "issueID") or issue_id
            if execution_id is not None:
                try:
                    _record_shape("zapi.step_results", client.get_step_results(execution_id))
                except Exception as e:
                    _warn("stepresult/search failed", str(e)[:160])
                try:
                    _record_shape("zapi.attachments_execution",
                                  client.get_execution_attachments(project_id, ex_issue_id, execution_id))
                except Exception as e:
                    _warn("attachment/search/execution failed", str(e)[:160])

    # ---------------- Qase ----------------
    print("\n— Qase —")
    qase_host = str(config.get("qase.host") or "qase.io")
    scheme = "https" if config.get("qase.ssl") in (None, True) else "http"
    delimiter = "-" if config.get("qase.enterprise") else "."
    try:
        resp = requests.get(f"{scheme}://api{delimiter}{qase_host}/v1/project",
                            headers={"Token": str(config.get("qase.api_token"))},
                            params={"limit": 1}, timeout=(15, 30))
        if resp.status_code == 200 and (resp.json() or {}).get("status"):
            total = ((resp.json().get("result") or {}).get("total")) or 0
            _report("Qase auth · GET /v1/project", True, f"{total} project(s) in workspace")
        else:
            _report("Qase auth · GET /v1/project", False, f"HTTP {resp.status_code}: {resp.text[:200]}")
    except requests.exceptions.RequestException as e:
        _report("Qase auth · GET /v1/project", False, str(e)[:200])

    # ---------------- Persist shapes ----------------
    if _shapes:
        try:
            with open(_SHAPES_FILE, "w") as f:
                json.dump(_shapes, f, indent=2, default=str)
            print(f"\n  📄 Wrote sampled shapes to {_SHAPES_FILE} (gitignored)")
        except OSError as e:
            _warn("could not write shapes file", str(e)[:160])

    return _finish()


def _finish():
    failed = _results.count(False)
    print()
    if failed:
        print(f"❌ Preflight FAILED — {failed} check(s) failed.")
        sys.exit(1)
    print("✅ Preflight passed (read-only).")
    sys.exit(0)


if __name__ == "__main__":
    main()
