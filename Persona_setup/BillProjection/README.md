# Bill Projection email setup

Triggers the **Bill Projection** (`BILL_PROJECTION`) email for one or more
existing users. Give it a UUID — typically one created by
[`cdg_user_setup/create_users.py`](../../cdg_user_setup/README.md) — and it
makes the projection email send.

`setup` runs these steps per user:

1. **User record** — reads the user and confirms it's on the configured pilot.
2. **Projection check** — reads the user's live projection and prints it, since
   a missing or zero projection is the usual reason this email doesn't render.
3. **Subscription** — writes `event_subscriptions.BILL_PROJECTION.<FUEL>` and
   the `.OPT_OUT` variant with `delivery_modes = ["Email"]`.
4. **Sent-count reset** — zeroes the sent count so the email fires again.
5. **Aggregation + publish + verify** — reruns aggregation, publishes the
   event, then polls until the notification records a send.

This script does **not** set the user's rate plan — run
[`../Standard/`](../Standard/README.md) or [`../TOU/`](../TOU/README.md) first
if the user isn't already on the right plan.

## Variants

| `VARIANT` | Expects | Golden dataset row |
|---|---|---|
| `STANDARD` | any user | 8 — Bill Projection |
| `TOU_PROMOTION` | a TOU-promotion user | 9 — TOU Promotion Elements |
| `TOU_COACHING` | a TOU-coaching user | 10 — TOU Coaching Elements |

Row 21 (Bill Projection – Gas User) is `STANDARD` with `"FUEL": "GAS"`.

## The projection prerequisite

The projection is computed from usage in the **current** billing cycle, so a
user with no mid-cycle consumption has nothing to project and the email won't
render. That data comes from the CDG port — this script ingests nothing. The
projection is read from
`/2.1/users/<uuid>/homes/<home>/billprojections` and reported as, e.g.:

```
projection: 216.60 so far 48.68
  days left in cycle: 23
```

If it prints `none reported yet`, the email is unlikely to send.

## Setup

Configuration lives in the single shared
[`../config.json`](../config.json.example) — there is no per-folder config
file. Add each user under `USERS` with `"scripts": ["BillProjection"]`:

```json
{ "UUID": "…", "label": "BP - TOU coaching", "scripts": ["BillProjection"], "VARIANT": "TOU_COACHING" }
```

Point `BASE_URL` and `PILOT_ID` at a new pilot and this script works there
unchanged.

### Settings

| Key | Default | Used for |
|-----|---------|----------|
| `VARIANT` | `STANDARD` | See the table above. Settable per user. |
| `FUEL` | `ELECTRIC` | `ELECTRIC` or `GAS`. |
| `SKIP_AGGREGATION` | `false` | Skip the aggregation rerun. |
| `STATUS_TIMEOUT` | `180` | Seconds to wait for `sentCount >= 1`. |

## Running

```bash
uv run python setup_bill_projection_email.py
uv run python setup_bill_projection_email.py --uuid <uuid> --variant TOU_COACHING
```

Or via the runner: `uv run python ../run_personas.py BillProjection`

Expected subject: `You've spent $N on electricity so far. See how much you're
expected to pay!`

## Requirements

- The `aws` CLI on `PATH`, with credentials that can publish to the pilot's
  notification queue.
- Network access to `BASE_URL`.
