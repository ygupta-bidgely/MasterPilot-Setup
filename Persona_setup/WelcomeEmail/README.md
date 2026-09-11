# User Welcome email setup

Triggers the **User Welcome** (`USER_WELCOME`) email for one or more existing
users. Give it a UUID — typically one created by
[`cdg_user_setup/create_users.py`](../../cdg_user_setup/README.md) — and it
makes the welcome email send.

It never writes pilot-level configuration; every override is scoped to the
users configured. It also never ingests data: the users' consumption/billing
data already exists from the CDG port, so nothing is uploaded here.

`setup` runs these steps per user:

1. **User record** — reads the user and confirms it's on the configured pilot.
2. **Subscription** — writes `event_subscriptions.USER_WELCOME.<FUEL>` and the
   `.OPT_OUT` variant with `delivery_modes = ["Email"]`. Both are written
   because QA users are commonly provisioned as `OPT_OUT` and the pipeline
   reads the variant matching the user's notification user type.
3. **Sent-count reset** — zeroes the `USER_WELCOME` sent count so an
   already-sent welcome email fires again.
4. **Publish + verify** — publishes the notification event to the pilot's
   notification queue, then polls until the notification records a send.

The shared reset → publish → poll core lives in
[`../_common/notification_trigger.py`](../_common/notification_trigger.py), and
the queue is discovered from the pilot — nothing is hardcoded.

## Setup

Configuration lives in the single shared
[`../config.json`](../config.json.example) — there is no per-folder config
file. Add each user under `USERS` with `"scripts": ["WelcomeEmail"]` and a
`FUEL`:

```json
"USERS": [
  { "UUID": "…", "label": "Welcome - electric",  "scripts": ["WelcomeEmail"], "FUEL": "ELECTRIC" },
  { "UUID": "…", "label": "Welcome - gas user",  "scripts": ["WelcomeEmail"], "FUEL": "GAS" }
]
```

Point `BASE_URL` and `PILOT_ID` at a new pilot and this script works there
unchanged.

### Settings

| Key | Default | Used for |
|-----|---------|----------|
| `FUEL` | `ELECTRIC` | `ELECTRIC` or `GAS` — the measurement type for the subscription and the notification. Use `GAS` for the gas-user welcome email. Settable per user. |
| `RERUN_AGGREGATION` | `false` | The welcome email isn't derived from an aggregation run, so the rerun is skipped by default. Set `true` to run one anyway. |
| `STATUS_TIMEOUT` | `180` | Seconds to wait for `sentCount >= 1`. |
| `STATUS_INTERVAL` | `10` | Poll interval for the wait. |
| `QUEUE_URL` | *(discovered)* | Set only to bypass queue discovery. |

## Running

```bash
uv run python setup_welcome_email.py            # every configured WelcomeEmail user
uv run python setup_welcome_email.py --uuid <uuid>   # one specific user
```

Or through the runner: `uv run python ../run_personas.py WelcomeEmail`

Expected email subject:

```
Welcome to your new alerts from Energy Co!
```

The recipient address comes from each user's own profile — it is not part of
the published event payload.

## Requirements

- The `aws` CLI on `PATH`, with credentials that can publish to the pilot's
  notification queue.
- Network access to `BASE_URL`.
