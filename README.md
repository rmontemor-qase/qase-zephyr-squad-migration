# Zephyr Squad → Qase Migration

Migrates test data from **classic Zephyr Squad for Jira Cloud** (the ZAPI v1.0
product, where test cases are Jira issues) into [Qase](https://qase.io).

## Which Zephyr is this for?

- ✅ **Classic Zephyr Squad Cloud (ZAPI v1.0)** — a *test case is a Jira issue*
  (issue type `Test`); steps, cycles, and executions live only in Zephyr's own
  backend. Auth is a per-request **JWT** signed with an access/secret key pair.
  API base: `https://prod-api.zephyr4jiracloud.com/connect`.
- ❌ **Zephyr Squad "new experience" / v2 API** (`…/v2`, Bearer access token,
  test cases as native Zephyr entities with folders) — different API surface and
  data model. Not covered here.
- ❌ **Zephyr Scale** — use `qase-zephyr-scale-migration`.
- ❌ **Zephyr Essential** — use `qase-zephyr-essential-migration`.
- ❌ **Zephyr Squad DC / Server** — different (self-hosted ZAPI) surface.

**Not sure which you have?** If your test cases appear in Jira as issues of type
`Test` (they have Jira keys like `PROJ-123`, a reporter, components, labels), and
Jira → Apps → Zephyr Squad offers an **API key** screen with an access key and
secret key, this is the right script.

### Why Jira is a first-class source here

Because a classic Squad test *is* a Jira issue, the migration reads from **two**
APIs and needs credentials for both:

| Data | Source |
|---|---|
| Test cases: summary, description, priority, labels, components, issue links, reporter/assignee, attachments | **Jira REST** (JQL search + issue reads) |
| Test steps, test cycles, executions, execution statuses | **ZAPI v1.0** (JWT) |

## What is migrated

| Zephyr Squad / Jira | Qase |
|---|---|
| Jira project | Project (created, or reused via `projects.mapping`) |
| Jira components | Suites — one per component, **or** a single suite (`features.single_suite`) |
| Jira issue (type `Test`) | Test case — title, description, priority, status |
| Jira labels (+ component names when no component field) | Case tags (max 10) |
| ZAPI test steps (`step` / `data` / `result`) | Case steps (action / data / expected result) |
| Jira components | Optional per-project **multiselect custom field** (`features.component_custom_field`) — keeps *all* components of a test |
| Jira issue links (excluding test→test) | **"Jira Links"** text custom field (`relationship KEY — url`) |
| Jira issue attachments | Real file attachments on the case (opt-in — see [Attachments](#attachments)) |
| Jira reporter (else assignee) | Case author — matched onto an **existing** Qase user, else `users.default` |
| Jira key suffix (`PROJ-123`) | Case id `123` (`cases.preserve_ids`) |
| Zephyr test cycle | Test run (one run per cycle, named after it) |
| Execution (status, executed-on, executed-by) | Run result — status mapped, dated, authored |

**Not migrated:** milestones/test plans, environments, step-level results
(`stepresult/search` is unavailable on classic Squad), Zephyr-internal
attachments on steps/executions, dashboards and saved filters. Execution
*comments* are carried into the result comment **when the tenant's ZAPI exposes
them** — several classic tenants return no comment field at all, in which case
there is simply nothing to migrate.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json   # then fill in credentials
```

### Credentials

Both sides are required — see [CREDENTIALS.md](CREDENTIALS.md) for a field-by-field guide.

1. **Zephyr ZAPI key pair** — in Jira: **Apps → Zephyr Squad → Zephyr Squad API
   Key → Generate new key** → `zephyr.access_key` + `zephyr.secret_key`.
   ⚠️ The secret is usually shown **once**, and regenerating can revoke the
   existing key on that account — confirm with the customer first.
2. **Atlassian account id** — `GET /rest/api/3/myself` → `accountId`
   (`5b10ac…` or `557058:…`) → `zephyr.account_id`. The JWT is signed *for this
   account*, so it must be the account the key pair belongs to.
3. **Jira API token** — [id.atlassian.com → Security → API tokens](https://id.atlassian.com/manage-profile/security/api-tokens)
   → `jira.base_url` / `jira.email` / `jira.api_token`. Read-only use.
4. **Qase API token** — Qase → API tokens → `qase.api_token`.
5. **`projects.import`** — the Jira project keys to migrate.

> `projects.import_all` is **not supported** for classic Squad Cloud: ZAPI v1.0
> has no project-list endpoint, so the keys must be listed explicitly.

## Validate, then run

```bash
python preflight.py          # read-only: validates config, JWT auth, and samples shapes
python start.py --dry-run    # full read-only rehearsal: reads everything, writes NOTHING to Qase
python start.py              # the real migration
```

`--dry-run` runs the whole pipeline against the source, so unmapped statuses,
missing steps, and field problems all surface in the migration report, while
every Qase write is logged as `[DRY-RUN] would …`.

Logs land in `logs/`, stats in `stats/` (JSON + XLSX). Each run ends with a
**migration report** listing every skipped or degraded item and why.
`config.json`, `logs/`, and `stats/` are gitignored — never commit them.

## Config reference

### Scope

| Key | Meaning | Default |
|---|---|---|
| `projects.import` | Jira project keys to migrate (**required**) | `[]` |
| `projects.exclude` | Keys to subtract from the list | `[]` |
| `projects.mapping` | `{ "JIRA_KEY": "QASE_CODE" }` — target an existing/pre-created Qase project. Created if missing | `{}` |
| `cases.jira_issue_type` | Jira issue type holding test cases | `Test` |
| `cases.jira_statuses` | Only migrate tests in these Jira **statuses**, e.g. `["Testing"]`. Empty = every status. Matched on the canonical (English) status name | `[]` |
| `cases.limit` | Cap the number of cases (validation runs). `0` = no limit | `0` |

### Mapping and shaping

| Key | Meaning | Default |
|---|---|---|
| `cases.preserve_ids` | Send the Jira key suffix as the Qase case id (`PROJ-123` → case `123`). **Only reliable into a brand-new project** — Qase never reuses an id that has been consumed, so a project that previously held (even deleted) cases can shift ids | `true` |
| `cases.priority_map` | Extra Jira priority → Qase slug (`high`/`medium`/`low`) | `{}` |
| `runs.status_map` | Zephyr execution status id → Qase result slug | `{}` |
| `features.single_suite` | File **every** case under one suite instead of one suite per component | `false` |
| `features.single_suite_title` | Name of that suite | `Migrated from Zephyr` |
| `features.component_custom_field` | Create a **per-project multiselect** custom field whose options are that project's Jira components, and set all of a test's components on it | `false` |
| `features.component_field_title` | Title of that field | `Feature Area` |
| `features.component_field_fallback_option` | Option used for tests with no component | `No Feature Area` |
| `features.required_field_defaults` | `{ "Field Title": "Value" }` — fixed value stamped on every case to satisfy **required** Qase custom fields the source has no equivalent for. Resolved per project and only for fields actually in scope for that project | `{}` |

### Users

| Key | Meaning | Default |
|---|---|---|
| `users.default` | Qase user id used when nothing matches | `1` |
| `users.map` | Atlassian account id → Qase user id (highest priority) | `{}` |
| `users.resolve_from_jira` | Match source users onto **existing** Qase users by email, else display name. **Never creates users** | `true` |

> Jira Cloud usually hides `emailAddress` on issue reporters, so matching often
> falls back to display name — and only succeeds for people who are already Qase
> workspace members. Everyone else lands on `users.default`. Adding the testers
> to Qase *before* migrating measurably improves authorship retention.

### Rate limits and throughput

| Key | Meaning | Default |
|---|---|---|
| `zephyr.max_requests_per_min` | Global ZAPI pacer. ZAPI is strictly rate-limited (documented ~1000 req/hr) and behaves like a token bucket; ~25/min is a safe sustained rate | `25` |
| `qase.throttle_requests` / `qase.throttle_interval_sec` | Qase request budget per interval | `120` / `12` |
| `qase.rate_limit_max_retries` | Retries on a Qase `429`, with backoff honouring `Retry-After` | `8` |
| `runs.concurrency` | Parallel run creations (each cycle costs ~3 Qase calls) | `6` |
| `cases.batch_size` | Cases per fetch+create batch, so cases land progressively | `50` |

### Behavior

| Key | Meaning | Default |
|---|---|---|
| `zephyr.attachments.enabled` | Migrate **Jira issue attachments** onto the case (see [Attachments](#attachments)) | `false` |
| `runs.complete` | Mark migrated runs complete | `true` |
| `migration.delta` | Add-only top-up of a previous migration — see [Delta migrations](#delta-migrations) | `false` |
| `migration.allow_existing_target` | Import into a non-empty Qase project (duplicates likely) | `false` |
| `migration.qase_bulk_hydrate_case_ids` | Look up each case's real id after bulk create. Only needed when `preserve_ids` is off; matches by title, so duplicate titles are ambiguous | `false` |
| `prefix` / `debug` | Output filename prefix / verbose logging | `zephyr-squad` / `false` |

## Attachments

Because a classic Squad test case **is a Jira issue**, its attachments are
ordinary **Jira issue attachments** — so they download with the plain Jira API
token already configured, through the documented REST API. No browser session, no
CDN replay, nothing outside the sanctioned surface.

Set `zephyr.attachments.enabled: true` and each case's Jira attachments are
downloaded and uploaded to Qase as real file attachments on the case. It is
opt-in only because it adds a download+upload per file, which lengthens the run.

Not covered: **Zephyr-internal** attachments on test *steps*, *executions*, or
*cycles*. Those are not Jira attachments, and ZAPI v1.0 does not serve their
binaries to a JWT client. If a tenant needs them, treat it as out of scope unless
the customer's own security team proposes and approves an approach.

## Safety guarantees

### Source (Jira / Zephyr) is strictly read-only

- Every request to a source host is a `GET`. There is no `POST`/`PUT`/`PATCH`/
  `DELETE` aimed at Jira or Zephyr anywhere in the code.
- The ZAPI client exposes read endpoints only (cycles, executions, test steps,
  statuses, attachment metadata) — it has no write methods to call by accident.
- The Jira client's surface is a JQL search plus project/component/version/user/
  attachment reads.

### Target (Qase) — no pollution of pre-existing data

- **The script never deletes anything in Qase.** Worst case is additive.
- **Target guard**: a project already containing cases, suites, runs, or
  milestones is skipped (with a report entry) unless
  `migration.allow_existing_target: true`.
- **Custom fields are project-scoped**: fields the script creates ("Jira Links",
  the component field) are enabled only for the target project codes. When a
  same-titled field already exists it *appends* the target to its scope, never
  shrinks it and never flips it workspace-global. Updating a select field always
  resends its full option list and existing flags, so options and scope survive.
- A required field is only ever written when it is genuinely **in scope for that
  project** — sending an out-of-scope field makes Qase reject the whole case.

## Behavior notes

- **Statuses**: PASS → passed, FAIL → failed, BLOCKED → blocked, SKIPPED →
  skipped, WIP → in progress, UNEXECUTED → untested. Results mapping to
  *untested* or *in progress* are omitted, leaving the case Untested in the run
  rather than inventing a result. Custom statuses map via `runs.status_map`.
- **Priorities**: Qase offers exactly `high`/`medium`/`low`. Highest/Urgent/High
  → high, Medium/Normal → medium, Low/Lowest → low; anything else → medium with
  a warning (override via `cases.priority_map`).
- **Multi-component tests**: a Qase case lives in exactly one suite, so
  suite-per-component can only represent one component (the first match wins).
  Pick the mode that fits the customer's data — see
  [Choosing how to organise cases](#choosing-how-to-organise-cases).
- **Jira issue links**: written to a "Jira Links" text custom field as
  `relationship KEY — url`. Links whose target is another `Test` issue are
  dropped on purpose: they point at test cases that are themselves being
  migrated (and abandoned in Zephyr), not at requirements or defects.
- **Runs are cycle-driven but fetched per case**: executions are read per test
  issue and grouped by `cycleId`, so nothing can be created until the full fetch
  completes. On a large project this fetch is the dominant cost — it is bounded
  by the ZAPI rate limit, not by Qase.
- **Cycles with no execution timestamps** produce runs dated at the Unix epoch
  (1970) because the source carries no date. They are migrated as-is rather than
  silently dropped.
- Errors and warnings always print to the console, not just the log file.
- Deterministic 4xx responses fast-fail; `408`/`429`/`5xx`/network errors retry.

## Choosing how to organise cases

Three supported shapes. Start with the default; the others exist for data that
does not fit it.

**1. Suite per component (default — no config needed).**
Each Jira component becomes a suite; components are also written as case tags.
The natural fit when tests carry a single component, and the closest thing to a
folder tree that classic Squad's flat model allows.

**2. Suite per component + component field** (`features.component_custom_field: true`).
Keeps the suite tree *and* records **every** component of each test in a
per-project multiselect field. Choose this when a meaningful share of tests carry
more than one component: the suite still gives structure, and nothing is lost to
the one-case-one-folder limit. Components move from tags to the field, which
keeps Qase's 10-tag cap free for real labels.

**3. Single suite + component field**
(`features.single_suite: true` + `features.component_custom_field: true`).
Everything lands in one suite and organisation happens entirely through filtering
on the component field. Choose this when *most* tests are multi-component (so any
folder choice would be arbitrary), or when the customer explicitly prefers a flat
repository they navigate by filter rather than by tree.

To decide, check the source first — the share of tests with more than one
component is the deciding number, and it varies a lot between projects.

## Delta migrations

`migration.delta: true` turns a re-run into an **add-only top-up** — the standard
cutover pattern: trial migration now, final delta at switch-over. Per project the
target is read first, then only what's missing is created:

- **Cases** match by preserved id (`PROJ-123` ↔ case `123`) — hence the
  `cases.preserve_ids` requirement, on the original run too. Skipped cases stay
  registered in the mapping, so new cycles referencing old cases resolve.
- **Suites** match by title + parent; **runs** by title.
- Nothing is ever updated or deleted.

Honest limitations (source-API constraints, not shortcuts):

- **Edits to already-migrated cases are not re-synced** — ZAPI exposes no
  modified date, so change detection is impossible. Freeze case editing during
  the migration window.
- **Executions added to an already-migrated cycle are not re-synced** — the run
  is matched by title and skipped whole. New cycles come over completely.
- Suite/run matching assumes the Qase side wasn't renamed after the first run.

## Re-run behavior

Projects dedup by title; custom fields dedup by title+scope; **suites, cases,
runs, and results are create-only** — a full re-run duplicates them. The target
guard therefore refuses a non-empty project by default. For iteration, delete and
recreate the target project between runs; note that a **fresh** project is also
what `cases.preserve_ids` needs to reproduce Jira ids exactly.

## How the API surface was verified

Classic ZAPI v1.0 (`/connect/public/rest/api/1.0`) endpoint paths, JWT claim
structure (`iss`/`sub`/`qsh`/`iat`/`exp` with the `zapiAccessKey` header), and
entity shapes were confirmed against a live Squad tenant (2026-07), including the
`/executions` page-size cap of 50 and the absence of `stepresult/search`.
