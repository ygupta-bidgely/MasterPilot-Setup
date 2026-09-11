# Solar True-Up persona setup

Turns an existing user on pilot **88001** (MasterPilot ProductQA) into one of
the 8 Solar true-up personas (kWh vs $, crossed with plain/TOU/EV/TOU+EV), by
writing exactly the configs the
[persona config sheet](https://docs.google.com/spreadsheets/d/1cEhxZ0GgOojPX0l5YaUanKAHY6Rm4n7dHQDzW2bkF3c/edit?gid=1045877645#gid=1045877645)
(`configs` tab) and `persona_config_matrix.md` say these personas need. It
never writes pilot-level configuration - every override it creates is scoped
to the single user configured.

Plain Solar personas (no true-up split) are a separate tool - see
[`../Solar/`](../Solar/README.md).

`setup` runs these steps in order:

1. **User record** - reads the user's pilot (and confirms it's 88001) and
   current rate plan.
2. **Ingestion contract** - reads the pilot's launchpad ingestion config,
   including the `SOLAR_USER`/`TRUE_UP_DATE` field positions in
   `user_creation_launchpad`.
3. **Persona config** - writes `email_moderation.monthly_summary_email_variant = MS_SOLAR`
   and `meta_data.credit_balance_value_identifier` (`CONSUMPTION` for kWh,
   `COST` for $).
4. **Enrolment file** - clones the user's newest `USERENROLL` row, sets the
   rate plan (622-SOLAR) plus the `solar_user`/`true_up_date` fields, and
   uploads it to S3. This always runs (even if the user is already on the
   solar plan) so the solar/true-up fields still get sent.
5. **Rate transition** - waits for the new plan to land on the user's rate
   schedule (skipped if the user was already on the target plan).

The token is never printed. AWS credentials/permissions are resolved by the
AWS CLI (`aws s3api`/`aws s3`).

## Unit and variant

All eight personas use rate plan **21 (622-SOLAR)**.

| `UNIT` | `credit_balance_value_identifier` | Persona label |
|---|---|---|
| `KWH` | `CONSUMPTION` | Solar (True up kWh) |
| `USD` | `COST` | Solar (True up $) |

| `VARIANT` | Adds |
|---|---|
| `PLAIN` | - |
| `TOU` | + TOU |
| `EV` | + EV |
| `TOU_EV` | + TOU + EV |

E.g. `UNIT=USD, VARIANT=TOU` is "Solar ($) + TOU".

**Note on the underlying data**: per `persona_config_matrix.md`, the
CREDIT_BALANCE invoice line carries *both* a cost and a consumption field;
the config only selects which one is *displayed*. Populate the field that
matches the config, or the persona shows a zero balance - that population is
invoice data, out of scope for this script.

## Setup

Configuration lives in the single shared [`../config.json`](../config.json.example)
— there is no per-folder config file. Add the user under `USERS` with
`"scripts": ["SolarTrueUp"]`, plus any per-script setting (e.g. `UNIT` and `VARIANT`).

Nothing about the pilot is hardcoded: point `BASE_URL` and `PILOT_ID` at a new
pilot and this script works there unchanged.

These may be set on the user entry, under `scripts.SolarTrueUp`, or left at the
default:

### Per-script settings

| Key | Default | Used for |
|-----|---------|----------|
| `VARIANT` | `PLAIN` | `PLAIN`, `TOU`, `EV`, or `TOU_EV` (see table above). |
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
uv run python setup_solar_trueup_persona.py
```

### Outputs

Written to `output/` (git-ignored):

| File | Purpose |
|------|---------|
| `<ENROLL_PREFIX>_D_<timestamp>_T3.txt` | The enrolment row uploaded to S3. |

## Not covered by this script

- **The EV signal** (for `EV`/`TOU_EV`) - disagg-detected data
  (`electricvehiclesoutput`), not a config. Use a pre-ported persona UUID
  from the reference sheet if you need a user with a real EV signal.
- **Which CREDIT_BALANCE field is actually populated** - the config only
  selects display; the underlying invoice data isn't touched here.

## Known issues / gotchas

- **`solar_user` / `true_up_date` are unverified for an already-existing
  user.** Both are live fields in pilot 88001's `user_creation_launchpad`
  contract (`solar_user` at field position 35), sent through the *same*
  `USERENROLL` S3 file/queue `Persona_setup/TOUOnboarding` already uses for
  rate-plan changes. Whether the ingestion pipeline actually applies those
  fields to a user that already exists - versus only reading them at initial
  user creation - has not been confirmed.
- **`monthly_summary_email_variant` is an unverified key name**, same caveat
  as the other persona scripts in this repo (PE-13839, postdates the
  checkout `persona_config_matrix.md` was written against).
- **`credit_balance_value_identifier` is not currently seeded** on pilot
  88001 either, though the write mechanism (`ConfigManager`'s
  broad-to-specific hierarchy down to `USER`) is well established by other
  configs this repo already writes successfully.
- **`excludeSolarUsers`** (a TOU pilot config, not written here) defaults to
  `true`, which drops Solar + TOU personas from TOU cost aggregation on some
  pilots - relevant to the `TOU`/`TOU_EV` variants.

## Requirements

- The `aws` CLI on `PATH`, configured with credentials that can read/write
  the pilot's ingestion bucket.
- Network access to `BASE_URL`.
