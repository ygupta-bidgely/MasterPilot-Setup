#!/usr/bin/env python3
"""TOU COACHING for an existing TOU user on <deployment> the target pilot: the Monthly Summary whose variant is
MS_TOU_COACHING (TOU_USAGE / TOU_BILL_SAVING_RECO rendered). There is no separate TOU coaching event.

NON-SOLAR (MDS_NON_SOLAR copy of MDS_AUTOMATION): the user is an MDS non-solar premise -- SOLAR_USER FALSE,
DELIVERED = whole consumption, RECEIVED present with all-zero readings, FEED_IN 0 kWh / $0, no solar user-scope
configs, checks expect solarUser / GLOBAL.SOLAR not true and no SOLAR_ENERGY_FLOW. Plans come from the profile. No
solar chart flag or decouple on a non-solar home.

    Default (system mode): sends NOTHING. Re-checks the Monthly Summary the SYSTEM generated in the
    notifications-enabled aggregation pass (C01-C08: variant, TOU usage vs RAW On-Peak kWh, chart values, reco,
    bill = invoice, templating, branding) and diagnoses + classifies a missing email.
    --manual-trigger  legacy: rewind the MS sent-state and send NotificationsProcessorEvent {MONTHLY_SUMMARY}
                      (--solar-tou-chart decouple|flag|auto applies around that manual send)

RUN
    python3 mds_user_automation/send_mds_tou_coaching_email.py --uuid <uuid>
OUTPUT  <run>/email/TOU_COACHING_MS_<id>.html, <run>/reports/tou_coaching_validation_report.md
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mds_tou  # noqa: E402

if __name__ == "__main__":
    sys.exit(mds_tou.main(__doc__, "configure,coaching"))
