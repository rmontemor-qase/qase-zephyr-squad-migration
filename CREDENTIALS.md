# Credentials to populate — `config.json`

Fill in every `REPLACE_ME__…` value in **`config.json`** (this repo's real config,
already gitignored). **Do not** put secrets in `config.example.json` or paste them
into chat. If a value has a live integration depending on it, coordinate with the customer
before generating/rotating.

## Test incrementally — two phases
- **Phase 1 (now):** only **Zephyr** (`account_id`/`access_key`/`secret_key`) + **Qase**
  (`api_token`) are required. `python preflight.py config.json` validates the ZAPI JWT
  auth and samples cycles/executions/steps/statuses. Test *steps* are sampled via the
  `issueId` carried on executions — **no Jira needed**.
- **Phase 2 (later):** add **Jira** (`base_url`/`email`/`api_token`) to enumerate *all*
  test issues (including never-executed ones) and their metadata. Leave the `jira` block
  blank for Phase 1.

## `zephyr` — Zephyr Squad ZAPI (v1.0, JWT) — the only genuinely new credentials
| Field | What it is | Where to get it |
|---|---|---|
| `account_id` | Your **Atlassian account ID** (the account the API key belongs to) | `GET https://<their-site>.atlassian.net/rest/api/3/myself` → `accountId` field. Looks like `5b10ac…` or `557058:…`. |
| `access_key` | ZAPI **Access Key** | Jira → Apps → **Zephyr Squad → Zephyr Squad API Key** → *Generate new key*. |
| `secret_key` | ZAPI **Secret Key** | Same screen, shown at generation. ⚠️ Often displayed **once** — copy it immediately. |
| `base_url` | ZAPI base (constant — leave as-is) | `https://prod-api.zephyr4jiracloud.com/connect` |

⚠️ **Before generating:** confirm with the customer. Regenerating can **revoke the existing
key** on that account and break any live integration using it.

## `jira` — read access (Phase 2 — leave blank for Phase 1)
Squad test cases are Jira issues; needed to enumerate all of them and pull metadata.
| Field | What it is | Where to get it |
|---|---|---|
| `base_url` | The customer's Jira site | e.g. `https://<their-site>.atlassian.net` |
| `email` | The account email the Jira API token belongs to | Your provisioned account on their Jira |
| `api_token` | A **Jira API token** (read-only use) | https://id.atlassian.com/manage-profile/security/api-tokens |

## `qase` — destination (YOUR side, not the customer's)
| Field | What it is | Where to get it |
|---|---|---|
| `api_token` | **Your** Qase API token (use a scratch/test workspace) | Qase → API tokens |
| `host` | Qase host (leave as-is) | `qase.io` |

## `projects`
| Field | What it is |
|---|---|
| `import` | List of Jira **project keys** to migrate (e.g. `["DP", "QA"]`) |
| `mapping` | Optional `{ "JIRA_KEY": "QASE_PROJECT_CODE" }` overrides |

---
Once populated, we run a **read-only preflight** (`python preflight.py config.json`)
against the customer's tenant — but only with explicit go-ahead (CLAUDE.md Rule 7).
