# Best Rate email persona setup

Prepares and triggers a user-scoped **Best Rate** email in MasterPilot ProductQA,
end to end, for one user. It intentionally does not write any pilot-level
configuration — everything it touches is scoped to the target user.

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
   MasterPilot notifications queue via SQS.
8. **Poll** - polls notification status until `sentCount >= 1`.

The token is never printed. AWS credentials/permissions are resolved by the
AWS CLI (`aws sqs send-message`, `aws s3api put-object`).

## Setup

The real config file holds a live token and is git-ignored. Copy the template
and fill it in:

```bash
cp config.json.example config.json
```

```json
{
  "AUTH_TOKEN": "<api-server bearer token>",
  "UUID": "<destination user uuid>",
  "PILOT_ID": 88001
}
```

### Required

| Key | Used for |
|-----|----------|
| `AUTH_TOKEN` | Bearer token for every `BASE_URL` call. |
| `UUID` | The user this Best Rate email is prepared and sent for. |

### Optional

| Key | Default | Used for |
|-----|---------|----------|
| `PILOT_ID` | `88001` | Must stay `88001` - the script is intentionally locked to MasterPilot ProductQA; it validates the resources, queue, and buckets it touches only apply to that pilot. |
| `BASE_URL` | `https://api-server-masterpilot-productqa.bidgely.com` | Every API call. |
| `HOME_ORDINAL` | `1` | Rate Comparison lookup, the interaction write/verify, and the notification payload. |
| `REGION` | `us-west-2` | SQS queue lookup/send and the S3 upload. |
| `QUEUE_NAME` | `NotificationsProcessorEvent-productqa-masterpilot` | The SQS queue the notification event is published to. |
| `PROFILE_BUCKET` | `bidgely-profile-data-productqa` | Where the partitioned interaction profile is uploaded. |
| `DASHBOARD_URL` | `https://masterpilot-mppqa01.bidgely.com/dashboard/insights/rate-plans` | The action's "View rate plan details" CTA link. |
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

- The `aws` CLI on `PATH`, configured with credentials that can send to
  `QUEUE_NAME` and write to `PROFILE_BUCKET`.
- Network access to `BASE_URL`.
