#!/usr/bin/env python3
"""TOU ONBOARDING for an existing TOU user on <deployment> the target pilot (created by create_mds_tou_user.py).

NON-SOLAR (MDS_NON_SOLAR copy of MDS_AUTOMATION): the user is an MDS non-solar premise -- SOLAR_USER FALSE,
DELIVERED = whole consumption, RECEIVED present with all-zero readings, FEED_IN 0 kWh / $0, no solar user-scope
configs, checks expect solarUser / GLOBAL.SOLAR not true and no SOLAR_ENERGY_FLOW. Plans come from the profile. A
non-solar home takes the regular TOU branch.

    Default (system mode): sends NOTHING. Re-checks the onboarding email the SYSTEM generated when the user's
    rate-transition file was ingested (O01-O10) and, if there is none, diagnoses the notification/emailer passes and
    classifies the cause (CONFIG / DATA / SQL fixable here, or TICKET). To produce a new system-generated onboarding
    email, create a new user: create_mds_tou_user.py.
    --manual-trigger  legacy: reset the TOU_ONBOARDING state and send NotificationsProcessorEvent {TOU_ONBOARDING}

RUN
    python3 mds_user_automation/send_mds_tou_onboarding_email.py --uuid <uuid>
OUTPUT  <run>/email/TOU_ONBOARDING_<id>.html, <run>/reports/tou_onboarding_validation_report.md
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mds_tou  # noqa: E402

if __name__ == "__main__":
    sys.exit(mds_tou.main(__doc__, "configure,onboarding"))
