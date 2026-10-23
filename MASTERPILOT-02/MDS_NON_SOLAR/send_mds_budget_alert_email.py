#!/usr/bin/env python3
"""Budget Alert email(s) for an EXISTING MDS user on <deployment> the target pilot (any create_mds_* user).

NON-SOLAR (MDS_NON_SOLAR copy of MDS_AUTOMATION): the user is an MDS non-solar premise -- SOLAR_USER FALSE,
DELIVERED = whole consumption, RECEIVED present with all-zero readings, FEED_IN 0 kWh / $0, no solar user-scope
configs, checks expect solarUser / GLOBAL.SOLAR not true and no SOLAR_ENERGY_FLOW. Plans come from the profile. The
solar solarUser decouple around the send is skipped (the home is not solar).

    configure  USER scope: event_subscriptions.BUDGET_ALERT Email; home budget amount (computed from live aggregates)
    send       75% MidCycleBudgetAlert then 100% UsageExceedBudgetAlert (--alert midcycle|exceed|both), each: budget,
               state reset, one aggregation message (the only trigger that runs the processor), wait, validate

RUN
    python3 mds_user_automation/send_mds_budget_alert_email.py --uuid <uuid>
OUTPUT  <run>/email/BA_*.html, <run>/reports/BA_SUMMARY.md
See mds_ba.py for the gate chain and why solarUser is decoupled for the send.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mds_ba  # noqa: E402

if __name__ == "__main__":
    sys.exit(mds_ba.main(__doc__, "configure,send"))
