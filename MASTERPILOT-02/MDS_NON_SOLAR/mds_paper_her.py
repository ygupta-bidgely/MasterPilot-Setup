"""Paper HER (EventNames.HER): a PDF and a report URL, never an email -- the `paper` phase of the MDS HER scripts.

NON-SOLAR (MDS_NON_SOLAR copy): a non-solar AMI home resolves HER_<REPORT>.<fuel>.AMI layouts (a DEFAULT-only layout
rendered no report in the Hawk non-solar runs), so the NBI-free ladder is written for the AMI suffix as well.

HEREventProcessor only posts a PDFGenerationEvent; the pdf-generation daemon renders the report, writes
s3://bidgely-pdf-reports-<environment>/<pilot>/<ReportType>/default/<ReportType>_<pilot>_<account>_<ts>_0_1_V2.pdf,
the HER/<fuel>/Paper state, and a Paper utility_notifications row whose notificationBody is the report URL.

Proven on the target pilot on 2026-09-29 (HER_MONTHLY_REPORT_the target pilot_1838523978_...pdf). The gates, in the order they bit:
  * schedule    HERScheduleManager reads the schedule at PILOT level only. the target pilot carries HER_MONTHLY_REPORT and the
                seasonal rows per fuel; DFHER_MONTHLY_REPORT | ELECTRIC,GAS was added (pilot write, additive).
  * layouts     DCX gives every user HER_MONTHLY_REPORT.<fuel>.SOLAR orders that ALL contain EE_NBI. <deployment> has
                no PAPER_NBI asset rows, so every NBI (and HER ITEMIZATION, which needs 1-2 NBI actions) fails
                validateNBI ("NBI is null or not have any properties") and no order validates -> no report.
                The DCX orders are kept first; NBI-free orders are appended at USER scope:
                HEADER, ENERGY_FLOW, SHC_GRAPH_AND_WIDGET, BAR_CHART, QR_CODE, FOOTER (then shorter ones).
  * renderer    ITEMIZATION_TOP_APPLIANCES validates in the processor but the homeEnergyReport page never finishes
                loading with it, so the pdf-generation daemon logs "Received null PDF" and writes nothing (bisected).
  * nbi run     report_type_nbi_run_mapping has only HER_MONTHLY_REPORT|HER; DFHER gets |HER at USER scope.
  * branding    the GLOBAL welcome letter (HER.WLC.PARA_3 / HER.WLC.V2.WELCOME_MSG.PARA_2) names a real utility's
                programme site; both are overridden at PILOT scope with generic text (only if a name from the
                profile's optional email.provisioner.target.brand_blocklist is found; empty list -> never).
"""
from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import mds_common as mc
import mds_target
from mds_common import Run, SetupError, api, log, payload, step

PDF_BUCKET = PROFILE_BUCKET = None        # the profile's email.provisioner.target.pdf_bucket / profile_bucket


def _retarget(v: dict) -> None:
    globals().update(PDF_BUCKET=v.get("pdf_bucket"), PROFILE_BUCKET=v.get("profile_bucket"))


def _target_view() -> dict:
    return {"pdf_bucket": PDF_BUCKET, "profile_bucket": PROFILE_BUCKET}
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "paper_her_interactions.json"
DF_KEY = "ELECTRIC,GAS"
ANY = "(?s).*"


def _s(*names: str) -> list[dict]:
    return [{"herTemplateSection": n, "checksMap": {}} for n in names]


# NBI-free orders, richest first. Appended AFTER the pilot/DCX orders, so a pilot that does have PAPER_NBI assets
# still gets its EE_NBI layout. ENERGY_FLOW only validates for solar homes; the orders without it cover the rest.
# ITEMIZATION_TOP_APPLIANCES is deliberately absent: on <deployment> it VALIDATES in the processor (and its pdf-report
# API returns 200) but the homeEnergyReport web page never finishes loading with it, so headless Chrome times out
# and the daemon logs "Received null PDF ... Not writing to S3" (bisected on 2026-09-29, three renders).
FALLBACK_ORDERS = [
    _s("HEADER", "ENERGY_FLOW", "SHC_GRAPH_AND_WIDGET", "BAR_CHART", "QR_CODE", "FOOTER"),
    _s("HEADER", "SHC_GRAPH_AND_WIDGET", "BAR_CHART", "QR_CODE", "FOOTER"),
    _s("HEADER", "SHC_GRAPH_AND_WIDGET", "FOOTER"),
]
# sections that validate but break the homeEnergyReport page render on <deployment> (null PDF); orders carrying them
# are removed from the user's layouts, including ones an earlier version of this script appended
RENDER_BREAKING = {"ITEMIZATION_TOP_APPLIANCES"}
# the dual fuel report: ENERGY_FLOW is single-fuel data, so the DF ladder starts without it
DF_FALLBACK_ORDERS = [o for o in FALLBACK_ORDERS if not any(s["herTemplateSection"] == "ENERGY_FLOW" for s in o)]

GENERIC_WELCOME = ("These reports help you better understand your energy use so you can save money and live better. "
                   "Get energy-saving ideas and learn about incentives for home upgrades on your online energy portal.")
WELCOME_IDS = ("HER.WLC.PARA_3", "HER.WLC.V2.WELCOME_MSG.PARA_2")
APPLIANCE_LABELS = ("Cooling", "Heating", "Lighting", "Refrigeration", "Water Heating", "Entertainment", "Laundry",
                    "Cooking", "Always On", "Pool Pump", "Electric Vehicle", "Space Heating")


def brand_blocklist() -> tuple[str, ...]:
    """Real-utility/programme names the PDF branding check (P07) and the welcome-letter override look for: the
    profile's optional email.provisioner.target.brand_blocklist, lower-cased. Default: empty (nothing assumed)."""
    t = getattr(mds_target, "_TARGET", None)
    raw = t.section("email.provisioner.target.brand_blocklist") if t is not None else None
    return tuple(str(x).strip().lower() for x in (raw or []) if str(x).strip())



def report_type(fuels: tuple[str, ...]) -> str:
    return "DFHER_MONTHLY_REPORT" if len(fuels) > 1 else "HER_MONTHLY_REPORT"


def schedule_rows(pilot: int) -> list[dict]:
    r = payload(api("GET", f"v2.0/her-schedule/pilot/{pilot}", allow=(404,)))
    return r if isinstance(r, list) else []


def ensure_schedule(run: Run, fuels: tuple[str, ...]) -> None:
    """PILOT write, additive and only when the row is missing (HERScheduleManager has no user-level schedule)."""
    rtype = report_type(fuels)
    wanted = [(rtype, DF_KEY if len(fuels) > 1 else fuels[0])]
    wanted += [("HER_MONTHLY_REPORT", f) for f in fuels]          # the single-fuel fallback of a DF report
    rows = schedule_rows(mc.PILOT_ID)
    have = {(r.get("reportType"), r.get("measurementType")) for r in rows}
    missing = [w for w in dict.fromkeys(wanted) if w not in have]
    if not missing:
        log(f"  schedule present: {sorted(w for w in dict.fromkeys(wanted))}")
        return
    ref = rows[0] if rows else {"startTimeEpoch": int(time.time()) - 30 * 86400,
                                "endTimeEpoch": int(time.time()) + 5 * 365 * 86400}
    body = [{"startTimeEpoch": ref["startTimeEpoch"], "endTimeEpoch": ref["endTimeEpoch"],
             "minNumberOfBillingCycles": 1, "maxNumberOfBillingCycles": 1, "maxNumberOfNotification": 300,
             "reportType": t, "measurementType": m} for t, m in missing]
    (run.dir("api_evidence") / "her_schedule_before.json").write_text(json.dumps(rows, indent=1))
    log(f"  PILOT WRITE her-schedule {PILOT_TAG()} += {missing}", "WARN")
    api("POST", f"v2.0/her-schedule/pilot/{mc.PILOT_ID}", body)
    run.mutate("PILOT_her_schedule", added=missing, before=rows)
    now = {(r.get("reportType"), r.get("measurementType")) for r in schedule_rows(mc.PILOT_ID)}
    if any(w not in now for w in missing):
        raise SetupError(f"her-schedule POST returned but {missing} do not read back")


def PILOT_TAG() -> str:  # noqa: N802 -- log label
    return f"pilot {mc.PILOT_ID}"


def ensure_generic_welcome(run: Run) -> None:
    """PILOT-scope string override, only when the resolved welcome letter names a real utility/programme."""
    for rid in WELCOME_IDS:
        cur = (payload(api("GET", f"2.1/stringResources/pilot/{mc.PILOT_ID}/resource/{rid}?locale=en_US",
                           allow=(400, 404, 500))) or {}).get(rid) or ""
        if not any(b in cur.lower() for b in brand_blocklist()):
            continue
        log(f"  PILOT WRITE string {rid}: real-utility wording -> generic", "WARN")
        mc.put_user_string(run, str(mc.PILOT_ID), rid, GENERIC_WELCOME)
        run.mutate("PILOT_string", resource=rid, before=cur, after=GENERIC_WELCOME)


def user_config_vals(uuid: str, ctype: str) -> dict[str, str]:
    return {k: v.get("val") for k, v in (mc.read_config("user", uuid, ctype) or {}).items()}


def layout_updates(existing: dict[str, str], fuels: tuple[str, ...]) -> dict[str, str]:
    """her_template_sections_config values: the user's current (DCX/pilot) orders first, then the NBI-free ladder.
    Keys follow <ReportType>.<mTypes>.<suffix>; on a DCX pilot the suffix is the priority cluster ref (SOLAR on
    the target pilot) and must match exactly, so every suffix the user already resolves is written, plus DEFAULT."""
    suffixes = {"DEFAULT", "AMI"} | {k.split(".")[-1] for k in existing if k.split(".")[0].endswith("REPORT")}
    targets = [(f"HER_MONTHLY_REPORT.{f}", FALLBACK_ORDERS) for f in fuels]
    if len(fuels) > 1:
        targets.append((f"DFHER_MONTHLY_REPORT.{DF_KEY}", DF_FALLBACK_ORDERS))
    out = {}
    for base, ladder in targets:
        for sfx in sorted(suffixes):
            key = f"{base}.{sfx}"
            try:
                cur = json.loads(existing.get(key) or "[]")
            except json.JSONDecodeError:
                cur = []
            keep = [o for o in cur if o not in ladder
                    and not any(sec.get("herTemplateSection") in RENDER_BREAKING for sec in o)]
            orders = keep + ladder
            if orders != cur:
                out[key] = json.dumps(orders, separators=(",", ":"))
    return out


def configure_paper(run: Run, fuels: tuple[str, ...]) -> None:
    uuid, rtype = run.m["uuid"], report_type(fuels)
    step(f"PAPER HER configure ({rtype}) for {uuid}")
    ensure_schedule(run, fuels)
    ensure_generic_welcome(run)
    subs = user_config_vals(uuid, "event_subscriptions.HER")
    if "Paper" not in (subs.get("delivery_modes") or ""):
        mc.apply_user_configs(run, {"event_subscriptions.HER": {"delivery_modes": '["Paper"]'}})
    her = user_config_vals(uuid, "her_moderation")
    upd: dict[str, str] = {}
    if str(her.get("enable_her_v2", "")).lower() != "true":
        upd["enable_her_v2"] = "true"
    mapping = [e for e in (her.get("report_type_nbi_run_mapping") or "").split(",") if e.strip()]
    for t in dict.fromkeys([rtype, "HER_MONTHLY_REPORT"]):
        if not any(e.split("|")[0] == t for e in mapping):
            mapping.append(f"{t}|HER")
    if ",".join(mapping) != (her.get("report_type_nbi_run_mapping") or ""):
        upd["report_type_nbi_run_mapping"] = ",".join(mapping)
    if len(fuels) > 1 and not her.get("max_dfher_cycle_gap_in_seconds"):
        upd["max_dfher_cycle_gap_in_seconds"] = "604800"
    if upd:
        mc.apply_user_configs(run, {"her_moderation": upd})
    layouts = layout_updates(user_config_vals(uuid, "her_template_sections_config"), fuels)
    if layouts:
        mc.apply_user_configs(run, {"her_template_sections_config": layouts})
    log(f"  layouts: {sorted(layouts) or 'already carry the NBI-free ladder'}")
    upload_nbi_profiles(run, fuels)


def upload_nbi_profiles(run: Run, fuels: tuple[str, ...]) -> None:
    """Interaction profiles for EE_NBI at the key InteractionProfileHandler.getLatestProfile lists. On <deployment>
    they are found but rejected (no PAPER_NBI assets); uploaded anyway so a pilot with assets gets its NBI layout."""
    uuid = run.m["uuid"]
    fx = json.loads(FIXTURE.read_text())
    s3 = mc.aws(None).client("s3")
    batch = int(time.time())
    for dtype in dict.fromkeys([report_type(fuels), "HER_MONTHLY_REPORT"]):
        for fuel in fuels:
            key = (f"home/mode=batch/profile=interaction/uuid={uuid}/source=DISAGG/nbiRun=HER/deliveryMode=Paper/"
                   f"deliveryType={dtype}/fuelType={fuel}/batch={batch}/"
                   f"HERNBI_{random.Random(f'{uuid}{dtype}{fuel}{batch}').getrandbits(62)}_01.json")
            s3.put_object(Bucket=PROFILE_BUCKET, Key=key, Body=json.dumps({"interactions": fx[fuel]}).encode(),
                          ContentType="application/json")
    run.mutate("paper_nbi_profiles", batch=batch)
    log(f"  NBI interaction profiles uploaded (batch {batch})")


def her_state_path(uuid: str, fuel: str) -> str:
    return f"notification/notificationStatus/{uuid}/{mc.HID}/HER/{fuel}/Paper"


def trigger_paper(run: Run, a: argparse.Namespace, fuels: tuple[str, ...]) -> None:
    uuid = run.m["uuid"]
    step(f"PAPER HER trigger for {uuid}")
    run.m["paperWindowStart"] = int(time.time())
    for fuel in fuels:                   # a report already sent for this cycle is skipped; back up and clear
        path = her_state_path(uuid, fuel)
        before = payload(api("GET", path, allow=(404, 500)))
        if isinstance(before, dict) and before.get("sentCount"):
            api("POST", path, {"billStartTs": 0, "billEndTs": 0, "sentTimestamp": 0, "sentCount": 0})
            run.mutate("her_state_reset", path=path, before=before)
            log(f"  HER {fuel} state reset (was {json.dumps(before)[:140]})")
    mc.config_cache_settle(run, a.settle_config)
    sqs = mc.aws(a.aws_profile).client("sqs")
    mc.sqs_send(run, sqs, mc.NOTIF_QUEUE, json.dumps({"userId": uuid, "homeOrdinal": mc.HID,
                                                     "metaData": {"EventName": "HER"}}), "HER")
    run.m["paperTriggeredAt"] = int(time.time())
    run.save()


# ---------------------------------------------------------------------------
# validation: the PDF against the data the report is built from
# ---------------------------------------------------------------------------
def pdf_report_api(uuid: str, path: str, section: str, fuel: str, rtype: str) -> Any:
    return payload(api("GET", f"v2.0/pdf-report/users/{uuid}/{path}?locale=en_US&measurement-type={fuel}"
                              f"&report-type={rtype}&section-name={section}", allow=(400, 404, 500)))


def find_pdf(account: str, since: int, wait: int, poll: int) -> dict | None:
    s3 = mc.aws(None).client("s3")
    deadline = time.time() + wait
    while True:
        hits = []
        for rtype in ("DFHER_MONTHLY_REPORT", "HER_MONTHLY_REPORT"):
            pre = f"{mc.PILOT_ID}/{rtype}/default/{rtype}_{mc.PILOT_ID}_{account}_"
            for o in s3.list_objects_v2(Bucket=PDF_BUCKET, Prefix=pre).get("Contents") or []:
                if o["LastModified"].timestamp() >= since:
                    hits.append({"key": o["Key"], "reportType": rtype, "size": o["Size"],
                                 "at": int(o["LastModified"].timestamp())})
        if hits or time.time() > deadline:
            return max(hits, key=lambda h: h["at"]) if hits else None
        time.sleep(poll)


def pdf_text(path: Path) -> str:
    try:
        return subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True,
                              timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def validate_paper(run: Run, a: argparse.Namespace, fuels: tuple[str, ...], check_cls: Any) -> list:
    uuid = run.m["uuid"]
    step(f"PAPER HER validate for {uuid}")
    checks: list = []
    add = lambda cid, name, st, det: checks.append(check_cls(cid, name, st, det))  # noqa: E731
    header = pdf_report_api(uuid, "header", "HEADER", fuels[0], report_type(fuels)) or {}
    account = str(header.get("accountId") or "").split("_", 1)[-1]
    since = int(run.m.get("paperWindowStart") or 0)
    hit = find_pdf(account, since, a.her_wait, a.poll) if account else None
    if not hit:
        add("P01", "PDF generated", "FAIL", f"no {mc.PILOT_ID}/*REPORT/default/*_{account}_*.pdf since {since} after "
            f"{a.her_wait}s. Processor side: envlogs grep notifications '{uuid}' (HERLayoutLoader 'Evaluating order', "
            f"validateNBI; 'URL generated' = a layout won). Renderer side: envlogs grep pdf '{uuid}' -- 'Received null "
            f"PDF' means the homeEnergyReport page never finished loading for the winning layout's sections")
        return finish_paper(run, checks)
    out = run.dir("paper")
    local = out / hit["key"].rsplit("/", 1)[1]
    s3 = mc.aws(None).client("s3")
    s3.download_file(PDF_BUCKET, hit["key"], str(local))
    url = s3.generate_presigned_url("get_object", Params={"Bucket": PDF_BUCKET, "Key": hit["key"]},
                                    ExpiresIn=7 * 86400)
    rows = [n for n in (payload(api("GET", f"2.1/utility_notifications/users/{uuid}?from={since - 3600}"
                                         f"&to={int(time.time()) + 3600}", allow=(404,))) or {})
            .get("notificationsList") or [] if n.get("notificationType") == "HER"
            and (int(n.get("generationTimestamp") or 0) // (1000 if int(n.get("generationTimestamp") or 0) > 10 ** 11
                                                             else 1)) >= since]
    body = (payload(api("GET", f"2.1/utility_notifications/notifications/{rows[0]['notificationId']}")) or {}) \
        if rows else {}
    report_url = body.get("notificationBody") or ""
    run.m["paperHer"] = {"s3Key": hit["key"], "file": str(local), "reportUrl": report_url, "presignedUrl": url,
                         "presignedExpires": int(time.time()) + 7 * 86400, "reportType": hit["reportType"],
                         "notificationId": rows[0]["notificationId"] if rows else None}
    run.save()
    (out / "PDF_LINK.txt").write_text(f"report URL (notification record): {report_url}\n"
                                      f"presigned download link (7 days): {url}\ns3://{PDF_BUCKET}/{hit['key']}\n")
    add("P01", "PDF generated", "PASS" if rows else "WARN",
        f"s3://{PDF_BUCKET}/{hit['key']} ({hit['size']} B); notification "
        f"{rows[0]['notificationId'] + ' ' + str(rows[0].get('status')) if rows else 'record not found yet'}")
    want = report_type(fuels)
    add("P02", "report type", "PASS" if hit["reportType"] == want else "WARN",
        f"{hit['reportType']} (asked {want}{'' if hit['reportType'] == want else ': fell back to single fuel'})")

    text = pdf_text(local)
    (out / "her_pdf.txt").write_text(text)
    flat = re.sub(r"\s+", " ", text)
    low = flat.lower()
    # P03 header identity
    ident = [str(header.get(k) or "") for k in ("userFirstName", "userAddress", "accountId", "userZip")]
    miss = [v for v in ident if v and v not in flat]
    add("P03", "header identity", "PASS" if not miss else "FAIL",
        f"name/address/account/zip {'all present' if not miss else 'missing ' + str(miss)}")
    # P04 SHC numbers per fuel, as shown (usage for solar/estimated-total reports, else $). The DF report's comparison
    # is an OR section: it shows ONE fuel's comparison, so a missing fuel is a WARN, and no fuel at all is the FAIL.
    shc_rows = []
    for fuel in fuels if want.startswith("DF") else fuels[:1]:
        shc = (pdf_report_api(uuid, "shc-bargraph-and-widget", "SHC_GRAPH_AND_WIDGET", fuel, hit["reportType"])
               or {}).get("shcGraphDataList") or [{}]
        app = (shc[-1].get("applianceInfo") or {}).get("17") or {}
        cons = [app.get("userCons"), app.get("averageSHCCons"), app.get("efficientSHCCons")]
        cost = [app.get("userCost"), app.get("averageSHCCost"), app.get("efficientSHCCost")]
        shown_c = [v for v in cons if v is not None and re.search(rf"\b{int(v)}\s*(kWh|CCF|therms?)", flat)]
        shown_d = [v for v in cost if v is not None and f"${int(v)}" in flat]
        unit, shown, vals = ("usage", shown_c, cons) if len(shown_c) >= len(shown_d) else ("$", shown_d, cost)
        diff = (vals[0] - vals[1]) if None not in vals[:2] else None
        diff_ok = diff is not None and (re.search(rf"\b{abs(int(diff))}\s*(kWh|CCF|therms?)", flat) if unit == "usage"
                                        else f"${abs(int(diff))}" in flat)
        shc_rows.append((fuel, len(shown) == 3 and bool(diff_ok),
                         f"api usage={cons} cost={cost}; PDF shows ({unit}) {shown}, you-vs-average {diff} "
                         f"{'shown' if diff_ok else 'not shown'}"))
    any_ok = any(ok for _, ok, _ in shc_rows)
    for fuel, ok, det in shc_rows:
        add(f"P04{fuel[0]}", f"SHC comparison {fuel}", "PASS" if ok else ("WARN" if any_ok else "FAIL"),
            det + ("" if ok or not any_ok else " -- this report's comparison section shows another fuel"))
    # P05 bar chart: the 13 month labels of the API series (bar heights are drawn, not text)
    bars = pdf_report_api(uuid, "bar-chart", "BAR_CHART", fuels[0], hit["reportType"]) or []
    labels = [str(b.get("intervalEndDate") or "")[:3].upper() for b in bars if isinstance(b, dict)]
    found = sum(1 for m in labels if m and m in flat)
    add("P05", "usage trend months", "PASS" if bars and found == len(labels) else ("WARN" if found else "FAIL"),
        f"{found}/{len(labels)} month labels of the bar-chart API series in the PDF; values "
        f"{[b.get('consumption') for b in bars][-3:]} (last 3) are the ones drawn")
    # P06 itemization: real appliance labels, not the word "appliances" (the header blurb always says
    # "top energy consuming appliances"). The NBI-free ladder carries no itemization section on <deployment>.
    names = [n for n in APPLIANCE_LABELS if re.search(rf"\b{re.escape(n)}\b", flat, re.I)]
    add("P06", "itemization", "PASS" if len(names) >= 3 else "WARN",
        f"appliance rows {names}" if len(names) >= 3 else
        f"no itemization block in this PDF (labels found {names}); the winning layout has no itemization section "
        "(HER ITEMIZATION needs PAPER_NBI assets; ITEMIZATION_TOP_APPLIANCES breaks the web page render)")
    # P08 energy flow (solar homes): generation + from-grid = estimated total, as the energy-flow-data API says
    if "energy flow" in low:
        ef = pdf_report_api(uuid, "energy-flow-data", "ENERGY_FLOW", fuels[0], hit["reportType"]) or {}
        vals = [ef.get("solarGeneration"), ef.get("consumption")]
        shown = [v for v in vals if v is not None and re.search(rf"\b{int(v)}\s*kWh", flat)]
        add("P08", "energy flow", "PASS" if len(shown) == 2 else "FAIL",
            f"api solar={vals[0]} grid={vals[1]} total={ef.get('estimatedConsumption')}; in PDF {shown}")
    # P07 generic branding
    blocklist = brand_blocklist()
    brands = [b for b in blocklist if b in low]
    add("P07", "generic branding", "FAIL" if brands else ("PASS" if blocklist else "WARN"),
        f"real-utility wording {brands}" if brands else
        ("no real-utility names; sender is the generic Energy Co." if blocklist else
         "NOT checked: the profile states no email.provisioner.target.brand_blocklist"))
    log(f"  PDF link: {report_url or '-'}")
    log(f"  presigned (7 days): {url[:120]}...")
    return finish_paper(run, checks)


def finish_paper(run: Run, checks: list) -> list:
    run.m["paperValidation"] = [{"id": c.id, "name": c.name, "status": c.status, "detail": c.detail} for c in checks]
    run.save()
    p = run.m.get("paperHer") or {}
    lines = [f"# MDS paper HER -- {run.m.get('uuid')}", "", f"PDF: {p.get('file', '-')}",
             f"report URL: {p.get('reportUrl', '-')}", "presigned link: see paper/PDF_LINK.txt", "",
             "| id | check | status | detail |", "|---|---|---|---|"]
    lines += [f"| {c.id} | {c.name} | {c.status} | {c.detail.replace('|', '/')} |" for c in checks]
    (run.dir("reports") / "paper_her_validation_report.md").write_text("\n".join(lines) + "\n")
    worst = "FAIL" if any(c.status == "FAIL" for c in checks) else "PASS"
    log(f"PAPER HER VALIDATION {worst}: " + " ".join(f"{c.id}={c.status}" for c in checks))
    return checks


def paper(run: Run, a: argparse.Namespace, fuels: tuple[str, ...], check_cls: Any) -> list:
    configure_paper(run, fuels)
    trigger_paper(run, a, fuels)
    return validate_paper(run, a, fuels, check_cls)
