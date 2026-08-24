# TOU Onboarding email persona setup

Prepares and triggers a user-scoped **TOU Rate Onboarding** email in
MasterPilot ProductQA, end to end, for one user. It never writes pilot-level
configuration - every override it creates is scoped to the single user
configured, and it reads the pilot's own contract (file layout, bucket,
delimiter, rate plans) instead of hardcoding one, so it works on a new pilot
too.

`setup` runs these steps in order:

1. **User record** - reads the user's pilot, notification user type, current
   rate plan, and rate schedule.
2. **Ingestion contract** - reads the pilot's launchpad ingestion config
   (file layout, delimiter, S3 bucket, enrol-file prefix, date format).
3. **TOU plan selection** - picks a residential TOU rate plan that's
   currently valid and has enough TOU months, ranking candidates by shape
   (split weekday/weekend bands and fewer TOU tiers first - see "Known
   issues" below) and walking the ranked list until one actually renders.
4. **User config** - subscribes the event, sets
   `email_template_sections_config`, and creates the
   `com.bidgely.cloud.email.subject.tou.onboarding` string resource (optionally
   copying the footer identity from another pilot).
5. **Enrolment file** - clones the user's newest `USERENROLL` row, swaps in
   the new rate plan and effective date, and uploads it to S3.
6. **Rate transition** - waits for the new plan to land on the user's rate
   schedule.
7. **Trigger** - resets the sent count and, if ingestion didn't already fire
   the event, publishes it directly to the notifications queue.
8. **Verify** - polls notification status for `sentCount >= 1`, then fetches
   and saves the rendered email, and if a plan doesn't render, auto-retries
   with the next ranked candidate.

The token is never printed. AWS credentials/permissions are resolved by the
AWS CLI (`aws s3api`/`aws s3`, `aws sqs`).

## Setup

The real config file holds a live token and is git-ignored. Copy the template
and fill it in:

```bash
cp config.json.example config.json
```

```json
{
  "AUTH_TOKEN": "<api-server bearer token>",
  "UUID": "<destination user uuid>"
}
```

### Required

| Key | Used for |
|-----|----------|
| `AUTH_TOKEN` | Bearer token for every `BASE_URL` call. |
| `UUID` | The user this TOU Onboarding email is prepared and sent for. |

### Optional

| Key | Default | Used for |
|-----|---------|----------|
| `BASE_URL` | `https://api-server-masterpilot-productqa.bidgely.com` | Every API call. |
| `HOME_ORDINAL` | `1` | The home this runs against. |
| `REGION` | `us-west-2` | SQS queue discovery/send and the S3 upload. |
| `RATE_PLAN` | derived | Pin a known-good plan number or name (fastest - skips ranking and retries). E.g. `194` on pilot `88001`. |
| `EFFECTIVE_DATE` | first of the current month | Rate plan effective date, in the pilot's own date format. |
| `FOOTER_FROM_PILOT` | none | Copy `utility_name`/`utility_address`/`utility_customer_cs_email` from this pilot's `email_template` config onto the user. |
| `SUBJECT_TEXT` | `Your New Rate Can Help You Save` | The subject string resource created for the user. |
| `QUEUE_URL` | discovered | Skip environment discovery and publish directly to this SQS queue URL. |
| `SCAN_LIMIT` | `40` | How many of the newest enrolment files to scan for the user's row. |
| `TRANSITION_TIMEOUT` | `300` | Seconds to wait for the rate transition to land. |
| `EMAIL_TIMEOUT` | `240` | Seconds to wait for `sentCount >= 1` per plan attempt. |
| `POLL_INTERVAL` | `10` | Poll interval (seconds) for both the transition and the email. |
| `RESET_SCHEDULE` | `false` | Collapse the rate schedule to one non-TOU plan first. **Needed if the user is already on a TOU plan** - otherwise `isNewTouUser` is false and the event drops as `OLD_TOU_USER`. |
| `MAX_PLAN_ATTEMPTS` | `3` | How many ranked TOU plans to try before giving up. |
| `SKIP_FILE` | `false` | Skip the enrolment file and re-fire the event directly - use when the user is already on the target plan. |

## Running

From this folder, after `uv sync` at the repo root (see the
[root README](../../README.md)):

```bash
uv run python setup_tou_onboarding_email.py
```

Wait ~2 minutes. On success it prints `email generated` plus the subject and
footer, and saves the rendered HTML under `output/`. Check the inbox on the
user's email address to confirm. On failure it prints `ERROR: ...`, or - if
every candidate plan was tried and none rendered - a reminder to pin
`RATE_PLAN` or check the Notifications Processor and Emailer logs.

### Outputs

Written to `output/` (git-ignored):

| File | Purpose |
|------|---------|
| `<ENROLL_PREFIX>_D_<timestamp>_T3.txt` | The enrolment row uploaded to S3. |
| `tou_onboarding_<notificationId>.html` | The rendered email body, once verified. |

## Known issues / gotchas

- **Notification status needs the measurement type in the path** -
  `/notification/notificationStatus/{uuid}/{home}/TOU_ONBOARDING/{measurementType}/Email`.
  Without it the response is always empty, which reads as "no email was ever
  sent."
- **S3 upload needs `utility_file_name` metadata** - without it the enrolment
  file sits in the bucket forever with no error and is never picked up.
- **Queue name matters** - the notification queue is
  `NotificationsProcessorEventPriority-<env>`; the non-`Priority` queue is a
  different environment and messages published there vanish silently. The
  script discovers the right suffix by probing which one resolves the
  pilot's own enrolment queue.
- **Pilot 88001: only plan 194 renders.** Plans 621, 622, and 609A do not -
  the render fails silently with no drop reason recorded. Best evidence so
  far: 622/609A leave `weekEndPeakHrs` null (a single `wk1-7` band instead of
  split weekday/weekend bands), and only the `rateType.diff.*` disclaimer
  strings exist on this pilot - the `season.diff.*` and `season.same.*`
  families are missing entirely. Worth a ticket on the notifications-processor
  side. Until then, `select_tou_plan` ranks split-week-band, fewer-TOU-tier
  plans first and the retry loop walks the list, but pinning `RATE_PLAN: 194`
  on this pilot is fastest.

## Requirements

- The `aws` CLI on `PATH`, configured with credentials that can read/write
  the pilot's ingestion bucket and send to its notifications queue.
- Network access to `BASE_URL`.
