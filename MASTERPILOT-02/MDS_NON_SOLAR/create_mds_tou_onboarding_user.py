#!/usr/bin/env python3
"""TOU RATE ONBOARDING, QA style: a NEW <deployment> the target pilot MDS user on the flat plan, whose history is ingested and
processed, then who switches to the TOU plan. The platform publishes TOU_ONBOARDING itself on the switch.
No NotificationsProcessorEvent.

NON-SOLAR (MDS_NON_SOLAR copy of MDS_AUTOMATION): the user is an MDS non-solar premise -- SOLAR_USER FALSE,
DELIVERED = whole consumption, RECEIVED present with all-zero readings, FEED_IN 0 kWh / $0, no solar user-scope
configs, checks expect solarUser / GLOBAL.SOLAR not true and no SOLAR_ENERGY_FLOW. Plans come from the profile. TOU
Onboarding: a non-solar home takes the regular branch (the solar TOU_RATE_STRUCTURE_NOT_AVAILABLE drop below is
solar-only); the TOU plan needs rateStructureImageUrl.

ROUND 1  BC_SCHEDULE, USERENROLL, METERENROLL (flat 905 MDS_SOLAR_TOU_D_R_2026), RAW D + R, INVOICE (RAW priced with the
         plan in force each interval: flat before the effective date, TOU after) -> disagg + aggregation (waits for both)
SWITCH   USERENROLL_..._T.csv: RATE_PLAN_ID MDS_TOU_EVRR_D_R (907), RATE_PLAN_EFFECTIVE_DATE = start of the last
         --transition-cycles completed cycles. the target pilot keeps launchpad_ingestion_configs.handle_meter_enrollment=true,
         and the METERENROLL path publishes no onboarding event until the TouOnboardingEmailTrigger fix is deployed,
         so the flag is false for this one file (PILOT write, backed up, always restored; --restore-pilot-flag).
         The ingester re-prices from the effective date and publishes TOU_ONBOARDING (LaunchpadUserCreator).
ROUND 2  disagg + aggregation again on the TOU plan (waits for both)
CONFIG   USER scope before any data: event_subscriptions.TOU_ONBOARDING=["Email"], TOU_ONBOARDING section list,
         subject string, and <uuid>.TOU_ONBOARDING.ELECTRIC email_itemization_section_configs.use_previous_billing_cycle
         =true (avoids the populateSolarEnergyFlowBlock NPE on a solar home).
CHECKS   T01 (schedule 905 -> 907 via ingestion), T02 (plan 907 TOU periods), O01-O10 (email, subject, rate-structure
         image, peak window, sections, templating, branding, sent-state). A missing email is diagnosed from the
         notification-priority / emailer passes and classified CONFIG/DATA/SQL/ENV/TICKET.
KNOWN    on a SOLAR home (every MDS home) the event is published and processed, then dropped
         TOU_RATE_STRUCTURE_NOT_AVAILABLE: populateTouRateStructure has no solar branch -> TICKET (reported as OX*).

RUN
    export BOPS_PW_FILE=$TMPDIR/bops_pw_$USER     # lets a missing email be diagnosed from the host logs
    python3 mds_user_automation/create_mds_tou_onboarding_user.py --email bidgelyqa+tou_onb_$(date +%m%d%H%M)@bidgely.com
OUTPUT results/tou/run_*/email/TOU_ONBOARDING_*.html, reports/tou_onboarding_validation_report.md
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mds_tou  # noqa: E402

if __name__ == "__main__":
    sys.exit(mds_tou.main(__doc__, "user,onboarding", flow="onboarding"))
