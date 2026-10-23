#!/usr/bin/env python3
"""Create a brand-new DUAL FUEL MDS user on target pilot the target pilot
(ELECTRIC AMI solar MDS with DELIVERED + RECEIVED, plus GAS AMI) and validate both fuels.

NON-SOLAR (MDS_NON_SOLAR copy of MDS_AUTOMATION): the user is an MDS non-solar premise -- SOLAR_USER FALSE,
DELIVERED = whole consumption, RECEIVED present with all-zero readings, FEED_IN 0 kWh / $0, no solar user-scope
configs, checks expect solarUser / GLOBAL.SOLAR not true and no SOLAR_ENERGY_FLOW. Plans come from the profile.
Emails: ONE combined dual-fuel Monthly Summary (ELECTRIC non-solar + GAS); the solar-home kWh/NPE limits below do
not apply.

Every run builds new launchpad files with unique ids and data anchored to today:
BC_SCHEDULE, USERENROLL (a GAS row and an ELECTRIC row), METERENROLL (one row per fuel),
RAW D, RAW R, RAW G (in ccf), and one INVOICE covering both fuels on identical periods.
The last completed month is the Monthly Summary cycle.

The flow is the single-fuel flow plus gas: electric hybrid V2, gas hybrid V1 (the code
routes gas to V1), and a per-fuel aggregation. It runs the electric checks, 10 gas
checks (G01-G10: gas meter, RAW ccf->Wh, cycles, invoices, energy, cost, gas
itemization == gas energy, widgets, and the gas part of the Monthly Summary email reconciled to the
gas itemization), 1 dual-fuel check and D01 (dashboard link).

MONTHLY SUMMARY: ONE combined dual-fuel email (--ms-mode combined, the default).
    The separate per-fuel GAS email cannot be generated on a DCX solar home (EmailDataProvider:980 NPE,
    code fix pending), so the script sets USER-scope configs only (never pilot) once the meters exist:
      email_moderation: enable_combined_dual_fuel_email=true, eligible_fuels_for_dual_fuel_email=["ELECTRIC","GAS"],
                        consumption_unit_type_map={"ELECTRIC":"kWh","GAS":"CCF"}
      event_subscriptions.MONTHLY_SUMMARY: delivery_modes=["Email"]      (combined pass asks with no fuel)
      email_template_sections_config: MONTHLY_SUMMARY.DF_EMAILS.OPT_IN/OPT_OUT = the user's MS list with
                        DF_ITEMIZATION in place of ITEMIZATION_V2 (renders one block per fuel)
      strings <uuid>.MONTHLY_SUMMARY.<FUEL>: "Your Electricity Use By Appliance" / "Your Natural Gas Use By
                        Appliance" block headings and "Your Electricity Summary"
    Product limits (not config): a combined email for a SOLAR home is always in kWh (EmailDataProvider:397),
    the subject and summary section are electric only, and gas shows usage without cost (G10 WARN).
    --ms-mode separate keeps one email per fuel; --ms-resend rewinds the sent-state and sends again.

PILOT PREREQUISITE: in MDS mode meters come only from launchpad_ingestion_configs
.supported_streams[fuel]. the target pilot ships ELECTRIC only, so without a GAS entry the gas
rows are accepted and no gas meter is ever created. The script refuses to run
until the entry exists. --ensure-gas-stream adds it (PILOT scope, backed up to the
run dir first):
    {"measurementType":"GAS","streams":[{"streamType":"DELIVERED","streamTokenSuffix":""}]}

RUN
    export BIDGELY_TOKEN=<target bearer token>      # or --token-file (0600)
    python3 mds_user_automation/create_mds_dual_fuel_user.py --ensure-gas-stream    # first run on the pilot
    python3 mds_user_automation/create_mds_dual_fuel_user.py                        # afterwards
    python3 mds_user_automation/create_mds_dual_fuel_user.py --dry-run
    python3 mds_user_automation/create_mds_dual_fuel_user.py --phases validate --manifest mds_user_automation/results/dual_fuel/run_<id>/manifest.json
    python3 mds_user_automation/create_mds_dual_fuel_user.py --phases notify,validate --ms-resend --manifest <run>/manifest.json

OUTPUT  mds_user_automation/results/dual_fuel/run_<stamp>_<id>/
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
    sys.exit(mds_common.main(__doc__, ("ELECTRIC", "GAS"), str(Path(__file__).resolve().parent / "results" / "dual_fuel")))
