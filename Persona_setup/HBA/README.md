# HBA (High Bill Alert) persona setup

Creates a brand-new synthetic MasterPilot user from scratch and drives it,
end to end, through the **HIGH_BILL_ALERT** (HBA) email trigger in ProductQA:
S3 ingestion, user-level HBA configuration, string-resource seeding,
projection calibration, SQS aggregation passes, and notification
verification.

Unlike every other folder under `Persona_setup/`, this tool does **not** read
a `config.json` and does **not** operate on an existing user — it is 100%
CLI-flag driven and always creates a fresh synthetic user for the run. See
[Why this folder is different](#why-this-folder-is-different) below.

`create_hba_s3_user.py` runs these steps in order:

1. Generate unique external user/account/premise/meter identifiers.
2. Generate a USERENROLL file, a completed-cycle INVOICE file, and two RAW
   files (a normal current-cycle seed day, then an abnormal high-usage
   stretch).
3. Upload them to the MasterPilot residential S3 ingestion bucket.
4. Resolve the UUID created by ingestion and configure HBA at USER level.
5. Seed and verify every string resource the HBA render dereferences without
   a null check, **before** any aggregation is queued — this is the step
   that most often decides whether an email ever arrives (see
   [Known issues / gotchas](#known-issues--gotchas)).
6. Upload only current-cycle raw data; completed historical bills remain
   INVOICE data.
7. Calibrate the abnormal usage, upload it, then reseed the expected bill
   against it and confirm the resulting projection lands inside HBA's accept
   band — correcting the multiplier once if it does not.
8. Close the seeding window so later passes evaluate instead of reseeding.
9. Send tightly scoped MONTH-only `AggregateUploadEvent` XML messages to the
   MasterPilot ProductQA aggregation queue.
10. Confirm `HIGH_BILL_ALERT` notification state.

Dates are derived from the execution date and `--cycle-day`. For example,
running on 2026-08-27 with the default cycle day 5 creates the active cycle
2026-08-05 through 2026-09-04 and completed historical cycles before it.

## Setup

No config file to copy. You need:

- **AWS credentials** on the normal boto3 resolution chain (env vars,
  `~/.aws/credentials`, SSO, etc.) that can `PutObject` in the destination
  bucket and `SendMessage` to `AggregationReadyUsers-productqa-masterpilot`.
- **A ProductQA API token** — set `BIDGELY_TOKEN` in the environment, or
  leave it unset and the script will prompt for it securely (never printed,
  never written to a file).
- **Pilot 88001 must already have `HIGH_BILL_ALERT`** in
  `meta_data.supported_communication_events` — the script verifies this but
  will not silently modify the pilot-wide gate.

`config.json.example` in this folder is a **reference only**, listing every
CLI flag and its default in JSON form for readability — the script never
reads it.

## Running

From this folder, after `uv sync` at the repo root (see the
[root README](../../README.md)):

```bash
uv run python create_hba_s3_user.py
uv run python create_hba_s3_user.py --dry-run
uv run python create_hba_s3_user.py --anchor-date 2026-08-27 --cycle-day 5
uv run python create_hba_s3_user.py --drop-mtd-section
uv run python create_hba_s3_user.py --restore-from hba-s3-user-output/<run>/manifest.json
```

`--dry-run` generates and validates every local file with no S3/API/SQS side
effects. `--restore-from <manifest>` re-applies just that prior run's config
restore and exits — use it to finish cleanup after an interrupted run.

### Flags

| Flag | Default | Used for |
|------|---------|----------|
| `--bucket` | `bidgely-masterpilot-resi-fl-productqa` | S3 ingestion bucket. |
| `--region` | `us-west-2` | AWS region for the S3/SQS clients. |
| `--api-base` | `https://api-server-masterpilot-productqa.bidgely.com` | ProductQA API base URL. |
| `--pilot-id` | `88001` | Pilot to create the user under. |
| `--email` | generated: `bidgelyqa+AUT_MP01_<meter-id>@bidgely.com` | Recipient override. |
| `--first-name` | `STEPHEN` | Synthetic identity. |
| `--last-name` | `CALANDRA` | Synthetic identity. |
| `--address` | `153 2nd St.` | Synthetic identity. |
| `--city` | `Los Altos` | Synthetic identity. |
| `--state` | `CA` | Synthetic identity. |
| `--zipcode` | `11691` | Synthetic identity. |
| `--timezone` | `America/Los_Angeles` | Validated via `ZoneInfo`. |
| `--rate-plan` | `194` | Rate plan for enrolment. |
| `--billing-cycle` | derived: `CDG_<cycle-day>` (e.g. `CDG_05`) | Billing cycle code. |
| `--cycle-day` | `5` | Must be 1–28. |
| `--history-cycles` | `24` | Must be ≥12, for a stable HBA baseline. |
| `--anchor-date` | today in `--timezone` | `YYYY-MM-DD`. |
| `--high-usage-multiplier` | `auto` | `auto` measures the priced seed-day cost and picks a multiplier landing the projection inside HBA's accept band; or pass a number > 1 to force one. |
| `--output-dir` | `hba-s3-user-output` | Local generated-file directory (git-ignored). |
| `--poll-seconds` | `20` | Poll interval, must be ≥1. |
| `--settle-seconds` | `120` | Post-upload ingestion settle delay, ≥0. |
| `--wait-minutes` | `30` | Max wait for MONTH aggregation output to verify, ≥1. |
| `--notification-wait-minutes` | `10` | Max wait on the final evaluation pass for the HBA notification, ≥1. |
| `--evaluation-passes` | `2` | Full-consumption-type SQS passes after seeding, must be ≥2 — pass 1 only raises the MTD disagg event and can never send. |
| `--mtd-wait-minutes` | `5` | Wait between evaluation passes so MTD disagg lands, ≥1. |
| `--mtd-pass-wait-minutes` | `1.5` | Notification poll window for a non-final (MTD-only) pass. |
| `--drop-mtd-section` | off | Omit `MTD_BREAK_DOWN_SECTION` — works around an `IndexOutOfBoundsException` in `MTDBreakDownContentProvider.populateMTDAppliances` when itemization yields exactly 1–2 appliances. |
| `--dry-run` | off | Generate/validate local files only; no S3/API/SQS calls. |
| `--restore-from` | none | Path to a prior run's `manifest.json`; re-applies only that run's config restore and exits. |

### Outputs

Written to `--output-dir` (default `hba-s3-user-output/`, git-ignored),
under a per-run `hba-<external-user>-<timestamp>/` directory:

| File | Purpose |
|------|---------|
| `USERENROLL_D_*.txt` | The synthetic user enrolment row uploaded to S3. |
| `INVOICE_01_*.txt` | Completed historical billing-cycle rows. |
| `RAW_D_900_S_*001-000_01.txt` (×2) | Current-cycle interval reads: one normal seed day, one abnormal high-usage stretch. |
| `manifest.json` | Full run record — IDs, cycle, config/resource snapshots, SQS message details, calibration numbers, final notification state. Also the sole input to `--restore-from`. |
| `run.log` | Timestamped step-by-step log of the run. |

## Cleanup

Config keys this run changes are restored exactly from the on-disk snapshot,
so an interrupted run can be finished with `--restore-from`. String
resources cannot be restored: `2.1/stringResources` exposes `POST`/`PUT` but
no `DELETE`, so any id this run seeds stays as a USER-scope row. Those are
recorded in the manifest under `resourceSnapshot` and reported as `RESOURCE
LEFTOVER` warnings at the end of the run — harmless for a throwaway QA user,
worth cleaning up in SQL if the user is reused for anything that should show
the pilot's real copy.

## Known issues / gotchas

- **String resources must be seeded before aggregation.** Every content
  provider on the HBA path does `stringMap.get(<id>).getText()` with no null
  check, so a single unseeded string resource id throws out of
  `GenericEventDataEmailContentBuilder.getHtmlContentData` and aborts the
  entire email — not just its own section. Notification state is written
  only on a successful send, so from the API this looks identical to a
  trigger-gate rejection: an empty
  `notificationStatus/<uuid>/1/HIGH_BILL_ALERT/ELECTRIC/Email`. Only
  `Emailer.log` tells the two apart.
- **Seed-window / projection ordering is deliberately fragile.**
  `isHBAEligibleForMTDTrigger` accepts a projection only in
  `[expected * threshold, expected * threshold * 4)` and drops it silently
  at both ends (the upper end logs "Projection crossed max threshold").
  Neither end writes notification state. `expected` is whatever
  `calculateExpectedBill` stored in `hba_expected_bill`
  (`/2.1/users/{uuid}/homes/1/hbaExpectedCost`), and that row is written
  only while `today <= billCycleStart + high_bill_alert_trigger_period_in_days`
  days — during which `isEligibleForHBATrigger` is false. The seed window
  has to stay open across the high-usage upload and close only once the
  stored value reflects the data the evaluation passes will judge.
- **RAW files need distinct batch stamps.** Two RAW files sharing the same
  14-digit stamp and differing only in the trailing sequence collide:
  ingestion silently keeps the first and drops the second, with no error
  anywhere. The script gives each RAW file its own stamp and keeps sequence
  `001` for both — the only sequence observed to ingest reliably.
- **`--drop-mtd-section`** is a workaround, not a permanent fix, for an
  itemization edge case (1–2 appliances after filtering) that throws inside
  the platform's MTD breakdown renderer.

## Why this folder is different

Every other tool under `Persona_setup/` takes an **existing** user (a
`UUID` in `config.json`) and layers persona config onto it. This tool
instead provisions a brand-new synthetic user from raw S3 ingestion files,
which is why it stays CLI-flag/env-var driven rather than adopting the
`config.json` convention — porting the full from-scratch generation,
calibration, and multi-pass evaluation flow into that shape wasn't worth the
risk to logic whose step ordering is load-bearing (see
[Known issues / gotchas](#known-issues--gotchas)).

## Requirements

- Python packages `boto3` and (implicitly) `botocore`, installed via
  `uv sync` at the repo root.
- AWS credentials that can `PutObject` in the destination bucket and
  `SendMessage` to `AggregationReadyUsers-productqa-masterpilot`.
- A ProductQA API token in `BIDGELY_TOKEN`, or enter it at the secure prompt.
- Network access to `--api-base`.
