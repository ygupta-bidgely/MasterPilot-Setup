#!/usr/bin/env python3
"""HER EMAIL (eHER) for a DUAL FUEL (ELECTRIC + GAS) MDS user on target pilot the target pilot.

NON-SOLAR (MDS_NON_SOLAR copy of MDS_AUTOMATION): the user is an MDS non-solar premise -- SOLAR_USER FALSE,
DELIVERED = whole consumption, RECEIVED present with all-zero readings, FEED_IN 0 kWh / $0, no solar user-scope
configs, checks expect solarUser / GLOBAL.SOLAR not true and no SOLAR_ENERGY_FLOW. Plans come from the profile.
Paper DFHER/HER layouts are also written for the .AMI suffix a non-solar home resolves.

ONE combined email covers both fuels (enable_combined_dual_fuel_email at USER scope): the sections are read ONLY
from NEIGHBOURHOOD_COMPARISON.DF_EMAILS.OPT_IN/OPT_OUT and the sent-state is written with NO measurement type.
Layout = NVE RESI <source pilot> production: TEASER, HEADER_V4, GREETING_V2, NEW_SERVICE_V2, DF_SHC_SUMMARY,
DF_SHC_BARGRAPH_COMPARISON*, SHC_COMPARISON_WIDGET_V2*, DF_ITEMIZATION, GEN_AI_NBI_RECO_ELEMENT_SECTION,
SPONSORED_RECO_V2, ENERGY_SAVINGS_TIPS_V2, CTA_WITH_DES_SECTION_V2, FEEDBACK_V4, FOOTER_V4 (* mandatory).
Shown in $; the three DF summary strings missing on <deployment> are seeded at USER scope.

The combined path ALSO sidesteps the per-fuel GAS defect on DCX solar homes (EmailDataProvider:980 NPE).

GATES this script clears (each one silently dropped the email on 2026-09-29):
  * SHC data in the keyspace HER reads. ncRunner on the etl host publishes NMetrics to Cassandra `bidgely`, while
    <deployment> notifications and the API read `bidgely_<deployment>`. --shc-bootstrap and --shc-publish republish
    ntype the target pilot into bidgely_<deployment> (host/NcPublish<deployment>.java), then check /nhs/.../nmetrics.
  * event_subscriptions.NEIGHBOURHOOD_COMPARISON.delivery_modes=["Email"] (USER). The combined pass asks for delivery
    modes with no fuel; without this: "already processed all delivery modes ... skipping".
  * Strings absent on <deployment>, seeded at USER scope: the solar SHC_SUMMARY link, the AMI itemization title, the
    itemization survey/feedback nudges, and the DF summary strings.
  * Mock GenAI interactions are COMMUNITY type (the combined path keeps only community ones), with fresh ids on every
    send (ids already sent for the cycle are dropped). The trigger re-posts them.

PHASES  user -> shc -> configure -> interactions -> trigger -> validate   (see mds_her.py)

RUN
    export BIDGELY_TOKEN=<target bearer token>            # or --token-file (0600)
    export BOPS_PW_FILE=$TMPDIR/bops_pw_$USER                            # jumphost sudo password (for etl host steps)
    python3 mds_user_automation/create_mds_dual_fuel_her_user.py                       # brand-new user
    python3 mds_user_automation/create_mds_dual_fuel_her_user.py --uuid <uuid>          # existing MDS DF user
    python3 mds_user_automation/create_mds_dual_fuel_her_user.py --uuid <uuid> --shc-bootstrap   # no SHC cluster yet
    python3 mds_user_automation/create_mds_dual_fuel_her_user.py --uuid <uuid> --phases shc,trigger,validate \
        --shc-publish                                         # cluster + ncRunner done, but the nmetrics API is empty
    python3 mds_user_automation/create_mds_dual_fuel_her_user.py --uuid <uuid> --phases trigger,validate   # resend

OUTPUT  the user's mds_common run dir: email/HER_email_<id>.html, reports/her_validation_report.md,
        api_evidence/ (NMetrics, interactions), logs/run.log
Exit code: 0 = all checks PASS/WARN, 1 = a check FAILed, 2 = setup/wait error.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mds_her  # noqa: E402

if __name__ == "__main__":
    sys.exit(mds_her.main(__doc__, ("ELECTRIC", "GAS"), str(Path(__file__).resolve().parent / "results" / "dual_fuel")))
