# Persona setup

Tooling for setting up user personas and triggering their notification emails
on **any** pilot. Point the suite at a pilot, list the users, pick the scripts
to run, and the emails arrive.

Nothing about a pilot is hardcoded. Every pilot-specific value — ingestion
bucket, file prefixes, delimiter, date formats, rate plans, and the
environment's notification queue — is discovered at run time from the pilot's
own configuration. Setting up a brand-new pilot means changing `BASE_URL` and
`PILOT_ID`, not editing code.

## How it fits together

```
cdg_user_setup/            creates the users (CDG/DETO port)
        |
        v
Persona_setup/
  sync_users_from_pilot.py <- fills USERS with the pilot's real destination UUIDs
  PostCdgAggregation/      <- builds their billing cycles (run before the emails)
        |
        v
  <Script>/                <- conditions a user and fires its email
  config.json              <- ONE shared config for every script
  run_personas.py          <- interactive runner: pick which scripts to run
  _common/                 <- shared code (config, pilot discovery, trigger core)
```

For a brand-new pilot the order is: port the users, `sync_users_from_pilot.py`,
`PostCdgAggregation/`, then the email scripts.

The scripts take a **UUID and make a notification fire**. They never ingest
data: the users already exist and are already ingested by `cdg_user_setup/`.

## Setup

There is a single shared config for all the scripts — no per-folder config
files. Copy the template and fill it in:

```bash
cp config.json.example config.json
```

The only settings that cannot be derived from the pilot:

| Key | Required | Notes |
|-----|----------|-------|
| `BASE_URL` | yes | The environment's api-server. Identifies *which* deployment the pilot is in. |
| `AUTH_TOKEN` | yes | Bearer token. May be supplied as the `BIDGELY_TOKEN` environment variable instead, so a live token never has to be written to disk. |
| `PILOT_ID` | yes | The pilot to work against. |
| `REGION` | no | AWS region for the notification queue (default `us-west-2`). |

Users go in `USERS`. Each entry needs a `UUID`; `scripts` limits it to those
scripts, and any per-script setting (`VARIANT`, `FUEL`, `UNIT`) can sit on the
entry:

```json
"USERS": [
  { "UUID": "…", "label": "Welcome - electric", "scripts": ["WelcomeEmail"], "FUEL": "ELECTRIC" },
  { "UUID": "…", "label": "Monthly Summary / HER", "scripts": ["HER", "Standard"] }
]
```

Settings resolve most-specific-first: the user entry, then
`scripts.<Name>.<KEY>`, then the top-level `<KEY>`, then the script's default.

`config.json` is git-ignored because it holds a live token.

## Running

```bash
uv run python run_personas.py            # interactive menu
uv run python run_personas.py --list     # show scripts + configured users
uv run python run_personas.py --all      # run every script that has users
uv run python run_personas.py WelcomeEmail HER
uv run python run_personas.py --dry-run --all
```

Every script is still a normal standalone script in its own folder and can be
run directly; `run_personas.py` only sequences them, persona configuration
before the emails that depend on it.

## The scripts

| Script | What it does |
|--------|--------------|
| [`PostCdgAggregation/`](PostCdgAggregation/README.md) | **Run this first, after a CDG port.** Marks each user modified and runs aggregations, which builds the billing cycles the cycle-derived emails are generated from. A freshly ported user has none, so those emails cannot send until this has run. |
| [`Standard/`](Standard/README.md) | Standard (flat/tier rate) persona: rate plan + `monthly_summary_email_variant`. |
| [`TOU/`](TOU/README.md) | TOU-family persona (TOU, TOU Promotion, TOU Coaching). |
| [`EV/`](EV/README.md) | EV-family persona (EV, TOU + EV). |
| [`Solar/`](Solar/README.md) | Solar-family persona (Solar, + TOU, + EV, + EV + TOU), plus the `solar_user`/`true_up_date` enrolment fields. |
| [`SolarTrueUp/`](SolarTrueUp/README.md) | The 8 Solar true-up personas (kWh vs $, crossed with plain/TOU/EV/TOU+EV), incl. `credit_balance_value_identifier`. |
| [`PoolPump/`](PoolPump/README.md) | Pool Pump persona, plus a live check of the pilot's `enable_pp` prerequisite. |
| [`BudgetBilling/`](BudgetBilling/README.md) | Budget Billing-family persona (BB, + TOU, + TOU + EV). |
| [`WelcomeEmail/`](WelcomeEmail/README.md) | Triggers the User Welcome (`USER_WELCOME`) email. `FUEL` selects electric or gas. |
| [`MonthlySummary/`](MonthlySummary/README.md) | Triggers the Monthly Summary (`MONTHLY_SUMMARY`) email. `VARIANT` selects standard / solar / TOU promotion / TOU coaching / budget billing / demand coaching. |
| [`BillProjection/`](BillProjection/README.md) | Triggers the Bill Projection (`BILL_PROJECTION`) email. `VARIANT` selects standard / TOU promotion / TOU coaching; reports the live projection first. |
| [`BudgetAlert/`](BudgetAlert/README.md) | Sets a budget the user has already crossed, then triggers the Budget Alert (`BUDGET_ALERT`) email at 75% or 100%. |
| [`BestRateEmail/`](BestRateEmail/README.md) | Prepares and triggers the Best Rate (`RATE_COMPARISON`) email: picks the highest-savings rate, configures NBI assets, publishes the event. |
| [`TOUOnboarding/`](TOUOnboarding/README.md) | Prepares and triggers the TOU Rate Onboarding email: picks a valid TOU plan, uploads the rate-change enrolment file, waits for the transition. |
| [`HER/`](HER/README.md) | HER (Home Energy Report) persona: pilot config, DB rows, interactions payload, string resources, SHC 2.0 mock pipeline. |
| [`HBA/`](HBA/README.md) | High Bill Alert. Unlike the others it **creates its own synthetic user** and is CLI-flag driven, so it does not read the shared config. |

## Shared code

[`_common/`](_common/) holds what every script needs:

| Module | What it provides |
|--------|------------------|
| `config.py` | The single shared config: `load(script_name)` and the `Config` lookup/user-selection logic. |
| `pilot_context.py` | `PilotContext` — discovers the pilot's buckets, prefixes, delimiter, date formats, rate plans, and resolves the notification queue by probing the pilot's own enrolment queue. |
| `notification_trigger.py` | `NotificationEvent` and the shared reset → aggregate → publish → poll trigger, plus email-delivery subscription. |
| `api.py` | `ApiClient`, user lookup, and user-scoped config writes. |

## Requirements

- `uv sync` at the repo root (see the [root README](../README.md)).
- The `aws` CLI on `PATH`, with credentials that can read the pilot's buckets
  and publish to its notification queue.
- Network access to `BASE_URL`.
