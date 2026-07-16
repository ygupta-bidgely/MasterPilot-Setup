# CDG user creation

Creates QA users by running the full CDG (Customer Data Generator) porting flow
for each user: it pulls a production user's data via the DETO API, writes the
user metadata (persona, rate plan, email, billing cycle, budget-billing
duration), syncs weather, generates the enroll/raw/billing files, and reads back
the destination UUID. Constant destination settings live in `config.json`; one
row per user lives in `sources.csv`.

## Setup

The real config files hold live tokens and are git-ignored. Copy the templates
and fill them in:

```bash
cp config.json.example config.json
cp sources.csv.example sources.csv
```

**`config.json`** — the constant DETO + destination settings shared by every user:

```json
{
  "DETO_BASE_URL": "https://deto-dev-api.bidgely.com",
  "DETO_ACCESS_TOKEN": "<deto bearer token>",
  "UTILITY_NAME": "masterpilot-88001",
  "DESTINATION_PILOT_ID": "88001",
  "DESTINATION_ENVIRONMENT": "dev",
  "FILE_UPLOAD_BUCKET": "bidgely-masterpilot-resi-fl-dev",
  "SOURCE_COUNTRY": "US",
  "TIMEOUT": 60
}
```

| Key | Required | Notes |
|-----|----------|-------|
| `DETO_BASE_URL` | yes | DETO **API** base URL (not the console UI). |
| `DETO_ACCESS_TOKEN` | yes | DETO bearer token. A leading `Bearer ` is tolerated. |
| `UTILITY_NAME` | yes | Destination utility name. |
| `DESTINATION_PILOT_ID` | yes | Pilot the users are created in. |
| `DESTINATION_ENVIRONMENT` | yes | Destination env, e.g. `dev` / `uat` / `productqa`. |
| `FILE_UPLOAD_BUCKET` | yes | S3 bucket for generated files. |
| `SOURCE_COUNTRY` | no | Country for weather sync (default `US`). |
| `TIMEOUT` | no | Per-request timeout in seconds (default `60`). |

**`sources.csv`** — one row per user to create:

```csv
PERSONA,PERSONA_DESCRIPTION,SOURCE_UUID,SOURCE_ENVIRONMENT,RATE_PLAN,PRIMARY_EMAIL,BILLING_CYCLE,METER_FUEL,BB_DURATION
High usage EV owner,Large home with an EV on a TOU plan,1e893a83-...,NA,24,,CDG_01,AMI-ELECTRIC,3
Dual fuel family,Gas heating + electric,2f904b94-...,NA,,persona.dualfuel@bidgely.com,,AMI-ELECTRIC|AMI-GAS,0
```

| Column | Required | Notes |
|--------|----------|-------|
| `PERSONA` | no | Stored as the user's persona name. |
| `PERSONA_DESCRIPTION` | no | Stored as the user's persona description. |
| `SOURCE_UUID` | yes | Production user UUID to port. Blank rows are skipped. |
| `SOURCE_ENVIRONMENT` | no | **Source** prod region the UUID is pulled from (`NA`, `EU`, …). Default `NA`. |
| `RATE_PLAN` | no | Rate plan number. Blank uses the plan on the prod user's meter. |
| `PRIMARY_EMAIL` | no | Blank auto-generates `bidgelyqa_<uuid>@bidgely.com`. |
| `BILLING_CYCLE` | no | Blank defaults to `CDG_01`. |
| `METER_FUEL` | no | Fuel(s), default `AMI-ELECTRIC`. For dual/multi-fuel separate with `\|` or `;` (e.g. `AMI-ELECTRIC\|AMI-GAS`). |
| `BB_DURATION` | no | Budget-billing duration in **months, just the number** (e.g. `0`, `3`, `12`). Sent as `"<n> Month"`. Blank defaults to `0`. |

## Running

From this folder, after `uv sync` at the repo root (see the
[root README](../README.md)):

```bash
uv run python create_users.py                 # port every row
uv run python create_users.py --limit 1       # just the first user (single test run)
uv run python create_users.py --delete-existing   # delete + recreate for clean re-runs
uv run python create_users.py --skip-weather --skip-cluster
```

Each row runs the 9-step CDG flow independently; a per-user summary and the
destination UUIDs are written to `create_users_results.json`. Exits non-zero if
any user fails.

By default no existing user is deleted (`--delete-existing` is off). Re-running
for a `SOURCE_UUID` that is already ported re-POSTs its metadata; use
`--delete-existing` for clean, idempotent re-runs (it deletes only the matching
`source_uuid` in this pilot, then re-ports).
