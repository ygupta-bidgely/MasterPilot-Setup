#!/usr/bin/env python3
"""TOU Rate Promotion end to end on target pilot the target pilot (MDS): a NEW single fuel user, then the rate
comparison, the promotion config + text, and the Monthly Summary with the TOU_RATE_PROMOTION section (variant
MS_TOU_PROMOTION), validated.

NON-SOLAR (MDS_NON_SOLAR copy of MDS_AUTOMATION): the user is an MDS non-solar premise -- SOLAR_USER FALSE,
DELIVERED = whole consumption, RECEIVED present with all-zero readings, FEED_IN 0 kWh / $0, no solar user-scope
configs, checks expect solarUser / GLOBAL.SOLAR not true and no SOLAR_ENERGY_FLOW. Plans come from the profile. The
new user is non-solar; the promotion section has no solar gate.

    user            new ELECTRIC AMI solar D&R user on the flat plan 905 (launchpad files, ingestion, disagg,
                    aggregation); the automatic Monthly Summary is held so the promotion MS is the only one
    ratecomparison  NVE rate_comparison config at USER scope, map 905 -> the priceable TOU plans cheapest for this
                    user (906 today), RC API (waited for), gates RC01-RC08
    configure       user-scope MS section list with TOU_RATE_PROMOTION + the section text computed from the result
    email           MS resend, row + HTML saved, checks P01-P11

RUN
    export BIDGELY_TOKEN=<target token>          # or --token-file (0600)
    python3 mds_user_automation/create_mds_tou_rate_promotion_user.py
    python3 mds_user_automation/create_mds_tou_rate_promotion_user.py --phases user           # user only
    python3 mds_user_automation/send_mds_tou_rate_promotion_email.py --uuid <uuid>            # rest, existing user
    options: --rate-plan <flat plan name>  --map 905:906  --max-saving-pct 100  --plan-display-name "..."
             (+ any mds_common user-creation option, e.g. --zip)
OUTPUT  results/tou_promotion/run_*/: input_files/, email/TOU_PROMOTION_MS_*.html,
        reports/{mds_validation_report,rate_comparison_report,tou_rate_promotion_validation_report}.md, logs/
Exit code: 0 = all checks PASS/WARN, 1 = a check FAILed, 2 = setup error.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import send_mds_tou_rate_promotion_email as promo  # noqa: E402

if __name__ == "__main__":
    sys.exit(promo.main(default_phases="all"))
