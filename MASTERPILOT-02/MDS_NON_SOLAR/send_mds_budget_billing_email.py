#!/usr/bin/env python3
"""BUDGET BILLING Monthly Summary for an existing BB user on <deployment> the target pilot (created by create_mds_budget_billing_user.py).

NON-SOLAR (MDS_NON_SOLAR copy of MDS_AUTOMATION): the user is an MDS non-solar premise -- SOLAR_USER FALSE,
DELIVERED = whole consumption, RECEIVED present with all-zero readings, FEED_IN 0 kWh / $0, no solar user-scope
configs, checks expect solarUser / GLOBAL.SOLAR not true and no SOLAR_ENERGY_FLOW. Plans come from the profile. B14
checks the home is NOT solar.

    configure  pilot BB keys (only if missing) + user MS section list with BB_CUMULATIVE_BALANCE
    verify     B01-B08: programs API row, BB_TrueUp_Period, web budgetBillingInfo (bbAmount/actualCost/trueUp), dashboard link
    email      rewind the MS sent-state, send MONTHLY_SUMMARY, E01-E10 (MS_BB variant, BB chart, balance insight vs the
               generated data, True-up disclaimer, bill = invoice, templating, branding)

RUN
    python3 mds_user_automation/send_mds_budget_billing_email.py --uuid <uuid>
    python3 mds_user_automation/send_mds_budget_billing_email.py --uuid <uuid> --phases verify      # web/API state only
OUTPUT  <run>/email/BB_MS_<id>.html, <run>/reports/bb_*_validation_report.md
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mds_bb  # noqa: E402

if __name__ == "__main__":
    sys.exit(mds_bb.main(__doc__, "configure,verify,email"))
