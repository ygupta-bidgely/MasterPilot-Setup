# Monthly Summary email setup

Triggers the **Monthly Summary** (`MONTHLY_SUMMARY`) email for one or more
existing users. Give it a UUID — typically one created by
[`cdg_user_setup/create_users.py`](../../cdg_user_setup/README.md) — and it
makes the monthly summary send.

`setup` runs these steps per user:

1. **User record** — reads the user and confirms it's on the configured pilot.
2. **Variant config** — writes `email_moderation.monthly_summary_email_variant`,
   plus the demand-charge display for the demand-coaching variant.
3. **Subscription** — writes `event_subscriptions.MONTHLY_SUMMARY.<FUEL>` and
   the `.OPT_OUT` variant with `delivery_modes = ["Email"]`.
4. **Sent-count reset** — zeroes the sent count so the email fires again.
5. **Aggregation + publish + verify** — reruns aggregation, publishes the
   event, then polls until the notification records a send.

This script does **not** set the user's rate plan — that's the persona
scripts' job. Run [`../Standard/`](../Standard/README.md),
[`../TOU/`](../TOU/README.md), [`../Solar/`](../Solar/README.md) or
[`../BudgetBilling/`](../BudgetBilling/README.md) first if the user isn't
already on the right plan.

## Variants

| `VARIANT` | Sets | Golden dataset row |
|---|---|---|
| `STANDARD` | `MS_STANDARD` | 2 — Monthly Summary |
| `SOLAR` | `MS_SOLAR` | 3 — Solar Elements |
| `TOU_PROMOTION` | `MS_TOU_PROMOTION` | 4 — TOU Promotion Elements |
| `TOU_COACHING` | `MS_TOU_COACHING` | 5 — TOU Coaching Elements |
| `BUDGET_BILLING` | `MS_BB` | 6 — Budget Billing Elements |
| `DEMAND_COACHING` | `MS_STANDARD` + `demand_charges_config` | 7 — Demand Coaching Elements |

**Demand Coaching is not a separate notification.** It is `MONTHLY_SUMMARY`
with the user's demand-charge display enabled, which is why it lives here as a
variant rather than in its own folder.

## Setup

Configuration lives in the single shared
[`../config.json`](../config.json.example) — there is no per-folder config
file. Add each user under `USERS` with `"scripts": ["MonthlySummary"]`:

```json
{ "UUID": "…", "label": "MS - solar", "scripts": ["MonthlySummary"], "VARIANT": "SOLAR" }
```

Point `BASE_URL` and `PILOT_ID` at a new pilot and this script works there
unchanged.

### Settings

| Key | Default | Used for |
|-----|---------|----------|
| `VARIANT` | `STANDARD` | See the table above. Settable per user. |
| `FUEL` | `ELECTRIC` | `ELECTRIC` or `GAS` — use `GAS` for row 22 (Monthly Summary – Gas User). |
| `SKIP_AGGREGATION` | `false` | Skip the aggregation rerun. |
| `STATUS_TIMEOUT` | `180` | Seconds to wait for `sentCount >= 1`. |

## Running

```bash
uv run python setup_monthly_summary_email.py
uv run python setup_monthly_summary_email.py --uuid <uuid> --variant SOLAR
```

Or via the runner: `uv run python ../run_personas.py MonthlySummary`

Expected subject (electric): `You spent $N on electricity in the last billing
cycle`; for gas, `… on gas in the last billing cycle`.

## Known caveats

- **`monthly_summary_email_variant` is an unverified key name**, the same
  caveat as the other persona scripts in this repo.
- Variants other than `STANDARD` expect the user to already *be* that persona;
  this script sets the email variant, not the underlying rate plan or data.

## Requirements

- The `aws` CLI on `PATH`, with credentials that can publish to the pilot's
  notification queue.
- Network access to `BASE_URL`.
