# Rate structure migration

Migrates utility rate plans from one or more **source** environments into a
single **target** environment. For each plan it fetches the flat rate structure
from the source, reshapes it into the nested `RatePlanConfigurationDTO` the
target's configuration API expects, and POSTs it.

## Setup

The real config files hold live tokens and are git-ignored. Copy the templates
and fill them in:

```bash
cp config.json.example config.json
cp sources.csv.example sources.csv
```

**`config.json`** — the single target environment every plan is pushed to:

```json
{
  "TARGET_BASE_URL": "https://api-server-masterpilot-dev.bidgely.com",
  "TARGET_TOKEN": "<target bearer token>",
  "TARGET_PILOT_ID": "10001"
}
```

**`sources.csv`** — one row per plan to migrate:

```csv
SOURCE_BASE_URL,ENV_TOKEN,SOURCE_PILOT_ID,PLAN_ID,TARGET_PLAN_ID
https://naapi2-external.bidgely.com,<source bearer token>,10057,24,
,,10057,2,22
```

| Column | Required | Notes |
|--------|----------|-------|
| `SOURCE_BASE_URL` | yes\* | Source environment base URL. |
| `ENV_TOKEN` | yes\* | Source bearer token. |
| `SOURCE_PILOT_ID` | yes | Source utility/pilot id to read from. |
| `PLAN_ID` | yes | Source plan number to read. |
| `TARGET_PLAN_ID` | no | Plan number to write on the target. Blank keeps `PLAN_ID`. |

\* `SOURCE_BASE_URL` and `ENV_TOKEN` may be left blank to reuse the value from
the row above — handy when migrating several plans from the same source
environment.

## Running

From this folder, after `uv sync` at the repo root (see the
[root README](../README.md)):

```bash
uv run python migrate_rate_structure.py
```

Exits non-zero if any row fails; each row is independent, so one failure does
not stop the others.
