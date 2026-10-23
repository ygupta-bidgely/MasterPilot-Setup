"""HER email (eHER) for MDS users on target pilot the target pilot -- shared engine for
create_mds_single_fuel_her_user.py and create_mds_dual_fuel_her_user.py.

What "HER email" is on this platform
    The paper HER (EventNames.HER) never emails: HEREventProcessor.processUser returns at
    DELIVERY_MODE_PAPER_NOT_AVAILABLE unless the user has DeliveryModes.Paper, and every path below it
    renders a PDF. The emailed HER ("eHER") is the NEIGHBOURHOOD_COMPARISON (SHC) email with an
    itemization block: EmailDataProvider:2141 sets isEHER when the NC section list contains ITEMIZATION.
    NVE RESI <source pilot> runs exactly that in production (db/277-Drop2/data-server/critical/NVE-resi-missing-configs.sh),
    and the section lists below are NVE's, plus the GenAI NBI reco block fed by the mocked interactions.

    single fuel  per-fuel NC email, sections from NEIGHBOURHOOD_COMPARISON.OPT_IN/OPT_OUT
    dual fuel    ONE combined NC email (enable_combined_dual_fuel_email), sections read ONLY from
                 NEIGHBOURHOOD_COMPARISON.DF_EMAILS.OPT_IN/OPT_OUT, state written with NO measurement type

Phases (--phases, default all)
    user          create a brand-new MDS user through mds_common (preflight..aggregate, no MS email);
                  skipped with --uuid / --manifest (an existing MDS run)
    shc           SHC data (read-only): the home must sit in a target neighbourhood that has NMetrics for the cycle
                  month. SHCDataProvider reads NMetrics (C / GAS_C) from Cassandra; only the warehouse ncRunner writes
                  them. --shc-bootstrap / --shc-publish are UNAVAILABLE: creating clusters or publishing NMetrics needs
                  a reviewed, fixed ETL job protocol, and the kit's `envlogs run` is read-only. Both flags stop with
                  UNAVAILABLE before any network or S3 call (the former host procedure is kept, as history only, in
                  the docstrings of shc_bootstrap / publish_nmetrics)
    configure     USER-scope only: NVE section list, NC delivery modes, $ display, combined-DF flags, GenAI NBI
                  config, data_point_threshold=0, and the DF summary strings that are absent on <deployment>
    interactions  mock GenAI NBI interactions per fuel for the processed cycle (POST v1.0/nbi/interactions/userUpdate)
    trigger       rewind the NC sent-state, wait out the config cache, one NotificationsProcessorEvent
    validate      fetch the rendered email and check SHC, itemization, fuels, interactions, state, $ and templating
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import mds_common as mc
import mds_target
from dataclasses import dataclass

from mds_common import Run, SetupError, api, log, payload, status_of, step

EVENT = "NEIGHBOURHOOD_COMPARISON"
NTYPE = None                       # = the target pilot (set by _retarget)
HER_PHASES = ("user", "shc", "configure", "interactions", "trigger", "validate", "paper")
SHC_BUCKET = None                  # the profile's email.provisioner.target.dw_bucket
SHC_PREFIX = "shc-2"
SHC_FEATURE_YAML = SHC_TEMPLATE_YAML = None
ENVLOGS = mc.SKILL_DIR / "scripts/envlogs"         # read-only log access (pass/grep) for diagnosis only
READER_KEYSPACE = None             # the profile's email.cassandra.keyspace (where HER reads NMetrics)
SHC_UNAVAILABLE = ("UNAVAILABLE: SHC bootstrap/NMetrics publish needs a reviewed fixed ETL job protocol; envlogs run is "
                   "read-only. Use a target whose SHC clusters and NMetrics already exist for the cycle month, or have the "
                   "environment owner run the ETL jobs; nothing was written")


@dataclass
class Check:
    id: str
    name: str
    status: str = "PASS"
    detail: str = ""


def _sec(name: str, mandatory: bool = False) -> dict:
    return {"emailTemplateSection": name, "mandatory": mandatory}


# NVE <source pilot> single-fuel NC list, + the GenAI NBI reco block (it reads the mocked interactions; eHER path).
SINGLE_SECTIONS = [_sec("TEASER"), _sec("HEADER"), _sec("GREETING"), _sec("SHC_SUMMARY"), _sec("NEW_SERVICE"),
                   _sec("SHC_BARGRAPH_COMPARISON", True), _sec("SHC_COMPARISON_WIDGET", True), _sec("ITEMIZATION"),
                   _sec("ITEMIZATION_EXPLAINER"), _sec("GEN_AI_NBI_RECO_ELEMENT_SECTION"), _sec("ENERGY_SAVINGS_TIPS"),
                   _sec("FEEDBACK"), _sec("FOOTER")]
# NVE <source pilot> combined dual-fuel list (DF_EMAILS), + the GenAI NBI reco block.
DF_SECTIONS = [_sec("TEASER"), _sec("HEADER_V4"), _sec("GREETING_V2"), _sec("NEW_SERVICE_V2"), _sec("DF_SHC_SUMMARY"),
               _sec("DF_SHC_BARGRAPH_COMPARISON", True), _sec("SHC_COMPARISON_WIDGET_V2", True), _sec("DF_ITEMIZATION"),
               _sec("GEN_AI_NBI_RECO_ELEMENT_SECTION"), _sec("SPONSORED_RECO_V2"), _sec("ENERGY_SAVINGS_TIPS_V2"),
               _sec("CTA_WITH_DES_SECTION_V2"), _sec("FEEDBACK_V4"), _sec("FOOTER_V4")]

# EmailSHCSummarySectionContentProvider:265-267 reads these with a bare .get(id).getText() before the per-fuel
# loop; they are absent on <deployment>, and one missing id kills the combined render for BOTH fuels.
DF_SUMMARY_STRINGS = {
    "com.bidgely.cloud.email.shc.summary3.great.dualFuel.text": "Keep up the great work!",
    "com.bidgely.cloud.email.shc.summary3.improve.dualFuel.text":
        "Take a look at the tips below to see how you can improve your energy efficiency!",
    "com.bidgely.cloud.email.shc.summary.equal.dualFuel.text": "same amount on ${fuelType} as ${averageOrEfficient} user",
}
# String ids the NC sections dereference without a null check that are absent on <deployment> the target pilot: any one of them
# NPEs in the emailer and the whole email is dropped (no row, no drop reason). Texts are the production en_US ones.
#   SHC_SUMMARY, solar branch (EmailSummaryStringsProvider.getSolarTemplateDataMap)  -- VSE 20018 ships ""
#   ITEMIZATION (ItemizationSectionContentProvider: AMI solar title, survey/feedback nudges)
HER_REQUIRED_STRINGS = {
    "com.bidgely.cloud.email.greeting.summary.web.url.ms.text.solar": "",
    "com.bidgely.cloud.email.itemization.title.text.ami": "Your total estimated energy use by appliance",
    "com.bidgely.cloud.email.sentences.itemization.surveyNudge.disclaimer":
        "Data accuracy may vary due to limited information. Enhance accuracy by updating your profile.",
    "com.bidgely.cloud.email.sentences.itemization.surveyNudge.cta.text": "Personalize home profile",
    "com.bidgely.cloud.email.sentences.itemization.feedbackNudge.disclaimer":
        "AI-generated content may be inaccurate. Please share your feedback to help us serve you better.",
}

# Mock GenAI NBI interactions (InteractionInputDTO). Titles are unique so validate can find them in the email.
# insight_type COMMUNITY: the combined dual fuel SHC email (GenAINBIRecoElementDataProvider.getInteractionsForCombinedFuel)
# keeps ONLY community interactions; the single fuel eHER path (getGenAINBIRecoElementDataDefault) does not filter by type.
INTERACTIONS = {
    "ELECTRIC": [
        ("Run laundry and the dishwasher at midday",
         "Your solar panels produce the most between 10am and 3pm. Running big loads then uses your own power "
         "instead of buying it from the grid.", "Laundry", 59, 6.50, "COMMUNITY",
         "Your evening usage is higher than similar solar homes."),
        ("Set your thermostat to 78°F when you are home",
         "Each degree warmer on your cooling setpoint can trim cooling costs by about 3%.", "Cooling", 4, 9.25,
         "COMMUNITY", "Cooling is your largest electric appliance category."),
        ("Unplug idle electronics with a smart power strip",
         "Devices on standby draw power all day. A smart strip cuts them off automatically.", "Always On", 1, 3.10,
         "COMMUNITY", "Your always-on usage is above the neighbourhood median."),
    ],
    "GAS": [
        ("Lower your water heater to 120°F",
         "Most water heaters ship at 140°F. Dropping to 120°F reduces standby heat loss and scalding risk.",
         "Water Heating", 7, 5.40, "COMMUNITY", "Water heating is your largest gas appliance category."),
        ("Install a low-flow showerhead",
         "A WaterSense showerhead uses 2 gallons per minute or less and cuts hot water use by up to 25%.",
         "Water Heating", 7, 4.20, "COMMUNITY", "Your hot water usage is above similar homes."),
        ("Replace your furnace filter this season",
         "A clean filter keeps your furnace running efficiently through the heating season.", "Space Heating", 3,
         3.80, "COMMUNITY", "Space heating drives your winter gas bill."),
    ],
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def fuels_of(run: Run) -> tuple[str, ...]:
    return tuple(run.m.get("fuels") or ("ELECTRIC",))


def is_combined(fuels: tuple[str, ...]) -> bool:
    return "GAS" in fuels


def cycle(run: Run) -> dict:
    """The cycle the NC pass processes: the last COMPLETED one (notification `today` = last interval datapoint,
    and mds_common stops RAW at the last complete interval, so the open cycle is never processed)."""
    return run.m["cycles"][-1]


def nc_state_path(uuid: str, fuel: str | None) -> str:
    return f"notification/notificationStatus/{uuid}/{mc.HID}/{EVENT}/" + (f"{fuel}/" if fuel else "") + "Email"


def epoch_s(ts: Any) -> int:
    """utility_notifications timestamps are epoch MILLIseconds; comparing them to a seconds cut-off lets every older
    row through (a previous send's email then 'passes' for this one)."""
    v = int(ts or 0)
    return v // 1000 if v > 10 ** 11 else v


def current_rows(rows: list[dict], since: int) -> list[dict]:
    out = [n for n in rows if n.get("notificationType") == EVENT and n.get("deliveryMode") == "Email"
           and epoch_s(n.get("generationTimestamp")) >= since]
    return sorted(out, key=lambda n: -epoch_s(n.get("generationTimestamp")))


def nc_rows(uuid: str, since: int) -> list[dict]:
    r = payload(api("GET", f"2.1/utility_notifications/users/{uuid}?from={since - 3600}&to={int(time.time()) + 3600}",
                    allow=(404,))) or {}
    return current_rows(r.get("notificationsList") or [], since)


def home(uuid: str) -> dict:
    return payload(api("GET", f"meta/users/{uuid}/homes/{mc.HID}")) or {}


def nmetrics(nid: int, metric: str, t0: int, t1: int) -> list[dict]:
    r = api("GET", f"nhs/{NTYPE}/ids/{nid}/nmetrics/{metric}/data?startTime={t0 - 40 * 86400}&endTime={t1}",
            allow=(400, 404, 500))
    return r if isinstance(r, list) else []


def month_rows(rows: list[dict], cyc: dict, whole_home: int) -> list[dict]:
    """NMetrics rows keyed by month begin; NMetricCalculator reads the months the cycle overlaps."""
    lo, hi = cyc["startTs"] - 32 * 86400, cyc["endTs"]
    return [x for x in rows if lo <= int(x.get("time") or 0) < hi and x.get("applianceType") in (whole_home, None)]


def envlogs(args: list[str], timeout: int = 1200) -> str:
    import subprocess
    if not ENVLOGS.exists():
        raise SetupError(f"{ENVLOGS} not found: install the config-fail-testcase-debugger skill (bidgely-debug-kit) or run the host commands by hand")
    env = mc.kit_env()             # the deployment's profile (debug-kit 1.2.0 tools have no built-in environment)
    p = subprocess.run([str(ENVLOGS), *args], capture_output=True, text=True, timeout=timeout, env=env)
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode:
        raise SetupError(f"envlogs {args[0]} failed ({p.returncode}): {out[-600:]}")
    return out


# ---------------------------------------------------------------------------
# phase: shc
# ---------------------------------------------------------------------------
def shc_bootstrap(run: Run, a: argparse.Namespace, members: list[str]) -> int:
    """UNAVAILABLE: raises before any network, S3 or host call.

    HISTORICAL REFERENCE ONLY (not executed; environment-specific, unverified, kept so the procedure is not lost):
    the former mock SHC 2.0 cluster (Confluence PM/1455620097, <deployment> flavour) wrote three cluster files under
    s3://<dw_bucket>/shc-2/algo-outputs/outputs/{diff,total,cluster_info}/pilot_id=<p>/ntype=<p>/batch_id=<b>/, copied
    the shc2_configs_<template pilot>.yaml feature file, then ran on the target's datacube/etl host (as the service
    user, through `envlogs run`) the nhoodServices OneJar NeighbourhoodPostProcessingRunner (-ntype -shcDiffPath
    -totalOutputPath -shcClusterInfoPath -featureMetaDataFilePath ...) and the datacubejobs ncRunnerStarburst script,
    then publish_nmetrics. Running arbitrary java/sudo commands on a shared ETL host is not a reviewed, fixed job
    protocol, and `envlogs run` is a read-only diagnosis channel, so this path is disabled."""
    if not (SHC_BUCKET and SHC_FEATURE_YAML and SHC_TEMPLATE_YAML):     # configuration first (it used to follow S3 writes)
        raise SetupError(SHC_UNAVAILABLE + " (and the profile states no email.provisioner.target.dw_bucket / "
                                           "shc_template_pilot)")
    raise SetupError(SHC_UNAVAILABLE)


def publish_nmetrics(run: Run, a: argparse.Namespace) -> None:
    """UNAVAILABLE: raises before any network or host call.

    HISTORICAL REFERENCE ONLY (not executed; environment-specific, unverified): the former publish copied
    host/NcPublishTarget.java to the etl host and ran it with the datacubejobs OneJar against READER_KEYSPACE for the
    ntype, from the month before the target cycle. Disabled for the same reason as shc_bootstrap."""
    raise SetupError(SHC_UNAVAILABLE)


def shc(run: Run, a: argparse.Namespace) -> None:
    m, uuid, fuels = run.m, run.m["uuid"], fuels_of(run)
    if getattr(a, "shc_bootstrap", False) or getattr(a, "shc_publish", False):
        raise SetupError(SHC_UNAVAILABLE)                  # before any network call
    step(f"SHC neighbourhood + NMetrics for {uuid}")
    clusters = api("GET", f"nhs/{NTYPE}/ids") or []
    if not clusters:
        raise SetupError(f"ntype {NTYPE} has no SHC cluster: the HER email needs an existing neighbourhood with NMetrics "
                         f"({SHC_UNAVAILABLE})")
    usable = [c for c in clusters if c.get("attributeMap")]
    if not usable:
        raise SetupError(f"ntype {NTYPE} clusters have no attributeMap: {json.dumps(clusters)[:300]}")
    h = home(uuid)
    nid = int(h.get("defNhoodId") or 0)
    if a.nid:
        want = a.nid
    elif nid and any(c["nid"] == nid for c in usable):
        want = nid
    else:
        want = usable[0]["nid"]
    if str(h.get("defNTypeId")) != str(NTYPE):
        raise SetupError(f"home defNTypeId={h.get('defNTypeId')} (expected {NTYPE})")
    if nid != want:
        # SHC_VERSION_V2 never assigns a launchpad user; the same V2 gate keeps this write from being overwritten
        api("POST", f"meta/users/{uuid}/homes/{mc.HID}", {"defNhoodId": str(want)})
        run.mutate("home_defNhoodId", before=nid, after=want)
        nid = int(home(uuid).get("defNhoodId") or 0)
        if nid != want:
            raise SetupError(f"wrote defNhoodId={want} but the home reads back {nid}")
    log(f"  home in ntype={NTYPE} nid={nid}")
    cyc = cycle(run)
    have = {}
    for fuel in fuels:
        metric, whole = ("C", 0) if fuel == "ELECTRIC" else ("GAS_C", 17)
        rows = month_rows(nmetrics(nid, metric, cyc["startTs"], cyc["endTs"]), cyc, whole)
        have[fuel] = [{k: r.get(k) for k in ("time", "applianceType", "avgConsumption", "avgCost", "userCount",
                                             "ntile25", "ntile75")} for r in rows]
        log(f"  NMetrics {metric} {cyc['start']}..{cyc['end']}: {len(rows)} month rows "
            f"{json.dumps(have[fuel])[:220]}", "INFO" if rows else "WARN")
    m["shc"] = {"ntype": NTYPE, "nid": nid, "nmetrics": have}
    run.save()
    run.evidence("shc_nmetrics", m["shc"])
    missing = [f for f in fuels if not have[f]]
    if missing:
        raise SetupError(f"no NMetrics for {missing} in cluster {nid} for {cyc['start']}: SHCDataProvider drops the "
                         f"fuel with CANNOT_COMPUTE_NEIGHBOURHOOD. The metrics must exist in {READER_KEYSPACE} (the keyspace "
                         f"HER reads) before this run ({SHC_UNAVAILABLE})")


# ---------------------------------------------------------------------------
# phase: configure
# ---------------------------------------------------------------------------
def her_configs(fuels: tuple[str, ...]) -> dict[str, dict[str, str]]:
    combined = is_combined(fuels)
    secs = json.dumps(DF_SECTIONS if combined else SINGLE_SECTIONS)
    key = f"{EVENT}.DF_EMAILS" if combined else EVENT
    subs = {f"{EVENT}.delivery_modes": '["Email"]'}
    for f in fuels:
        subs[f"{EVENT}.{f}.delivery_modes"] = '["Email"]'
    out = {"email_template_sections_config": {f"{key}.OPT_IN": secs, f"{key}.OPT_OUT": secs},
           "event_subscriptions": subs,
           "email_moderation": {"displayValueIdentifier": "COST", "block_shc_email_without_invoice": "true"},
           # absent at every level -> getGenAINBIConfigs returns null and the reco block NPEs
           "genai_nbi_config": {"number_of_reco_to_be_shown_in_email": "3", "filter_repeated_insights": "false"},
           # few mock-cluster members: relax the minimum sample behind avg/efficient (runbook step 5)
           "neighbourhood_comparison": {"data_point_threshold": "0"}}
    # GEN_AI_NBI_RECO_ELEMENT_SECTION: absent at every level on the target pilot -> every string id is null and the section NPEs
    # in the emailer (whole email dropped). Values = production db/260-Drop1/data-server/genAINBINewSectionsConfigs.sh
    out.update(GENAI_RECO_SECTION_CONFIGS)
    if not combined:
        # V1 FEEDBACK has no default title id: feedback.vm references an unset $feedbackTitle and Velocity aborts the
        # whole render ("Unable to render template"). V4 (combined layout) hard-codes its title.
        out["email_feedback_section_configs"] = {"title_text_content": "com.bidgely.cloud.email.content.feedback.title.v3"}
        # ITEMIZATION_EXPLAINER "See how appliance breakdown works!" is GLOBAL-configured to a Google Drive file;
        # keep the NVE section but point it at the white-label dashboard (generic branding, H10)
        out["email_itemization_explainer_section_configs"] = {"video_url": f"{mc.DASHBOARD_URL}/dashboard"}
    if combined:
        # the combined pass asks for delivery modes with measurementType=null -> legacy SubscriptionService, which
        # reads event_subscriptions.<event>; without it: "already processed all delivery modes ... skipping"
        out[f"event_subscriptions.{EVENT}"] = {"delivery_modes": '["Email"]'}
        out["email_moderation"].update({
            "enable_dual_fuel_emails": "true", "enable_combined_dual_fuel_email": "true",
            "eligible_fuels_for_dual_fuel_email": '["ELECTRIC","GAS"]', "use_secondary_fuels_for_shc_email": "true",
            "displayValueIdentifierDualFuelCombinedEmail": "cost",
            "consumption_unit_type_map": '{"ELECTRIC":"kWh","GAS":"CCF"}'})
    return out


def string_present(uuid: str, rid: str) -> bool:
    r = payload(api("GET", f"2.1/stringResources/user/{uuid}/resource/{rid}?locale=en_US", allow=(400, 404, 500)))
    return bool(r) and status_of(r) == 200 and json.dumps(r) not in ("{}", "null")


def configure(run: Run) -> None:
    fuels, uuid = fuels_of(run), run.m["uuid"]
    step(f"CONFIGURE eHER (reference eHER layout) at USER scope for {uuid}")
    mc.apply_user_configs(run, mc.user_configs(fuels))          # solar/hybrid + $ itemization (idempotent)
    mc.apply_user_configs(run, her_configs(fuels))
    strings = {**HER_REQUIRED_STRINGS, **(DF_SUMMARY_STRINGS if is_combined(fuels) else {})}
    for rid, text in strings.items():
        if string_present(uuid, rid):
            log(f"  string {rid} present")
        else:
            mc.put_user_string(run, uuid, rid, text)
    for rid, url in GENERIC_LINK_STRINGS.items():
        cur = (payload(api("GET", f"2.1/stringResources/user/{uuid}/resource/{rid}?locale=en_US",
                           allow=(400, 404, 500))) or {}).get(rid)
        if cur == url:
            log(f"  link {rid} = {url}")
        else:
            mc.put_user_string(run, uuid, rid, url)
    run.m["herConfigured"] = True
    run.save()


# ---------------------------------------------------------------------------
# phase: interactions
# ---------------------------------------------------------------------------
def interaction_body(uuid: str, fuel: str, bill_start: int, nonce: int = 0) -> dict:
    """nonce makes the ids unique per send: the processor drops ids already recorded in the interaction-usage table for
    the cycle (genAINBIInteractionUsageDao), so a resend with the same ids renders an empty reco block."""
    items = []
    for rank, (title, text, app_name, app_id, usd, itype, insight) in enumerate(INTERACTIONS[fuel], 1):
        items.append({"interaction_rank": rank, "interaction_id": f"MDS_HER_{fuel[0]}_{rank:02d}" + (f"_{nonce}" if nonce else ""),
                      "insight_action": {
                          "insight": {"raw_insight_text": insight, "insight_text": insight, "insight_type": itype,
                                      "insight_appliance_name": app_name, "insight_justification": insight,
                                      "ranking_justification": "Ranked by savings potential.",
                                      "insight_evidence": "Billing-cycle itemization."},
                          "action": {"action_title": title, "action_text": text, "action_frequency": "MONTHLY",
                                     "appliance_id": app_id, "action_appliance_name": app_name,
                                     "saving_potential_usd": usd,
                                     "saving_potential_usd_calculation": "Estimated from this cycle's itemization",
                                     "saving_potential_usd_source": "Bidgely Analytics"}}})
    return {"uuid": uuid, "pilot_id": str(mc.PILOT_ID), "bill_start_timestamp": bill_start,
            "computed_timestamp": int(time.time() * 1000), "source": "GENAI", "nbi_type": "EE",
            "measurement_type": fuel, "interactions": items,
            "aggregate_potential_savings_usd": round(sum(x[4] for x in INTERACTIONS[fuel]), 2)}


def interactions(run: Run) -> None:
    uuid, fuels, cyc = run.m["uuid"], fuels_of(run), cycle(run)
    step(f"INTERACTIONS mock GenAI NBI for cycle {cyc['start']} ({cyc['startTs']})")
    got = {}
    for fuel in fuels:
        body = interaction_body(uuid, fuel, cyc["startTs"], nonce=int(time.time()))
        (run.dir("api_evidence") / f"interactions_{fuel}.json").write_text(json.dumps(body, indent=2))
        r = api("POST", "v1.0/nbi/interactions/userUpdate", body, allow=(400, 500))
        if status_of(r) != 200:
            raise SetupError(f"interaction insert {fuel} -> HTTP {status_of(r)}: {json.dumps(r)[:300]}")
        back = payload(api("GET", f"v1.0/nbi/interactions/user/{uuid}?billCycleStart={cyc['startTs']}"
                                  f"&measurementType={fuel}")) or []
        got[fuel] = len(back) if isinstance(back, list) else 0
        log(f"  {fuel}: inserted {len(body['interactions'])}, read back {got[fuel]}",
            "INFO" if got[fuel] >= len(body["interactions"]) else "WARN")
    run.m["interactions"] = {"billCycleStart": cyc["startTs"], "readBack": got,
                             "titles": {f: [x[0] for x in INTERACTIONS[f]] for f in fuels}}
    run.m["lastConfigWriteAt"] = time.time()
    run.save()


# ---------------------------------------------------------------------------
# phase: trigger
# ---------------------------------------------------------------------------
def trigger(run: Run, a: argparse.Namespace) -> None:
    uuid, fuels = run.m["uuid"], fuels_of(run)
    step(f"TRIGGER {EVENT} ({'combined dual fuel' if is_combined(fuels) else 'single fuel'})")
    if "interactions" not in a.phase_list:
        interactions(run)          # fresh ids, otherwise a resend drops the already-sent ones (see interaction_body)
    # blockEmailIfAlreadySent: `sent` (a timestamp) >= current cycle start blocks. Zero it for this run.
    # The validation window opens HERE, not at the SQS send: an emailer retry of an earlier failed send can land
    # between the reset and our message (it then renders with this run's configs and our message is blocked as
    # already sent). Anything generated before the reset is a previous send and never counts.
    run.m["herWindowStart"] = int(time.time())
    for fuel in ([None] if is_combined(fuels) else list(fuels)):
        path = nc_state_path(uuid, fuel)
        before = api("GET", path, allow=(404, 500))
        api("POST", path, {"billStartTs": 0, "sent": 0, "sentCount": 0, "sentWithSurveyCount": 0})
        run.mutate("nc_state_reset", path=path, before=before)
        log(f"  state {fuel or 'COMBINED'} reset (was {json.dumps(payload(before))[:120]})")
    mc.config_cache_settle(run, a.settle_config)
    sqs = mc.aws(a.aws_profile).client("sqs")
    sent_at = int(time.time())
    body = json.dumps({"userId": uuid, "homeOrdinal": mc.HID, "metaData": {"EventName": EVENT}})
    mc.sqs_send(run, sqs, mc.NOTIF_QUEUE, body, EVENT)
    run.m["herTriggeredAt"] = sent_at
    run.save()


# ---------------------------------------------------------------------------
# phase: validate
# ---------------------------------------------------------------------------
def money(text: str) -> list[float]:
    return [float(x.replace(",", "")) for x in re.findall(r"\$\s?(-?[\d,]+(?:\.\d+)?)", text)]


# Generic branding: NVE's layout, but every link points at this environment's white-label dashboard (the header/footer
# are already the generic "Energy Co." ones on the target pilot). <deployment> ships placeholder targets for these two ids
# (https://www.google.com for "Complete home profile", dashboard.bidgely.com for the GenAI tips CTA), so they are
# ALWAYS written at USER scope, not only when absent.
GENERIC_LINK_STRINGS: dict = {}
# hosts a generated HER may link to (the target dashboard + the profile's link_hosts); anything else is flagged by H10
ALLOWED_LINK_HOSTS: set = set()


def _retarget(v: dict) -> None:
    from urllib.parse import urlparse
    dash = (v.get("dashboard_url") or "").rstrip("/")
    tpl = v.get("shc_template_pilot")
    globals().update(
        NTYPE=int(v["pilot"]), SHC_BUCKET=v.get("dw_bucket"), READER_KEYSPACE=v.get("keyspace"),
        SHC_FEATURE_YAML=f"s3://{v.get('dw_bucket')}/{SHC_PREFIX}/config/shc2_configs_{v['pilot']}.yaml" if v.get("dw_bucket") else None,
        SHC_TEMPLATE_YAML=f"s3://{v.get('dw_bucket')}/{SHC_PREFIX}/config/shc2_configs_{tpl}.yaml" if v.get("dw_bucket") and tpl else None,
        GENERIC_LINK_STRINGS={"com.bidgely.cloud.email.shcWidget.cta.url": f"{dash}/dashboard/survey",
                              "com.bidgely.cloud.email.gen.ai.nbi.reco.section.cta.url": f"{dash}/dashboard"} if dash else {},
        ALLOWED_LINK_HOSTS=({urlparse(dash).hostname} if dash else set()) | set(v.get("link_hosts") or ()))


def _target_view() -> dict:
    return {"pilot": NTYPE, "dw_bucket": SHC_BUCKET, "keyspace": READER_KEYSPACE}


def brand_blocklist() -> tuple[str, ...]:
    """Real-utility names the generic-branding checks flag: the profile's optional
    email.provisioner.target.brand_blocklist (a list of strings), lower-cased. Default: empty (nothing assumed)."""
    t = getattr(mds_target, "_TARGET", None)
    raw = t.section("email.provisioner.target.brand_blocklist") if t is not None else None
    return tuple(str(x).strip().lower() for x in (raw or []) if str(x).strip())


_GENAI = "com.bidgely.cloud.email.gen.ai.nbi.reco.section."
GENAI_RECO_SECTION_CONFIGS = {
    "gen_ai_nbi_reco_element_section_configs": {
        "color_palette_id": "gen_ai_nbi_reco_element_section_configs_palette",
        "reco_section_element_icon_url": _GENAI + "icon.url",
        "reco_section_element_title_text": _GENAI + "title.text",
        "reco_section_element_default_title_text": _GENAI + "default.title.text",
        "min_saving_amount_for_title_text": "50",
        "reco_section_element_sub_text": _GENAI + "sub.text",
        "reco_section_element_tag_text": _GENAI + "tag.text",
        "reco_section_element_tag_min_saving_amount": "10",
        "reco_section_element_insight_reason_header_text": _GENAI + "insight.reason.header.text",
        "reco_section_element_cta_text": _GENAI + "cta.text",
        "reco_section_element_cta_url": _GENAI + "cta.url",
        "reco_section_element_disclaimer_text": _GENAI + "disclaimer.text"},
    "gen_ai_nbi_reco_element_section_configs_palette": {
        "color1": "#FFFFFF", "color2": "#333333", "color3": "#E6F3E9", "color4": "#018425",
        "color5": "#FEEEE6", "color6": "#F85A01", "color7": "#FFFFFF", "color8": "#e4194b"},
}

FUEL_UNITS = {"ELECTRIC": ("kWh",), "GAS": ("CCF", "therms", "therm")}


def labelled_usage(text: str, units: tuple[str, ...]) -> dict[str, list[float]]:
    """{'you'|'average'|'efficient': [values]} from 'You 617 kWh'-style bar-graph labels in one fuel's unit."""
    out: dict[str, list[float]] = {}
    unit_re = "|".join(re.escape(u) for u in units)
    for label, num in re.findall(rf"\b(You|Average|Efficient)\s+(-?[\d,]+(?:\.\d+)?)\s*(?:{unit_re})\b", text):
        out.setdefault(label.lower(), []).append(float(num.replace(",", "")))
    return out


def has_money(vals: list[float], want: float | None, tol: float = 1.0) -> bool:
    return want is not None and any(abs(v - want) <= tol for v in vals)


def shc_api(uuid: str, fuel: str) -> dict:
    r = payload(api("GET", f"v2.0/pdf-report/users/{uuid}/shc-bargraph-and-widget?locale=en_US&measurement-type={fuel}"
                           f"&report-type=HER_MONTHLY_REPORT&section-name=SHC_GRAPH_AND_WIDGET", allow=(400, 404, 500)))
    try:
        return (r.get("shcGraphDataList") or [{}])[-1].get("applianceInfo", {}).get("17") or {}
    except AttributeError:
        return {}


def validate(run: Run, a: argparse.Namespace) -> list[Check]:
    m, uuid, fuels = run.m, run.m["uuid"], fuels_of(run)
    combined = is_combined(fuels)
    since = int(m.get("herWindowStart") or m.get("herTriggeredAt") or 0)
    step(f"VALIDATE {EVENT} email for {uuid}")
    checks: list[Check] = []

    def add(cid: str, name: str, status: str, detail: str) -> None:
        checks.append(Check(cid, name, status, detail))
        log(f"  {cid} {status:4} {name}: {detail}", "INFO" if status != "FAIL" else "ERROR")

    rows: list[dict] = []
    deadline = time.time() + a.her_wait
    while time.time() < deadline:
        rows = nc_rows(uuid, since)
        if rows:
            break
        time.sleep(a.poll)
    if not rows:
        add("H01", "email generated", "FAIL",
            f"no {EVENT} Email row since {datetime.fromtimestamp(since)} after {a.her_wait}s. Debug: "
            f"envlogs pass notifications {uuid}  /  envlogs pass emailer {uuid}")
        return finish(run, checks)
    row = rows[0]
    body = payload(api("GET", f"2.1/utility_notifications/notifications/{row['notificationId']}")) or {}
    htmlb = body.get("notificationBody") or ""
    f = run.dir("email") / f"HER_email_{row['notificationId']}.html"
    f.write_text(htmlb)
    text = mc.email_text(htmlb)
    m["herEmail"] = {k: row.get(k) for k in ("notificationId", "notificationTitle", "status", "deliveryDestination",
                                             "generationTimestamp")} | {
        "variant": (row.get("eventData") or {}).get("email_variant_code"), "file": str(f)}
    run.save()
    add("H01", "email generated", "PASS",
        f"{row.get('notificationTitle')!r} status={row.get('status')} to={row.get('deliveryDestination')} -> {f.name}")

    vals = money(text)
    low = text.lower()
    # H02: the neighbour comparison (the SHC part of the HER). Operands from the same provider the email uses.
    for fuel in fuels:
        info = shc_api(uuid, fuel)
        you, avg, eff = info.get("userCost"), info.get("averageSHCCost"), info.get("efficientSHCCost")
        found = [n for n, v in (("you", you), ("average", avg), ("efficient", eff)) if has_money(vals, v)]
        # the combined DF bar graph shows usage, not $: match the labelled "You/Average/Efficient <n> <unit>" values
        cons = {"you": info.get("userCons"), "average": info.get("averageSHCCons"), "efficient": info.get("efficientSHCCons")}
        shown = labelled_usage(text, FUEL_UNITS[fuel])
        found_cons = [n for n, v in cons.items() if v is not None and any(abs(x - v) <= 1 for x in shown.get(n, []))]
        best, unit = (found, "$") if len(found) >= len(found_cons) else (found_cons, FUEL_UNITS[fuel][0])
        status = "PASS" if len(best) == 3 else ("WARN" if best else "FAIL")
        add(f"H02{fuel[0]}", f"SHC comparison {fuel}", status,
            f"api you=${you}/{cons['you']} avg=${avg}/{cons['average']} efficient=${eff}/{cons['efficient']}; "
            f"matched in email ({unit}): {best or 'none'}")
    words = ("similar homes", "efficient", "average")
    add("H03", "SHC wording", "PASS" if all(w in low for w in words) else "FAIL",
        f"{[w for w in words if w in low]} of {list(words)}")
    # H04: itemization (what makes it the eHER)
    item_words = [w for w in ("appliance", "cooling", "heating", "lighting", "always on", "laundry", "cooking",
                              "entertainment", "refrigeration", "water heat", "others") if w in low]
    add("H04", "itemization block", "PASS" if len(item_words) >= 3 else "FAIL", f"appliance words {item_words}")
    if combined:
        both = ("electric" in low) and bool(re.search(r"\bgas\b", low))
        total = "total spent on electricity and gas" in low
        add("H05", "both fuels in ONE email", "PASS" if both and total else ("WARN" if both else "FAIL"),
            f"electric={'electric' in low} gas={bool(re.search(r'\bgas\b', low))} combined-total-line={total}")
    titles = [t for fuel in fuels for t in (m.get("interactions", {}).get("titles", {}).get(fuel) or [])]
    shown = [t for t in titles if t.lower().replace("°", "") in low.replace("°", "")]
    add("H06", "mock interactions rendered", "PASS" if shown else "FAIL", f"{len(shown)}/{len(titles)} titles: {shown[:3]}")
    raw = re.findall(r"\$\{[A-Za-z0-9_.]+\}", htmlb)
    nulls = re.findall(r'href="null"|>\s*null\s*<', htmlb)
    add("H07", "templating clean", "PASS" if not raw and not nulls else "FAIL",
        f"unrendered {sorted(set(raw))[:5]} null-links {len(nulls)}")
    add("H08", "shown in $", "PASS" if len(vals) >= 3 else "FAIL", f"{len(vals)} $ values, first {vals[:6]}")
    path = nc_state_path(uuid, None if combined else fuels[0])
    st = payload(api("GET", path, allow=(404, 500))) or {}
    sent = int((st or {}).get("sent") or 0) if isinstance(st, dict) else 0
    add("H09", "sent-state", "PASS" if sent >= max(since, cycle(run)["startTs"]) else "FAIL",
        f"{path.split(uuid)[1]} -> {json.dumps(st)[:160]} (window opened {since}, SQS at {m.get('herTriggeredAt')})")
    issues = branding_issues(htmlb, text)
    add("H10", "generic branding + links", "FAIL" if issues else "PASS",
        "; ".join(issues[:6]) if issues else
        (f"no utility brand text {list(brand_blocklist())[:3]}" if brand_blocklist() else
         "brand text NOT checked (no email.provisioner.target.brand_blocklist in the profile)")
        + f"; links/images only on {sorted(ALLOWED_LINK_HOSTS)[:3]}...")
    return finish(run, checks)


def branding_issues(htmlb: str, text: str) -> list[str]:
    """NVE layout, generic branding: no real-utility names, and every link on the white-label dashboard or a generic
    host (placeholder targets such as google.com are what <deployment> ships when a link string was never set)."""
    from urllib.parse import urlparse
    low = text.lower()
    brands = brand_blocklist()
    out = [f"brand text {b!r}" for b in brands if b in low]
    for u in re.findall(r'href="(https?://[^"]+)"', htmlb):
        host = urlparse(u).netloc.lower()
        if host not in ALLOWED_LINK_HOSTS:
            out.append(f"link to {host}")
    for u in re.findall(r'<img[^>]+src="(https?://[^"]+)"', htmlb):
        host = urlparse(u).netloc.lower()
        img_rx = mds_target.target_section("email.provisioner.target.image_host_regex")   # profile; unset -> host not checked
        if (img_rx and not re.search(str(img_rx), host)) or any(b.replace(" ", "") in u.lower() for b in brands):
            out.append(f"image {u[:80]}")
    return sorted(set(out))


def finish(run: Run, checks: list[Check]) -> list[Check]:
    run.m["herValidation"] = [{"id": c.id, "name": c.name, "status": c.status, "detail": c.detail} for c in checks]
    run.save()
    lines = [f"# MDS HER email (eHER) -- {run.m.get('uuid')}", "",
             f"email: {run.m.get('herEmail', {}).get('file', '-')}", "",
             "| id | check | status | detail |", "|---|---|---|---|"]
    lines += [f"| {c.id} | {c.name} | {c.status} | {c.detail.replace('|', '/')} |" for c in checks]
    (run.dir("reports") / "her_validation_report.md").write_text("\n".join(lines) + "\n")
    worst = "FAIL" if any(c.status == "FAIL" for c in checks) else "PASS"
    log(f"HER VALIDATION {worst}: " + " ".join(f"{c.id}={c.status}" for c in checks))
    return checks


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def find_manifest(uuid: str, root: Path) -> Path | None:
    hits = sorted(root.glob("*/run_*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in hits:
        try:
            if json.loads(p.read_text()).get("uuid") == uuid:
                return p
        except (OSError, json.JSONDecodeError):
            continue
    return None


def parse(doc: str, default_out: str, argv: list[str] | None) -> tuple[argparse.Namespace, list[str]]:
    p = argparse.ArgumentParser(description=doc, formatter_class=argparse.RawDescriptionHelpFormatter,
                                allow_abbrev=False)
    p.add_argument("--phases", default="all", help=f"comma list of {','.join(HER_PHASES)} or all")
    p.add_argument("--uuid", help="existing MDS user (its mds_common manifest is looked up under results/)")
    p.add_argument("--manifest", help="existing mds_common run manifest")
    p.add_argument("--output-dir", default=default_out)
    p.add_argument("--token-file")
    p.add_argument("--aws-profile")
    p.add_argument("--zip", default="11691")
    p.add_argument("--nid", type=int, help=f"neighbourhood id to use (default: the first {NTYPE} cluster)")
    p.add_argument("--shc-bootstrap", action="store_true",
                   help="UNAVAILABLE (stops before any network call): SHC cluster creation needs a reviewed ETL job protocol")
    p.add_argument("--shc-members", default="", help="unused (SHC bootstrap is UNAVAILABLE)")
    p.add_argument("--skip-ncrunner", action="store_true", help="no effect (SHC bootstrap / ncRunner are UNAVAILABLE)")
    p.add_argument("--shc-publish", action="store_true",
                   help="UNAVAILABLE (stops before any network call): NMetrics publish needs a reviewed ETL job protocol")
    p.add_argument("--settle-config", type=int, default=60, help="seconds between the last config write and the trigger")
    p.add_argument("--her-wait", type=int, default=600)
    p.add_argument("--poll", type=int, default=20)
    a, rest = p.parse_known_args(argv)
    a.phase_list = list(HER_PHASES) if a.phases == "all" else [x.strip() for x in a.phases.split(",") if x.strip()]
    bad = [x for x in a.phase_list if x not in HER_PHASES]
    if bad:
        p.error(f"unknown phases {bad}")
    a.shc_members = [x.strip() for x in a.shc_members.split(",") if x.strip()]
    if (a.uuid or a.manifest) and "user" in a.phase_list:
        a.phase_list.remove("user")
    return a, rest


def new_run_manifest(out_dir: Path, before: set[Path]) -> Path | None:
    """The manifest of the run mds_common just created: the ONE run dir that did not exist before it started (never
    "the newest manifest", which can belong to a concurrent run)."""
    created = [d for d in set(out_dir.glob("run_*")) - before if (d / "manifest.json").exists()]
    return created[0] / "manifest.json" if len(created) == 1 else None


def main(doc: str, fuels: tuple[str, ...], default_out: str, argv: list[str] | None = None) -> int:
    argv = mds_target.bootstrap(argv)          # PROFILE_REQUIRED before anything touches a target
    a, rest = parse(doc, default_out, argv)
    if a.shc_bootstrap or a.shc_publish:          # before the user phase, any token or any network call
        print(SHC_UNAVAILABLE, file=sys.stderr)
        return 2
    results = Path(__file__).resolve().parent / "results"
    manifest = Path(a.manifest) if a.manifest else None
    if a.uuid and not manifest:
        manifest = find_manifest(a.uuid, results)
        if not manifest:
            print(f"no mds_common manifest for {a.uuid} under {results}; pass --manifest", file=sys.stderr)
            return 2
    if "user" in a.phase_list:
        out_dir = Path(a.output_dir).expanduser()
        base = ["--phases", "preflight,generate,upload,configure,aggregate", "--output-dir", str(out_dir), "--zip", a.zip]
        if a.token_file:
            base += ["--token-file", a.token_file]
        if a.aws_profile:
            base += ["--aws-profile", a.aws_profile]
        before = set(out_dir.glob("run_*"))
        rc = mc.main(doc, fuels, str(out_dir), base + rest)
        manifest = new_run_manifest(out_dir, before)
        if rc != 0 or manifest is None:
            print(f"user creation did not complete (exit {rc}, run {manifest.parent if manifest else '?'}); "
                  f"fix it and resume with --manifest <run>/manifest.json --phases shc,configure,interactions,"
                  f"trigger,validate", file=sys.stderr)
            return rc or 2
    if not manifest:
        print("nothing to do: pass --uuid/--manifest or include the user phase", file=sys.stderr)
        return 2
    run = Run(manifest.parent, json.loads(manifest.read_text()))
    if a.uuid and run.m.get("uuid") != a.uuid:
        print(f"{manifest} belongs to {run.m.get('uuid')}, not {a.uuid}", file=sys.stderr)
        return 2
    if tuple(run.m.get("fuels") or ()) != fuels:
        print(f"{manifest} is a {run.m.get('fuels')} user; this script is for {list(fuels)}", file=sys.stderr)
        return 2
    mc.set_uuid_tag(run.m["uuid"])
    mc._LOG_FH = open(run.dir("logs") / "run.log", "a")
    log(f"HER email run for {run.m['uuid']} fuels {fuels} phases {a.phase_list} dir {run.out}")
    checks = None
    try:
        mc._TOKEN = mc.get_token(a.token_file)
        if "shc" in a.phase_list:
            shc(run, a)
        if "configure" in a.phase_list:
            configure(run)
        if "interactions" in a.phase_list:
            interactions(run)
        if "trigger" in a.phase_list:
            trigger(run, a)
        if "validate" in a.phase_list:
            checks = validate(run, a)
        if "paper" in a.phase_list:            # paper HER: a PDF + report URL, a separate flow sharing only the user
            import mds_paper_her
            checks = (checks or []) + mds_paper_her.paper(run, a, fuels, Check)
        return 1 if checks and any(c.status == "FAIL" for c in checks) else 0
    except SetupError as exc:
        log(str(exc), "ERROR")
        run.m.setdefault("errors", []).append(str(exc))
        run.save()
        return 2
    finally:
        if mc._LOG_FH:
            mc._LOG_FH.close()
            mc._LOG_FH = None
