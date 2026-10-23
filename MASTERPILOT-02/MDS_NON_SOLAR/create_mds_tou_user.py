#!/usr/bin/env python3
"""TOU user for target pilot the target pilot (MDS, single fuel ELECTRIC D&R solar): launchpad FILES + INGESTION
+ rate transition onto the NVE TOU plan, then (by default) the TOU Onboarding and TOU Coaching emails.

NON-SOLAR (MDS_NON_SOLAR copy of MDS_AUTOMATION): the user is an MDS non-solar premise -- SOLAR_USER FALSE,
DELIVERED = whole consumption, RECEIVED present with all-zero readings, FEED_IN 0 kWh / $0, no solar user-scope
configs, checks expect solarUser / GLOBAL.SOLAR not true and no SOLAR_ENERGY_FLOW. Plans come from the profile. TOU:
enable_tou_chart_for_solar stays false, no SOLAR_ENERGY_FLOW, home.solarUser is never set true (the decouple notes
below are solar history). The TOU plan needs rateStructureImageUrl on its weekday energy rows.

Prerequisite: plan 907 MDS_TOU_EVRR_D_R exists (create_mds_nve_tou_rate_plan.py).

FILES (mds_common generator, results/tou/run_*/input_files/)
    BC_SCHEDULE, USERENROLL, METERENROLL (base plan MDS_SOLAR_TOU_D_R_2026, flat), RAW D + R (15 min, Wh, 13 cycles,
    solar + evening peak load --peak-kw), INVOICE (TOTAL / CONSUMPTION_BASED / FEED_IN / FIXED), and
    METERENROLL_..._T.csv = the rate transition row: plan MDS_TOU_EVRR_D_R, RATE_PLAN_EFFECTIVE_DATE = start of the
    last --transition-cycles completed cycles. RAW is priced interval by interval with the plan in force (engine hour
    semantics), so invoice and BILLING_COST agree across the transition.
QA FLOW (default) -- files in, emails out; NO NotificationsProcessorEvent is ever sent
    1 user + USER-scope email configs (subscription, section lists, strings, enable_tou_chart_for_solar, and
      <uuid>.TOU_ONBOARDING.ELECTRIC email_itemization_section_configs.use_previous_billing_cycle=true)
    2 BC_SCHEDULE, USERENROLL, METERENROLL (flat 905), RAW D+R, INVOICE uploaded and confirmed through the APIs
    3 disagg (SQS DISAGG, data pipeline) -> hybrid itemization for the flat-plan history
    4 the customer switches to TOU: USERENROLL_..._T.csv (plan 907, RATE_PLAN_EFFECTIVE_DATE) while the target pilot's
      launchpad_ingestion_configs.handle_meter_enrollment is flipped false for that one file (PILOT write, backed up,
      always restored; --restore-pilot-flag repairs an interrupted run). The ingester re-prices from the effective
      date and itself publishes TOU_ONBOARDING (LaunchpadUserCreator -> TouEmailAlertManager)
    5 the notifications-enabled aggregation (SQS AGG, data pipeline) runs the system Monthly Summary pass ->
      TOU Coaching (variant MS_TOU_COACHING). home.solarUser is false for that pass only (--solar-tou-chart
      decouple): the deployed tou_usage_solar_* templates carry a U+200E filename and kill the MS
    6 checks: T01-T03, O01-O10 (onboarding), C01-C08 (coaching), E01-E21 data validation. A missing email is
      diagnosed from the host logs and every known signature is classified CONFIG / DATA / SQL (fixable here)
      or TICKET (code/deploy) -- see mds_tou.KNOWN_ISSUES
    --manual-trigger restores the legacy flow (SQS NotificationsProcessorEvent per email).

RUN
    export BIDGELY_TOKEN=<target token>             # or --token-file (0600)
    python3 mds_user_automation/create_mds_tou_user.py                          # new user + both emails
    python3 mds_user_automation/create_mds_tou_user.py --phases user            # files + ingestion only
    python3 mds_user_automation/create_mds_tou_user.py --uuid <uuid> --phases configure,onboarding,coaching
    # after aggregation + aggregation-priority were restarted (rate cache reload): rebuild TOU pricing, then coaching
    python3 mds_user_automation/create_mds_tou_user.py --uuid <uuid> --phases reaggregate,coaching
    options: --transition-cycles 2  --peak-kw 2.2  --solar-tou-chart decouple|flag|auto  (+ any mds_common option)
    For log diagnosis on a failed send: export BOPS_PW_FILE=<jumphost sudo password file> (config-fail-testcase-debugger skill).

OUTPUT  results/tou/run_*/: input_files/, email/TOU_ONBOARDING_*.html, email/TOU_COACHING_MS_*.html,
        reports/{mds_validation_report,tou_onboarding_validation_report,tou_coaching_validation_report}.md, logs/
Exit code: 0 = PASS/WARN, 1 = a check FAILed, 2 = setup/wait error.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mds_tou  # noqa: E402

if __name__ == "__main__":
    sys.exit(mds_tou.main(__doc__, "user,configure,onboarding,coaching,validate"))
