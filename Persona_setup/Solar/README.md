# Solar persona setup

Turns an existing user on pilot **88001** (MasterPilot ProductQA) into a
Solar-family persona (Solar, Solar + TOU, Solar + EV, Solar + EV + TOU), by
writing exactly the configs the
[persona config sheet](https://docs.google.com/spreadsheets/d/1cEhxZ0GgOojPX0l5YaUanKAHY6Rm4n7dHQDzW2bkF3c/edit?gid=1045877645#gid=1045877645)
(`configs` tab) and `persona_config_matrix.md` say these personas need. It
never writes pilot-level configuration - every override it creates is scoped
to the single user configured.

Solar true-up (kWh vs $) personas are a separate tool - see
[`../SolarTrueUp/`](../SolarTrueUp/README.md).

`setup` runs these steps in order:

1. **User record** - reads the user's pilot (and confirms it's 88001) and
   current rate plan.
2. **Ingestion contract** - reads the pilot's launchpad ingestion config,
   including the `SOLAR_USER`/`TRUE_UP_DATE` field positions in
   `user_creation_launchpad`.
3. **Persona config** - writes `email_moderation.monthly_summary_email_variant = MS_SOLAR`.
4. **Enrolment file** - clones the user's newest `USERENROLL` row, sets the
   rate plan (622-SOLAR) plus the `solar_user`/`true_up_date` fields, and
   uploads it to S3. This always runs (even if the user is already on the
   solar plan) so the solar/true-up fields still get sent.
5. **Rate transition** - waits for the new plan to land on the user's rate
   schedule (skipped if the user was already on the target plan).

The token is never printed. AWS credentials/permissions are resolved by the
AWS CLI (`aws s3api`/`aws s3`).

## Variants

All four variants use rate plan **21 (622-SOLAR)**.

| `VARIANT` | Persona |
|---|---|
| `PLAIN` | Solar |
| `TOU` | Solar + TOU |
| `EV` | Solar + EV |
| `EV_TOU` | Solar + EV + TOU |

Per `persona_config_matrix.md`, these four personas share the same
config/UE-file combination (`solar_user=true`, `true_up_date`, `MS_SOLAR`,
plan 21) - `VARIANT` mainly changes labelling and which "not covered" notes
get printed.

## Setup

Configuration lives in the single shared [`../config.json`](../config.json.example)
— there is no per-folder config file. Add the user under `USERS` with
`"scripts": ["Solar"]`, plus any per-script setting (e.g. `VARIANT`).

Nothing about the pilot is hardcoded: point `BASE_URL` and `PILOT_ID` at a new
pilot and this script works there unchanged.

These may be set on the user entry, under `scripts.Solar`, or left at the
default:

### Per-script settings

| Key | Default | Used for |
|-----|---------|----------|
| `VARIANT` | `PLAIN` | `PLAIN`, `TOU`, `EV`, or `EV_TOU` (see table above). |
| `HOME_ORDINAL` | `1` | The home this runs against. |
| `EFFECTIVE_DATE` | first of the current month | Rate plan effective date, in the pilot's own date format. |
| `TRUE_UP_DATE` | today, in the pilot's true-up date format (default `yyyy-MM`) | Value written to the `true_up_date` UE-file field. |
| `SCAN_LIMIT` | `40` | How many of the newest enrolment files to scan for the user's row. |
| `TRANSITION_TIMEOUT` | `300` | Seconds to wait for the rate transition to land. |
| `POLL_INTERVAL` | `10` | Poll interval (seconds) for the transition wait. |
| `OUTPUT_DIR` | `./output` | Where the generated enrolment file is saved locally. |

## Running

From this folder, after `uv sync` at the repo root (see the
[root README](../../README.md)):

```bash
uv run python setup_solar_persona.py
```

### Outputs

Written to `output/` (git-ignored):

| File | Purpose |
|------|---------|
| `<ENROLL_PREFIX>_D_<timestamp>_T3.txt` | The enrolment row uploaded to S3. |

## Not covered by this script

- **The EV signal** (for `EV`/`EV_TOU`) - disagg-detected data
  (`electricvehiclesoutput`), not a config. Use a pre-ported Solar+EV persona
  UUID from the reference sheet if you need a user with a real EV signal.

## Known issues / gotchas

- **`solar_user` / `true_up_date` are unverified for an already-existing
  user.** Both are live fields in pilot 88001's `user_creation_launchpad`
  contract (`solar_user` at field position 35), sent through the *same*
  `USERENROLL` S3 file/queue `Persona_setup/TOUOnboarding` already uses for
  rate-plan changes. Whether the ingestion pipeline actually applies those
  fields to a user that already exists - versus only reading them at initial
  user creation - has not been confirmed. Check the user's dashboard or a
  rendered Monthly Summary/HER email after running to see whether solar
  actually took effect.
- **`monthly_summary_email_variant` is an unverified key name.** It is not
  currently seeded anywhere on pilot 88001, and its exact key/configType
  postdates the checkout `persona_config_matrix.md` was written against
  (PE-13839).
- **`excludeSolarUsers`** (a TOU pilot config, not written here) defaults to
  `true`, which drops Solar + TOU personas from TOU cost aggregation on some
  pilots. If the `TOU`/`EV_TOU` variant's TOU behavior looks missing, check
  whether that pilot-level flag needs flipping to `false` (out of scope for
  this user-scoped script).

## Requirements

- The `aws` CLI on `PATH`, configured with credentials that can read/write
  the pilot's ingestion bucket.
- Network access to `BASE_URL`.
