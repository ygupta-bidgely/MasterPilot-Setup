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

Configuration lives in the single shared [`../config.json`](../config.json.example)
— there is no per-folder config file. Add the user under `USERS` with
`"scripts": ["TOUOnboarding"]`, plus any per-script setting below (on the user
entry, under `scripts.TOUOnboarding`, or at the top level).

Nothing about the pilot is hardcoded: point `BASE_URL` and `PILOT_ID` at a new
pilot and this script works there unchanged. The notification queue is
discovered from the pilot at run time.

### CREATE_USER mode

Set `CREATE_USER: true` under `scripts.TOUOnboarding` to have the script
create a brand-new masterpilot-01 user itself before doing everything it
already does, so a single run is fully self-contained - no pre-existing
target user needed (the `USERS` entry is then not required).

```json
{
  "scripts": {
    "TOUOnboarding": {
      "CREATE_USER": true,
      "FOOTER_FROM_PILOT": 10037
    }
  }
}
```

The pilot the new user is created under is the shared `PILOT_ID`.

It works by cloning the newest `USERENROLL` row in the bucket as a structural
template (same technique as the rate-change step below), then overwriting the
identity/plan fields by name using the pilot's own `user_creation_launchpad`
position map - the same contract Hawk's `MasterPilot01UserCreator` reads, so
a newly created user here is indistinguishable from one Hawk creates. It
polls `/meta/tokens/{token}` (active, then `:inactive`) until the fresh
enrolment resolves to a uuid, the same mechanism Hawk's own user creator
uses. The new identity (uuid, email, customerId) is written to
`output/created_user.json` in addition to the usual step log.

| Key | Default | Used for |
|-----|---------|----------|
| `EMAIL_PREFIX` | `bidgelyqa+AUT_MP01_` | Prefix for the generated user's email (same convention as Hawk's `userDefaults.emailPrefix` for masterpilot-01), so the mail lands in the same monitored catch-all inbox. |
| `NEW_USER_RATE_PLAN` | `180` | The plan the new user starts on (a known-good non-TOU baseline on pilot 88001), before the usual TOU transition step moves them onto a TOU plan. |
| `NEW_USER_EFFECTIVE_DATE_LOOKBACK_MONTHS` | `3` | How many months back the new user's rate plan effective date is set (always the 1st of that month, matching the bill-cycle-aligned convention the proven rate-change step already uses - `now.replace(day=1)`). Service start is backdated a further 6 months before that, mirroring `MasterPilot01UserCreator`'s own defaults (a multi-year gap between `serviceAgreementStartDate` and `ratePlanEffectiveDate`). |
| `NEW_USER_TOKEN_TIMEOUT` | `900` | Seconds to poll `/meta/tokens/{token}` for the new uuid before giving up. |

Note: only fields the pilot's `user_creation_launchpad` config actually names
get overridden (confirmed live for pilot 88001: `CUSTOMER_ID`,
`USER_ACCOUNT_ID`, `PREMISE_ID`, `EMAIL_ID`, `UNV_SDP`, `RATE_PLAN_ID`,
`RATE_PLAN_EFFECTIVE_DATE`). Any field the pilot leaves unnamed (e.g. phone
number on pilot 88001) is left as whatever the cloned template row carries -
the script logs which fields were skipped this way.

### Optional

| Key | Default | Used for |
|-----|---------|----------|
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
