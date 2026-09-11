# Standard persona setup

Turns an existing user on pilot **88001** (MasterPilot ProductQA) into a
Standard (flat/tier rate) persona, by writing exactly the configs the
[persona config sheet](https://docs.google.com/spreadsheets/d/1cEhxZ0GgOojPX0l5YaUanKAHY6Rm4n7dHQDzW2bkF3c/edit?gid=1045877645#gid=1045877645)
(`configs` tab) and `persona_config_matrix.md` say this persona needs. It
never writes pilot-level configuration - every override it creates is scoped
to the single user configured.

`setup` runs these steps in order:

1. **User record** - reads the user's pilot (and confirms it's 88001) and
   current rate plan.
2. **Ingestion contract** - reads the pilot's launchpad ingestion config
   (file layout, delimiter, S3 bucket, enrol-file prefix, date format).
3. **Persona config** - writes `email_moderation.monthly_summary_email_variant = MS_STANDARD`.
4. **Enrolment file** - if the user isn't already on the target rate plan,
   clones the user's newest `USERENROLL` row, sets the new plan, and uploads
   it to S3.
5. **Rate transition** - waits for the new plan to land on the user's rate
   schedule (skipped if the user was already on the target plan).

The token is never printed. AWS credentials/permissions are resolved by the
AWS CLI (`aws s3api`/`aws s3`).

## Variants

| `VARIANT` | Persona | Rate plan # |
|---|---|---|
| `TWO_TIER` | Standard - Flat Rate (2 tiers) | 11 (`011ID`) |
| `TIER` | Standard - Tier Rate | 1 (`180`) |

**Standard - Flat Rate (3 tiers) is not offered.** Pilot 88001 has no 3-tier
rate plan - `E-12` is 4-tier, not 3-tier (see `persona_config_matrix.md`
finding #4). There is no valid plan number to assign this persona on this
pilot.

## Setup

Configuration lives in the single shared [`../config.json`](../config.json.example)
— there is no per-folder config file. Add the user under `USERS` with
`"scripts": ["Standard"]`, plus any per-script setting (e.g. `VARIANT`).

Nothing about the pilot is hardcoded: point `BASE_URL` and `PILOT_ID` at a new
pilot and this script works there unchanged.

These may be set on the user entry, under `scripts.Standard`, or left at the
default:

### Per-script settings

| Key | Default | Used for |
|-----|---------|----------|
| `VARIANT` | `TIER` | `TWO_TIER` or `TIER` (see table above). |
| `HOME_ORDINAL` | `1` | The home this runs against. |
| `EFFECTIVE_DATE` | first of the current month | Rate plan effective date, in the pilot's own date format. |
| `SCAN_LIMIT` | `40` | How many of the newest enrolment files to scan for the user's row. |
| `TRANSITION_TIMEOUT` | `300` | Seconds to wait for the rate transition to land. |
| `POLL_INTERVAL` | `10` | Poll interval (seconds) for the transition wait. |
| `OUTPUT_DIR` | `./output` | Where the generated enrolment file is saved locally. |

## Running

From this folder, after `uv sync` at the repo root (see the
[root README](../../README.md)):

```bash
uv run python setup_standard_persona.py
```

### Outputs

Written to `output/` (git-ignored):

| File | Purpose |
|------|---------|
| `<ENROLL_PREFIX>_D_<timestamp>_T3.txt` | The enrolment row uploaded to S3, when a plan change was needed. |

## Known issues / gotchas

- **`monthly_summary_email_variant` is an unverified key name.** It is not
  currently seeded anywhere on pilot 88001, and its exact key/configType
  postdates the checkout `persona_config_matrix.md` was written against
  (PE-13839). The write follows the same `POST /v2.0/configs/{type}/user/{uuid}`
  pattern every other script in this repo already uses successfully for new
  keys, but confirm the email actually renders as `MS_STANDARD` before
  relying on it.

## Requirements

- The `aws` CLI on `PATH`, configured with credentials that can read/write
  the pilot's ingestion bucket.
- Network access to `BASE_URL`.
