# Post-CDG aggregation

The step between porting users and triggering their emails.

A freshly ported user has no **billing cycle** — `billStartTs` is `0`. The
cycle-derived notifications (Monthly Summary, Bill Projection, Budget Alert,
Neighbourhood Comparison) are generated *for a completed billing cycle*, so
until one exists the pipeline consumes the event and correctly produces
nothing: the notification status record appears, but `sentCount` stays `0`.

This folder fixes that. Per user, in order:

1. `GET /meta/users/<uuid>/homes/<home>/modified` — flag the home as modified
   so the pipeline reprocesses it.
2. **wait 2 minutes** — let the flag propagate.
3. `POST /billingdata/users/<uuid>/homes/<home>/run/aggregations` — build the
   billing cycles.
4. **wait 2 minutes** — let aggregation complete before the next user starts.

The waits are the point, not padding: each call hands off to an asynchronous
pipeline and the next step needs the previous one to have landed. At roughly
four minutes per user, a full run is meant to be started and left alone.

## Where this fits

```
cdg_user_setup/create_users.py     ports the users
        |
        v
PostCdgAggregation/                <- builds their billing cycles
        |
        v
WelcomeEmail/ MonthlySummary/ ...  trigger the emails
```

## Setup

Configuration lives in the single shared
[`../config.json`](../config.json.example) — there is no per-folder config
file. This step applies to **every** user in `USERS`, not a per-script subset,
since any user that will receive a cycle-derived email needs it.

Populate `USERS` from the pilot first:

```bash
uv run python ../sync_users_from_pilot.py
```

### Settings

| Key | Default | Used for |
|-----|---------|----------|
| `MODIFIED_WAIT_SECONDS` | `120` | Wait after the modified call. |
| `AGGREGATION_WAIT_SECONDS` | `120` | Wait after aggregation, before the next user. **Note:** the email scripts also read this key, with a much shorter default — pass `--aggregation-wait` here to avoid inheriting theirs. |

## Running

```bash
uv run python run_post_cdg_aggregation.py --dry-run          # plan + timing
uv run python run_post_cdg_aggregation.py                    # every configured user
uv run python run_post_cdg_aggregation.py --uuid <uuid>      # one user (repeatable)
uv run python run_post_cdg_aggregation.py --modified-wait 120 --aggregation-wait 120
```

Or through the runner: `uv run python ../run_personas.py PostCdgAggregation`

## Checking the result

Billing cycles are built asynchronously, so the run finishing is not the same
as the cycles existing. Check with:

```bash
uv run python check_billing_cycles.py
uv run python check_billing_cycles.py --event BILL_PROJECTION
```

```
persona                                 billStartTs   sentCount  projection  ready
TOU PROMOTION (pilot 88009)             2026-07-15    1          100.66      YES
Monthly Summary - TOU Coaching          none          0          186.76      no
```

A user showing `ready=YES` has a cycle and can generate its email. One showing
`none` cannot — re-check after giving the pipeline more time, since a
`projection` value alone is not sufficient.

## Requirements

- Network access to `BASE_URL`. No AWS credentials needed — this step makes no
  SQS calls.
