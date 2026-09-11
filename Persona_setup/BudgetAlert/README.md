# Budget Alert email setup

Triggers the **Budget Alert** (`BUDGET_ALERT`) email for one or more existing
users, by setting a budget the user has *already* crossed and then firing the
alert. Golden dataset row 12.

`setup` runs these steps per user:

1. **User record** — reads the user and confirms it's on the configured pilot.
2. **Budget** — reads the user's live projection and writes a budget onto the
   user's home record such that the projection sits at the target percentage.
3. **Subscription** — writes `event_subscriptions.BUDGET_ALERT.<FUEL>` and the
   `.OPT_OUT` variant with `delivery_modes = ["Email"]`.
4. **Sent-count reset** — zeroes the sent count so the alert fires again.
5. **Aggregation + publish + verify** — reruns aggregation, publishes the
   event, then polls until the notification records a send.

## How the budget is derived

A budget alert only fires once projected spend crosses a percentage of the
budget, so the budget must be set *relative to the projection*:

```
budget = projection / (THRESHOLD_PERCENT / 100)
```

So for a projection of `216.60`:

| `THRESHOLD_PERCENT` | Budget written | Alert |
|---|---|---|
| `75` | `288.80` | "reached 75% of your budget" |
| `100` | `216.60` | "reached 100% of your budget" |

The budget is written to `/meta/users/<uuid>/homes/<home>` as
`budgetThresholdAmount` (plus the fuel-specific key), which is an absolute
currency amount — not a percentage. Set `BUDGET_AMOUNT` to write an exact
figure and skip the computation.

If the user has no positive projection, the script **fails with a clear
error** rather than writing a meaningless budget — the user needs usage in the
current billing cycle first.

## Setup

Configuration lives in the single shared
[`../config.json`](../config.json.example) — there is no per-folder config
file. Add each user under `USERS` with `"scripts": ["BudgetAlert"]`:

```json
{ "UUID": "…", "label": "Budget Alert 75%", "scripts": ["BudgetAlert"], "THRESHOLD_PERCENT": 75 }
```

Point `BASE_URL` and `PILOT_ID` at a new pilot and this script works there
unchanged.

### Settings

| Key | Default | Used for |
|-----|---------|----------|
| `THRESHOLD_PERCENT` | `75` | `75` or `100` — which crossing to provoke. Settable per user. |
| `BUDGET_AMOUNT` | *(derived)* | Write this exact budget instead of deriving one from the projection. |
| `FUEL` | `ELECTRIC` | `ELECTRIC` or `GAS` — selects which fuel-specific budget key is written. |
| `SKIP_AGGREGATION` | `false` | Skip the aggregation rerun. |
| `STATUS_TIMEOUT` | `180` | Seconds to wait for `sentCount >= 1`. |

## Running

```bash
uv run python setup_budget_alert_email.py
uv run python setup_budget_alert_email.py --uuid <uuid> --percent 100
```

Or via the runner: `uv run python ../run_personas.py BudgetAlert`

## Note

This **writes a budget onto the user's home record**, changing that user's
state beyond the email itself. It's scoped to the single user and no
pilot-level config is touched, but the budget stays set after the run.

## Requirements

- The `aws` CLI on `PATH`, with credentials that can publish to the pilot's
  notification queue.
- Network access to `BASE_URL`.
