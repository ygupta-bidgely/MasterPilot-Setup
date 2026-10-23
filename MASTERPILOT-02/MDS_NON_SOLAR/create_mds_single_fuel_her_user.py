#!/usr/bin/env python3
"""HER EMAIL (eHER) for a SINGLE FUEL MDS user on target pilot the target pilot.

NON-SOLAR (MDS_NON_SOLAR copy of MDS_AUTOMATION): the user is an MDS non-solar premise -- SOLAR_USER FALSE,
DELIVERED = whole consumption, RECEIVED present with all-zero readings, FEED_IN 0 kWh / $0, no solar user-scope
configs, checks expect solarUser / GLOBAL.SOLAR not true and no SOLAR_ENERGY_FLOW. Plans come from the profile.
Paper HER layouts are also written for the .AMI suffix a non-solar home resolves.

The HER that arrives in an inbox is the NEIGHBOURHOOD_COMPARISON (similar-home comparison) email with an
itemization block -- the paper HER (EventNames.HER) only ever renders a PDF. Layout = NVE RESI <source pilot> production:
TEASER, HEADER, GREETING, SHC_SUMMARY, NEW_SERVICE, SHC_BARGRAPH_COMPARISON*, SHC_COMPARISON_WIDGET*,
ITEMIZATION, ITEMIZATION_EXPLAINER, GEN_AI_NBI_RECO_ELEMENT_SECTION, ENERGY_SAVINGS_TIPS, FEEDBACK, FOOTER
(* mandatory). Everything is shown in $. Every config write is USER scope.

GATES this script clears (each one silently dropped the email on 2026-09-29):
  * SHC data in the keyspace HER reads. ncRunner on the etl host publishes NMetrics to Cassandra `bidgely`, while
    <deployment> notifications and the API read `bidgely_<deployment>`. --shc-bootstrap and --shc-publish republish
    ntype the target pilot into bidgely_<deployment> (host/NcPublish<deployment>.java), then check /nhs/.../nmetrics.
  * Strings absent on <deployment>, seeded at USER scope. A solar AMI home takes SHC_SUMMARY's solar branch, which
    needs ...greeting.summary.web.url.ms.text.solar; ITEMIZATION needs the AMI title and the survey/feedback nudge
    strings. Any one missing aborts the render in the emailer (NPE, no notification row).
  * email_feedback_section_configs.title_text_content (USER). Without it, V1 FEEDBACK leaves $feedbackTitle unset
    and Velocity aborts the whole email ("Unable to render template").
  * Mock GenAI interactions get fresh ids on every send (ids already sent for the cycle are dropped, including by
    attempts that later failed to render). The trigger re-posts them.

PHASES  user -> shc -> configure -> interactions -> trigger -> validate   (see mds_her.py)

RUN
    export BIDGELY_TOKEN=<target bearer token>            # or --token-file (0600)
    # brand-new user end to end (user = mds_common preflight..aggregate, ~30-40 min)
    python3 mds_user_automation/create_mds_single_fuel_her_user.py
    # existing MDS user (its manifest is found under results/)
    python3 mds_user_automation/create_mds_single_fuel_her_user.py --uuid <uuid>
    # first time on a pilot with no SHC cluster / no NMetrics: build the mock SHC 2.0 cluster and run ncRunner
    export BOPS_PW_FILE=$TMPDIR/bops_pw_$USER     # jumphost sudo password file (config-fail-testcase-debugger: envlogs setpw)
    python3 mds_user_automation/create_mds_single_fuel_her_user.py --uuid <uuid> --shc-bootstrap \
        --shc-members <uuid2>,<uuid3>
    # cluster + ncRunner already done but /nhs/the target pilot/ids/<nid>/nmetrics is empty: publish into the reader keyspace
    python3 mds_user_automation/create_mds_single_fuel_her_user.py --uuid <uuid> --phases shc,trigger,validate \
        --shc-publish
    # resend only
    python3 mds_user_automation/create_mds_single_fuel_her_user.py --uuid <uuid> --phases trigger,validate

OUTPUT  the user's mds_common run dir: email/HER_email_<id>.html, reports/her_validation_report.md,
        api_evidence/ (NMetrics, interactions), logs/run.log
Exit code: 0 = all checks PASS/WARN, 1 = a check FAILed, 2 = setup/wait error.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mds_her  # noqa: E402

if __name__ == "__main__":
    sys.exit(mds_her.main(__doc__, ("ELECTRIC",), str(Path(__file__).resolve().parent / "results" / "single_fuel")))
