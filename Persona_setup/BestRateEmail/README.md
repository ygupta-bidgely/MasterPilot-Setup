# Best Rate email persona setup

Prepares and triggers a user-scoped **Best Rate** email, end to end, for one
user. It intentionally does not write any pilot-level configuration —
everything it touches is scoped to the target user.

`setup` runs these steps in order:

1. **Rate Comparison** - reads the user's live `BILLING_CYCLE_PROJECTED` Rate
   Comparison result and picks the highest-savings non-current rate.
2. **User config** - adds user-level email/NBI configuration (email profile
   mode, subscription/opt-out delivery modes, template sections, feedback
   section).
3. **NBI rendering assets** - pushes the string resource and NBI asset rows
   (`nbi_rate_comparison_plan_<n>` insight/action) that render the email.
4. **Manual interaction** - writes a manual `RATE_COMPARISON` interaction via
   the legacy internal endpoint, uploads the same payload to the partitioned
   S3 profile path the notification flow actually reads from, and verifies
   the partitioned copy is selectable.
5. **Reset sentCount** - resets the user's `RATE_COMPARISON` Email sent count
   to `0` so the notification isn't suppressed as a duplicate.
6. **Aggregation rerun** - requests an aggregation rerun and waits for it.
7. **Notification** - publishes the `RATE_COMPARISON` / `Email` event to the
   environment's notifications queue via SQS (discovered from the pilot).
8. **Poll** - polls notification status until `sentCount >= 1`.

The token is never printed. AWS credentials/permissions are resolved by the
AWS CLI (`aws sqs send-message`, `aws s3api put-object`).

## Setup

Configuration lives in the single shared [`../config.json`](../config.json.example)
— there is no per-folder config file. Add the user under `USERS` with
`"scripts": ["BestRateEmail"]`.

Nothing about the pilot is hardcoded: point `BASE_URL` and `PILOT_ID` at a new
pilot and this script works there unchanged. The notification queue is
discovered from the pilot at run time.

These may be set on the user entry, under `scripts.BestRateEmail`, or at the
top level of the shared config.

### Required

| Key | Used for |
|-----|----------|
| `PROFILE_BUCKET` | Where the partitioned interaction profile is uploaded (the platform's `bidgely-profile-data-<env>` bucket). Not derivable from the pilot's own config, so it must be set. |
| `DASHBOARD_URL` | The action's "View rate plan details" CTA link. Environment-specific, so there is no default. |

### Optional

| Key | Default | Used for |
|-----|---------|----------|
| `HOME_ORDINAL` | `1` | Rate Comparison lookup, the interaction write/verify, and the notification payload. |
| `REGION` | `us-west-2` | SQS queue lookup/send and the S3 upload. |
| `QUEUE_URL` | discovered | The SQS queue the notification event is published to. Set it to skip queue discovery. |
| `HTTP_TIMEOUT` | `60` | Per-request timeout (seconds) for every API call. |
| `AGGREGATION_WAIT_SECONDS` | `30` | Wait after requesting the aggregation rerun, before publishing the notification. |
| `STATUS_TIMEOUT` | `180` | How long to poll notification status for `sentCount >= 1`. |
| `STATUS_INTERVAL` | `10` | Poll interval (seconds) while waiting on `sentCount`. |

## Running

From this folder, after `uv sync` at the repo root (see the
[root README](../../README.md)):

```bash
uv run python setup_best_rate_email.py
```

On success it prints the SQS message ID and the final `sentCount`. On failure
it prints `ERROR: ...` and exits non-zero - check the Notifications Processor
and Emailer logs if it times out waiting for `sentCount`.

### Outputs

Written to `output/` (git-ignored):

| File | Purpose |
|------|---------|
| `manual-rate-comparison-nbi-<uuid>.json` | The interaction payload written via the legacy endpoint and uploaded to S3. |
| `best-rate-summary-<uuid>.json` | A short summary: current/recommended plan and savings. |

## Requirements

- The `aws` CLI on `PATH`, configured with credentials that can resolve and
  send to the environment's notification queue and write to `PROFILE_BUCKET`.
- Network access to `BASE_URL`.
