#!/usr/bin/env python3
"""Create a brand-new SINGLE FUEL MDS user on target pilot the target pilot and
validate the whole chain:

NON-SOLAR (MDS_NON_SOLAR copy of MDS_AUTOMATION): the user is an MDS non-solar premise -- SOLAR_USER FALSE,
DELIVERED = whole consumption, RECEIVED present with all-zero readings, FEED_IN 0 kWh / $0, no solar user-scope
configs, checks expect solarUser / GLOBAL.SOLAR not true and no SOLAR_ENERGY_FLOW. Plans come from the profile.
Emails: Monthly Summary for the last completed cycle (E21, $ amounts).

  ELECTRIC AMI solar, DELIVERED + RECEIVED streams, plan MDS_SOLAR_TOU_D_R_2026.

Every run builds new launchpad files (BC_SCHEDULE, USERENROLL, METERENROLL,
RAW D, RAW R, INVOICE) with unique ids and data anchored to today. The last completed
month (e.g. August when run in September) is the Monthly Summary cycle.

The run uploads each file and waits for its API state, sets user-scope flags, then
runs disagg (hybrid V2) and aggregation, sends the Monthly Summary email, and
runs 21 checks: user, meters, RAW, cycles, invoices == RAW, per-stream energy and
cost, net-billable, hybrid status, itemization == invoice energy for every cycle,
dashboard widgets, bill projection and analysis, and the MS email content against
the itemization.

RUN
    export BIDGELY_TOKEN=<target bearer token>      # or --token-file (0600)
    python3 mds_user_automation/create_mds_single_fuel_user.py                  # full end-to-end
    python3 mds_user_automation/create_mds_single_fuel_user.py --dry-run        # files only, offline
    python3 mds_user_automation/create_mds_single_fuel_user.py --phases preflight
    python3 mds_user_automation/create_mds_single_fuel_user.py --phases validate --manifest mds_user_automation/results/single_fuel/run_<id>/manifest.json

OUTPUT  mds_user_automation/results/single_fuel/run_<stamp>_<id>/
    SUMMARY.txt              user details (uuid, zipcode, ids, tokens), files, MS email, check table
    manifest.json            everything the run generated/learned (used by --manifest to resume)
    input_files/             the launchpad files uploaded to S3
    logs/run.log             full timestamped log, every line tagged [uuid=...] once created
    logs/log_grep_hints.txt  host + grep per service (ingestion, aggregation, PyAMI, notifications, emailer)
    reports/                 validation_report.md / .json, SUMMARY.txt
    email/                   MS_email_<n>.html exactly as sent
    api_evidence/            raw API responses behind every check
    sqs_messages/            every SQS body sent (disagg, aggregation, MS)
    pilot_config_backups/    pilot configs backed up before any --ensure-* pilot write
Exit code: 0 = all checks PASS/WARN, 1 = a check FAILed, 2 = setup/wait error.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mds_common  # noqa: E402

if __name__ == "__main__":
    sys.exit(mds_common.main(__doc__, ("ELECTRIC",), str(Path(__file__).resolve().parent / "results" / "single_fuel")))
