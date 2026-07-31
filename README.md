# Zephyr Squad → Qase Migration

Migrates test data from **Zephyr Squad** for Jira Cloud (the post-2025 SmartBear product, formerly Zephyr Squad) into [Qase](https://qase.io).

## Which Zephyr is this for?

- ✅ **Zephyr Squad** (Jira Cloud app v10+, "Zephyr Squad — Test Management for Jira"). Its REST API (v2.9) lives at `https://prod-api.zephyr4jiracloud.com/v2` and is schema-compatible with Zephyr Scale Cloud v2 — test cases, folders, cycles, and executions are native Zephyr entities.
- ❌ **Classic Zephyr Squad Cloud (v1 / ZAPI)** — grandfathered tenants where test cases are Jira issues. Different API surface, not covered by this script.
- ❌ **Zephyr Scale** — use `qase-zephyr-scale-migration`.
- ❌ **Zephyr Squad DC / Server** — different (ZAPI-style) API, not covered.

## What is migrated

| Zephyr Squad | Qase |
|---|---|
| Project | Project |
| Test-case folders (tree) | Suites |
| Test plans | Milestones |
| Test case (name, objective, precondition, labels, component, priority, status, custom fields) | Test case (component → tag) |
| Test steps (step / test data / expected result) | Case steps |
| BDD / plain-text test script | Case description (fenced code block) |
| Test cycle | Test run |
| Execution (status, comment, actual end date, execution time) | Run result (Qase attributes API results to the token owner; case owners map via `users.map`) |
| Execution step results (per-step status + actual result) | Result steps (positional; always migrated) |
| Environments (execution-level) | Qase environments; set on the run when the whole cycle shares one, noted per-result comment when mixed |
| Call-to-test steps (step referencing another case) | Textual "→ Call to test case KEY" step |
| Test case → Jira issue links | Native external issues on the case (`jira-cloud`), or "Jira Links" custom field without the integration |
| Test case web links | Markdown links in the case description |
| Cycle → Jira issue / web links | Markdown links on the run description |
| Execution → Jira issue links | Markdown links in the result comment |

**Not migrated by default:** attachments (opt-in — see [Attachments](#attachments)), per-case estimated time (no Qase bulk field), users (attributed to `users.default` unless mapped via `users.map`), dashboards/reports/saved filters.

**Attachments (2026-07):** the Squad API exposes attachment *metadata* but **denies the binary to the API access token** (verified: `401 Permission denied`; the public metadata never includes a downloadable URL). The binaries are reachable only through Zephyr's own browser-facing channels — the app's internal backend hands out pre-signed S3 URLs for file attachments, and inline images sit on a CDN — so downloading them requires replaying a captured browser session. By default binaries are **not** migrated: filenames are recorded (`📎 Attachments in source …`) on the case description / run description / result comment, with a console warning per affected entity, so nothing disappears silently. Binary migration is an explicit opt-in with real trade-offs — see [Attachments](#attachments).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json   # then fill in tokens
```

### Credentials

1. **Zephyr access token** — in Jira: **Settings (gear) → General Settings → Apps → Zephyr API Access Tokens → Create access token** → `zephyr.access_token`.
2. **Qase API token** — Qase workspace settings → API → `qase.api_token`.
3. `projects.import` — the Jira project keys to migrate (must be Zephyr-enabled), or set `projects.import_all: true` to migrate every Zephyr-enabled project on the tenant (`projects.exclude` subtracts from either form).
4. **Jira API token (optional but recommended)** — [id.atlassian.com → Security → API tokens](https://id.atlassian.com/manage-profile/security/api-tokens) → `jira.base_url` / `jira.email` / `jira.api_token`. The Squad API returns project name, components, and fixVersions as bare Jira id links; with Jira credentials the script resolves them (proper Qase project titles, component → tag, `[Release] Cycle` run titles). Without them, titles fall back to the project key, component tags are dropped, and runs lose version context — everything else migrates identically. Only `/myself` and `/project/{key}` metadata endpoints are ever called.

## Validate, then run

```bash
python preflight.py          # validates config, pings both APIs, checks each project + data counts
python start.py --dry-run    # full read-only rehearsal: reads everything, writes NOTHING to Qase
python start.py              # the real migration
```

`--dry-run` runs the entire pipeline against the source (so unmapped statuses,
missing steps, oversize fields, and attachment problems all surface in the
migration report) while every Qase write is logged as `[DRY-RUN] would …`.

Logs land in `logs/`, stats in `stats/` (JSON + XLSX). The run ends with a
**migration report** listing every skipped or degraded item with its reason —
if it's empty, the counts tell the whole story. `config.json`, `logs/`,
`stats/` are gitignored — never commit them.

## Config reference

| Key | Meaning | Default |
|---|---|---|
| `projects.import_all` | `true` = migrate **every** Zephyr-enabled project on the tenant (`projects.import` is ignored) | `false` |
| `projects.import` | Jira project keys to migrate (**required** unless `import_all` is true) | `[]` |
| `projects.exclude` | Project keys/names to skip — applies to both `import_all` and the explicit list | `[]` |
| `projects.mapping` | Force a source key → existing Qase project code | `{}` |
| `users.default` | Qase user id unmapped entities are attributed to | `1` |
| `users.map` | Atlassian account id → Qase user id, e.g. `{"5f7b7c01…": 3}` — maps case owners to Qase case authors. Also sent for run results, but the Qase API currently attributes every API-created result to the token owner regardless (verified live 2026-07) | `{}` |
| `runs.created_after` | Skip cycles older than this epoch (uses `plannedStartDate`; undated cycles are kept) | `0` |
| `runs.status_map` | Custom execution statuses → Qase result slugs, e.g. `{"deferred": "skipped"}`; unmapped ones stay Untested + console warning | `{}` |
| `cases.priority_map` | Custom Zephyr priorities → Qase slugs (`high`/`medium`/`low`), e.g. `{"urgent": "high"}`; unmapped → medium + warning | `{}` |
| `cases.preserve_ids` | Send the Zephyr key suffix as the Qase case id (`ZEM-T123` → case `123`), preserving cross-references in existing docs/links and enabling [delta migrations](#delta-migrations). **Only safe into an empty target project** (the re-run guard enforces this by default); combined with `migration.allow_existing_target` it can collide with existing ids | `true` |
| `migration.delta` | Re-run as an **add-only top-up** of a previously migrated target: whatever already exists is skipped, only the missing pieces are created, nothing is ever updated or deleted — see [Delta migrations](#delta-migrations). Requires `cases.preserve_ids` on both runs | `false` |
| `migration.allow_existing_target` | Write into a Qase project that already contains data **without** checking what exists — duplicates are likely. Only for "I know what I'm doing" cases; for a top-up use `migration.delta` instead | `false` |
| `zephyr.base_url` | API base URL | `https://prod-api.zephyr4jiracloud.com/v2` |
| `zephyr.attachments.enabled` | **Opt-in** to browser-session binary download (see [Attachments](#attachments)). `false` = filename notes only | `false` |
| `zephyr.attachments.session_file` | Path to the captured browser session (gitignored; a full Atlassian session credential) | `zephyr_session.json` |
| `zephyr.attachments.app_url` | Jira site (fallback nav target; falls back to `jira.base_url`) | `""` |
| `zephyr.attachments.app_page_url` | Zephyr app page revisited headlessly each run to mint a fresh Forge token — **auto-recorded by `capture_session.py`** | `""` |
| `zephyr.attachments.session_max_age_days` | Warn when the captured session is older than this | `21` |
| `prefix` | Filename prefix for `logs/` and `stats/` output | `zephyr-squad` |
| `debug` | Verbose console logging | `false` |

Keys with a sane default (`qase.ssl`, `qase.request_max_retries`,
`qase.request_retry_backoff_sec`, the `zephyr.attachments.*` overrides,
`prefix`, `debug`) are omitted from `config.example.json` on purpose — set
them only if you need to change the default. Step results and environments
are always migrated (no toggle).

## Safety guarantees

### Source (Jira / Zephyr) is strictly read-only

The script **cannot delete or modify anything** in Jira or Zephyr, enforced by
construction and verified by audit (2026-07):

- Every HTTP request against a source host in the entire runtime path is a
  `GET` — there is no `POST`/`PUT`/`DELETE`/`PATCH` aimed at Jira or Zephyr
  anywhere in the code.
- The Jira client's entire surface is five read lookups (`/myself`,
  project/components/versions metadata, and a JQL id→key search).
- The opt-in headless browser only *navigates* and *reads cookies* — it never
  clicks, fills, or submits anything. Loading the app page is equivalent to a
  person opening it in a tab (the app's own scripts run, as for any visitor).

### Target (Qase) — no pollution of pre-existing data

- **The script never deletes anything in Qase.** Worst case is additive.
- **Target guard**: a project that already contains any cases, suites, runs,
  or milestones is refused (skipped with a report entry) unless
  `migration.allow_existing_target: true` is set explicitly.
- **Custom fields are scoped, not global**: fields the script creates
  (including "Jira Links") are enabled only for the target project codes —
  they do not appear in the customer's other projects. When a field with the
  same name already exists, the script *appends* the target projects to its
  scope (never shrinks it, never flips it workspace-global, and leaves
  already-global fields untouched).
- Everything else the script writes (suites, cases, runs, results,
  milestones, environments, attachments) is project-scoped to the targets.
- Importing into a project that existed before the run (title match or
  `projects.mapping`) is called out in the end-of-run migration report.

## Behavior notes

- **Statuses**: Pass → passed, Fail → failed, Blocked → blocked, In Progress / Not Executed → left untested in the run. Custom statuses: map via `runs.status_map`, else untested + console warning.
- **Priorities**: Qase's priority options are exactly `high` / `medium` / `low`. Highest/High → high, Medium/Normal → medium, Low/Lowest → low; custom priorities map via `cases.priority_map`, else medium + console warning.
- **Elapsed time**: `actualEndDate − actualStartDate` when available, else `executionTime` (ms). Squad (like Scale) does not return `actualStartDate`, so `executionTime` is the usual source.
- **Custom fields**: the Squad API has no `/customfields` endpoint — field definitions are discovered by scanning test-case payloads and typed by inference. Multi-select values arriving as `[{"id":…,"name":…}]` are serialised by name.
- **Jira issue links**: Zephyr links expose only the numeric Jira issue id. With Jira credentials configured, ids are resolved to keys (read-only JQL lookup) and attached to Qase cases as **native external issues** — this requires the Qase↔Jira Cloud integration to be connected in the workspace (Qase validates every key against it). When native attach is unavailable (no integration) or a key can't be resolved, the links are written to a **"Jira Links" text custom field** instead (created automatically at the start of the run), so they are never lost. Cycle/execution links always land as markdown notes on the run description / result comment (runs and results have no external-issue field).
- **Step-level results**: per-step statuses map like execution statuses; anything outside passed/failed/blocked becomes a skipped step (Qase's step enum). If Qase rejects a run's step payload, the results are re-sent once without steps rather than losing the whole run.
- **Call-to-test steps**: a step referencing another test case is rendered as a text step (`→ Call to test case KEY`) — Qase bulk create can't reference shared steps (native shared-step conversion is a known enhancement).
- **Environments**: mirrored to Qase by name (deduped, case-insensitive). One environment across a whole cycle → set on the Qase run; a mix → per-result `🌐 Environment:` comment note (Qase results carry no environment field).
- Errors and warnings always print to the console (not just the log file).
- All deterministic 4xx responses fast-fail; only 5xx/408/429/network errors retry.

## Attachments

The Zephyr API access token can *list* attachment metadata but is **denied the
binary** (`401 Permission denied`), and the public metadata never includes a
downloadable URL — verified exhaustively against a live tenant. The binaries are
reachable only through Zephyr's own browser-facing channels: the app's internal
backend returns pre-signed S3 URLs for file attachments, and inline rich-text
images sit on a CDN. Both require replaying a captured browser session. There are
two ways to handle this, and you should understand both before choosing.

### Default: filename notes (no setup, recommended baseline)

Out of the box, attachment binaries are **not** migrated. Each source filename is
recorded on the Qase case description / run description / result comment as
`📎 Attachments in source (not migrated — could not be downloaded): …`, with a
console warning per affected entity. Nothing is lost silently, and the migration
has no extra dependencies. For many engagements a durable record that a file
existed is sufficient — confirm with the customer.

### Consider the sanctioned alternatives first

Before enabling the browser-session path below, weigh the routes that stay within
supported channels:

- **Jira issue attachments** — if the customer's attachments live on the Jira
  issues themselves (true for grandfathered *Zephyr Squad* tenants where test
  cases are Jira issues, and for attachments on linked requirement issues), those
  can be pulled with the Jira REST attachment API using a normal token — no
  browser, no CDN, fully sanctioned. **Note:** in *Zephyr Squad* test cases
  are native Zephyr entities, so their attachments are Zephyr-internal (on the
  CDN), **not** Jira attachments — this route covers little or nothing for a pure
  Squad tenant. It is the right path for Squad-era data.
- **SmartBear support export** — ask SmartBear for a data export of the tenant
  including attachment binaries.
- **Accept the filename notes** — the baseline above.

For an SSO/MFA enterprise, where even the one-time interactive login may be
constrained, treat binary attachment migration as **out of scope** unless the
customer's own security team proposes and approves an approach on their side.
There is no token-only path: the public API serves attachment metadata but
denies the binary.

### Opt-in: capture-once browser session

If the binaries are Zephyr-internal (the Squad case) and the customer needs
them, this mode replays a real browser session to fetch them. **It is off by
default and is a deliberate, disclosed choice** — when enabled, the run prints a
one-time disclosure of exactly what it does. Understand the trade-offs:

- It accesses the CDN **outside SmartBear's sanctioned API**. Review the
  customer's Atlassian and SmartBear terms of service — automated/non-API access
  and circumventing technical restrictions may be prohibited.
- The captured session (`session_file`) authorizes the **entire Atlassian
  account**, not just Zephyr. Treat it like a password: it is gitignored; in a
  Qase-run (Premium) engagement, prefer having the customer capture it on their
  own machine or via screen-share rather than sending you the file.
- Automated Atlassian login can trip **bot detection** on locked-down/SSO tenants
  and, worst case, temporarily lock the account.

**One-time setup:**

```bash
pip install playwright
playwright install chromium
```

One config key turns it on:

```json
"zephyr": {
  "access_token": "…",
  "attachments": { "enabled": true }
}
```

- `enabled` — must be `true` to activate this path. **Capturing a session does
  not by itself turn it on.**
- The rest is automatic: the session is stored in `zephyr_session.json`
  (gitignored; a full Atlassian session credential — never commit or share
  it), and `capture_session.py` records the Zephyr app-page URL each run
  revisits to mint a fresh token. The optional overrides (`session_file`,
  `app_url`, `app_page_url`, `session_max_age_days`) are listed in the
  [config reference](#config-reference).

**Two credentials, two lifetimes.** The manual login creates an Atlassian session
that lasts ~2–4 weeks. The token the attachment backend actually needs (a Forge
app JWT) lasts only ~30–40 minutes — but the migration re-mints it automatically
each run (and mid-run for long migrations) by revisiting the recorded app page.
So the manual step is roughly **monthly**, not per run.

**Capture the session (once, repeat ~monthly):**

```bash
python capture_session.py    # opens a browser — log in, then OPEN A ZEPHYR TEST CASE, then press ENTER
python start.py              # revisits the app page to mint a token, downloads attachments
```

⚠ During capture you **must** navigate into the Zephyr app (open a project's Zephyr
section or a test case) before pressing ENTER — that page's URL is what each run
revisits to mint the token. If you stop at the Jira dashboard, file attachments
won't download.

If Playwright isn't installed, the session is missing/expired, or
`zephyr.attachments.enabled` is `false`, the migration runs normally and falls
back to the `📎` filename notes — it never crashes.

**Where each attachment lands** (when browser-session mode is on):

| Zephyr location | Qase destination |
|---|---|
| Test case (attachment tab) | real file attachment on the case |
| Test case (inline rich-text image) | rendered inline in the case description |
| Test execution / result | real attachment on the run result |
| Test cycle | embedded inline in the run description (images shown, other files linked) — a Qase run has no attachments panel, but its description is markdown |

Without a session, every one of these degrades to a `📎` filename note instead.

## Delta migrations

`migration.delta: true` turns a re-run into an **add-only top-up** — the
standard cutover pattern: trial migration now, keep working in Zephyr, final
delta at switch-over. Per project:

- A target project that doesn't exist yet (or is empty) migrates **in full** —
  delta mode never restricts scope, `projects.import` does.
- A pre-existing target is first *read*, then only what's missing is created:
  - **Cases** match by preserved id (`ZEM-T123` ↔ case `123`) — hence the
    `cases.preserve_ids` requirement, on the original run too. Skipped cases
    are still registered in the mapping, so new cycles executing old cases
    resolve correctly.
  - **Suites** match by title + parent (new cases in old folders land in the
    right suite); **milestones** by title; **runs** by final run title.
- Nothing is ever updated or deleted. The migration report lists per-project
  `delta: skipped N …` summaries.

Honest limitations (source-API constraints, not shortcuts):

- **Edits to already-migrated cases are not re-synced** — the Squad API
  exposes no modified-date, so change detection is impossible. Freeze case
  editing during the migration window (normal cutover practice).
- **Executions added to an already-migrated cycle are not re-synced** — the
  run is matched by title and skipped whole. New cycles come over completely.
- Suite/run matching assumes the Qase side wasn't renamed/restructured after
  the first migration — renamed entities would be re-created under their
  source names.

## Re-run behavior

Projects dedup by title; custom fields and environments dedup by name; **suites, cases, runs, and results are create-only** — a full re-run duplicates them. Because of this the script **refuses to import into a Qase project that already contains cases** and reports it in the migration report; set `migration.allow_existing_target: true` only when you understand you'll get duplicates. For test iterations, use a scratch Qase project between runs (or delete the target project first).

## How the API surface was verified

Official OpenAPI spec: `https://api.swaggerhub.com/apis/smartbear-public/zephyr-squad-cloud-api/2.9/swagger.json` (linked from [developer.smartbear.com/zephyr-squad](https://developer.smartbear.com/zephyr-squad/default/introduction)), cross-checked against a live Squad 10.x tenant (auth, pagination, and entity shapes confirmed 2026-07).
