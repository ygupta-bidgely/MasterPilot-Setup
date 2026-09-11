# Pool Pump persona setup

Turns an existing user on pilot **88001** (MasterPilot ProductQA) into the
Pool Pump persona, by writing exactly the configs the
[persona config sheet](https://docs.google.com/spreadsheets/d/1cEhxZ0GgOojPX0l5YaUanKAHY6Rm4n7dHQDzW2bkF3c/edit?gid=1045877645#gid=1045877645)
(`configs` tab) and `persona_config_matrix.md` say this persona needs. It
never writes pilot-level configuration - every override it creates is scoped
to the single user configured.

There is only one persona in this group, so there's no `VARIANT` to set.

`setup` runs these steps in order:

1. **User record** - reads the user's pilot (and confirms it's 88001) and
   current rate plan.
2. **Ingestion contract** - reads the pilot's launchpad ingestion config
   (file layout, delimiter, S3 bucket, enrol-file prefix, date format).
3. **Persona config** - writes `email_moderation.monthly_summary_email_variant = MS_STANDARD`.
4. **Enrolment file** - if the user isn't already on rate plan 1 (180),
   clones the user's newest `USERENROLL` row, sets the new plan, and uploads
   it to S3.
5. **Rate transition** - waits for the new plan to land on the user's rate
   schedule (skipped if the user was already on the target plan).
6. **Prerequisite check** - reads (read-only) `disagg_preference.enable_pp`
   on the pilot and warns if it isn't `true`.

The token is never printed. AWS credentials/permissions are resolved by the
AWS CLI (`aws s3api`/`aws s3`).

## Setup

Configuration lives in the single shared [`../config.json`](../config.json.example)
— there is no per-folder config file. Add the user under `USERS` with
`"scripts": ["PoolPump"]`.

Nothing about the pilot is hardcoded: point `BASE_URL` and `PILOT_ID` at a new
pilot and this script works there unchanged.

These may be set on the user entry, under `scripts.PoolPump`, or left at the
default:

### Per-script settings

| Key | Default | Used for |
|-----|---------|----------|
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
uv run python setup_pool_pump_persona.py
```

### Outputs

Written to `output/` (git-ignored):

| File | Purpose |
|------|---------|
| `<ENROLL_PREFIX>_D_<timestamp>_T3.txt` | The enrolment row uploaded to S3, when a plan change was needed. |

## Not covered by this script

- **The Pool Pump signal itself.** PP presence (`poolsandsaunasoutput`) is
  disagg-detected data, not a config (see `persona_config_matrix.md` §2,
  "Three mechanisms"). There is no API or file contract to force it onto an
  arbitrary user. If you need a user with a real Pool Pump signal, use a
  pre-ported Pool Pump persona UUID from the reference sheet - it was
  created via `cdg_user_setup`, which clones a real production user that
  already has the signal.

## Known issues / gotchas

- **Pool Pump is pilot-wide disabled right now.** `disagg_preference.enable_pp`
  on pilot 88001 was `false` at the time this script was written. It won't
  matter for the config writes this script does, but no Pool Pump disagg
  will actually run for anyone on the pilot until that's flipped to `true`
  (pilot-level, so out of scope for this user-scoped script - the script
  only warns about it).
- **`monthly_summary_email_variant` is an unverified key name.** It is not
  currently seeded anywhere on pilot 88001, and its exact key/configType
  postdates the checkout `persona_config_matrix.md` was written against
  (PE-13839).

## Requirements

- The `aws` CLI on `PATH`, configured with credentials that can read/write
  the pilot's ingestion bucket.
- Network access to `BASE_URL`.
