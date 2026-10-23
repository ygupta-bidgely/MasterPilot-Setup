#!/usr/bin/env python3
"""BUDGET BILLING user for target pilot the target pilot (MDS, single fuel ELECTRIC D&R solar), END TO END IN ONE COMMAND:
a new user, its Budget Billing enrolment, the Monthly Summary email with the BB section, and the web dashboard BB data.
Everything it needs (pilot config, user config, strings, section list) is created by the script; nothing is manual.

NON-SOLAR (MDS_NON_SOLAR copy of MDS_AUTOMATION): the user is an MDS non-solar premise -- SOLAR_USER FALSE,
DELIVERED = whole consumption, RECEIVED present with all-zero readings, FEED_IN 0 kWh / $0, no solar user-scope
configs, checks expect solarUser / GLOBAL.SOLAR not true and no SOLAR_ENERGY_FLOW. Plans come from the profile. B14
checks the home is NOT solar (no solar cost-breakdown flag is written).

    python3 mds_user_automation/create_mds_budget_billing_user.py --token-file <0600 file>     # or export BIDGELY_TOKEN

STEPS (in order; each is idempotent and confirmed through the API before the next)
 1 PILOT the target pilot enablement, BEFORE any upload (additive: only MISSING keys, pre-change config backed up to pingpong/backups/):
        program_part_launchpad            USERPROGRAM field positions. Absent on the target pilot until 2026-10-01: without it the ingester NPEs
                                          (validateDates:337, marker 500.2001 "unknown internal error") and nobody can be enrolled
        budget_billing_configs            bb_program_ids_map ELECTRIC -> BB_PROGRAM_1 (+ averaging keys)
        bb_cumulative_balance_section_configs + ..._palette   chart colours 1/4/43/86-90 (a missing colour aborts the whole MS render)
 2 USER files (mds_common generator): BC_SCHEDULE, USERENROLL, METERENROLL, RAW D+R, INVOICE (+ a positive BB_AMOUNT row on each
        BB cycle), then USERPROGRAM (BB_PROGRAM_1, BB_TrueUp_Period) last; ingestion confirmed via API, programs API and the web widget
 3 USER-scope config: solar + hybrid flags, $ display, the 9 BB strings if missing, Monthly Summary section list with
        BB_CUMULATIVE_BALANCE; the automatic MS is held so the BB one is the only one
 4 disagg + aggregation + itemization, then the MONTHLY_SUMMARY email (variant MS_BB) and the passwordless web dashboard link
 5 VALIDATION, 41 checks, written to reports/ and summarised in reports/BB_SUMMARY.md:
        P01-P09  pilot prerequisites, ingestion markers, generated BB rows
        B01-B16  programs API row, BB_TrueUp_Period, start date, web budgetBillingInfo (bbAmount, actualBillAmount, trueUp), invoice
                 totals, platform-resolved + user MS section list, solar flags, dashboard link
        E01-E18  email generated, variant MS_BB, BB chart + title once, balance insight == budget - actual (from the generated data),
                 True-up + BB disclaimers, bill == invoice == web widget, charts fetch as images, templating, branding, sent-state
    BillProjection is not generated on MDS (known): reported as INFO, never a failure.

OPTIONS  --bb-amount 0 (0 = 110% of the average cycle bill)  --bb-trueup-period 12  --bb-elapsed-cycles 6  --bb-date-format %Y-%m-%d
         --no-pilot-config  --allow-auto-ms  --zip ...  (+ any mds_common option)
OTHER    --dry-run-files                 offline: generate every file, contact nothing
         --uuid <uuid> --phases configure,verify,email     redo the config + email for an existing BB user
         --uuid <uuid> --phases verify,email --no-send     re-validate the email already sent, send nothing
         --manifest <run>/manifest.json --phases pilot,program,aggregate,configure,verify,email    resume a stopped run
OUTPUT   results/budget_billing/run_*/: input_files/, email/BB_MS_*.html, reports/{BB_SUMMARY.md,bb_verify_validation_report.md,
         bb_email_validation_report.md}, logs/run.log, manifest.json
Exit code: 0 = no check FAILed, 1 = a check FAILed, 2 = setup/wait error.
Known platform defects found while building this: BUGS_budget_billing_mds.md
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__":
    if "--dry-run-files" in sys.argv:         # offline generation of every file, nothing uploaded
        import mds_common as mc
        argv = [x for x in sys.argv[1:] if x != "--dry-run-files"]
        sys.exit(mc.main(__doc__, ("ELECTRIC",), str(Path(__file__).resolve().parent / "results" / "budget_billing"),
                         ["--dry-run", "--budget-billing", *argv]))
    import mds_bb
    sys.exit(mds_bb.main(__doc__, "user,configure,verify,email"))
