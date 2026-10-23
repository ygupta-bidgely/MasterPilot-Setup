#!/usr/bin/env python3
"""TOU Rate Promotion for an existing MDS user on target pilot the target pilot: the TOU_RATE_PROMOTION section
of the Monthly Summary (variant MS_TOU_PROMOTION), set up like NVE UAT <source pilot>, all at USER scope.

NON-SOLAR (MDS_NON_SOLAR copy of MDS_AUTOMATION): the user is an MDS non-solar premise -- SOLAR_USER FALSE,
DELIVERED = whole consumption, RECEIVED present with all-zero readings, FEED_IN 0 kWh / $0, no solar user-scope
configs, checks expect solarUser / GLOBAL.SOLAR not true and no SOLAR_ENERGY_FLOW. Plans come from the profile. The
user phase creates a non-solar user; the promotion section has no solar gate.

PHASES
  user            (create_mds_tou_rate_promotion_user.py) new single fuel MDS user on the flat --rate-plan (905):
                  launchpad files, ingestion, disagg, aggregation; the automatic Monthly Summary is held (--hold-ms)
  ratecomparison  enable_mds_rate_comparison.py: NVE `rate_comparison` config at user scope (map translated to the target pilot
                  plan numbers + <current plan> -> 907 MDS_TOU_EVRR_D_R), the RC API (~5 min, waited for), gate checks
  configure       user-scope Monthly Summary section list = the user's DCX list with TOU_RATE_PROMOTION after the
                  itemization (TOU coaching sections out: they are for TOU users); --nve-promotion-configs also copies
                  NVE's tou_rate_promotion_section_configs values (minimum_saving_amount 10 instead of the target pilot's 5)
  email           rewind the MS sent-state, trigger the Monthly Summary (NotificationsProcessorEvent), wait for the
                  row, save the HTML exactly as sent, validate

GATES (TouRatePromotionDataProvider / TouRatePromotionSectionContentProvider, deployed code)
  current plan in rate_plans_comparison_map; RC result stored; best plan (cheapest incl. current) is TOU; savings >=
  minimum_saving_amount; savings % <= max_saving_threshold_in_percentage; rate-resources planName for the best plan;
  current plan's computedDurationBillEnd == the MS billing-cycle end (else the section is dropped, or retried while
  the invoice is younger than max_delay_days_for_promotion_section). The section has no solar gate.

CONTENT (configure phase, user-scope strings, computed per user from the stored RC result + the plans' rows)
  header     "Save ${savingAmount} a year by switching to the ${planName} plan"
  bullets    the off-peak ratio (current rate / best plan off-peak rate, written as a number: the platform prints
             "0X" for a flat current plan), the off-peak and peak hours + peak price as the engine resolves them
  paragraph  the two annual costs from the rate comparison
  The plan name is --plan-display-name (default "Time-of-Use Saver") written into the user's strings; ${planName}
  would read the pilot-level rate_resources.planName, which something on the target pilot resets to the plan code.
  --rename-plan also renames that pilot-level row.

CHECKS  P01 MS generated  P02 variant MS_TOU_PROMOTION  P03 header with the RC savings  P04 plan name
        P05 bullets rendered  P06 CTA  P07 no template leftovers  P08 generic branding  P09 off-peak ratio = computed
        P10 annual costs = RC  P11 TOU hours = best plan rows

RUN
    export BIDGELY_TOKEN=<target token>          # or --token-file
    python3 mds_user_automation/create_mds_tou_rate_promotion_user.py            # NEW user end to end (all phases)
    python3 mds_user_automation/send_mds_tou_rate_promotion_email.py --uuid <flat-plan MDS user>
    python3 mds_user_automation/send_mds_tou_rate_promotion_email.py --uuid <uuid> --phases configure,email
    python3 mds_user_automation/send_mds_tou_rate_promotion_email.py --uuid <uuid> --map 905:907,10196
OUTPUT  the user's run folder: email/TOU_PROMOTION_MS_*.html, reports/tou_rate_promotion_validation_report.md,
        reports/rate_comparison_report.md, api_evidence/rc_*.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import enable_mds_rate_comparison as erc  # noqa: E402
import mds_common as mc
import mds_target  # noqa: E402
import mds_her as mh  # noqa: E402
import mds_tou as mt  # noqa: E402
from mds_common import Run, SetupError, api, log, payload, step  # noqa: E402

HERE = Path(__file__).resolve().parent
MS = "MONTHLY_SUMMARY"
SECTION = "TOU_RATE_PROMOTION"
PHASES = ("user", "ratecomparison", "configure", "email")
OUT_DIR = HERE / "results" / "tou_promotion"
PROMO_IDS = {
    "non_tou_header_text": "com.bidgely.cloud.email.tou.rate.promotion.nontou.header.text",
    "tou_header_text": "com.bidgely.cloud.email.tou.rate.promotion.tou.header.text",
    "cta_label": "com.bidgely.cloud.email.tou.rate.promotion.cta.label.text",
}
# NVE UAT <source pilot> tou_rate_promotion_section_configs values that differ from the target pilot's pilot defaults. NVE's
# "non_tou_bullet_points_text" is misspelled there (the code reads non_tou_bullet_point_text), so it is not copied.
NVE_PROMO = {"minimum_saving_amount": "10", "display_only_for_tou_rate_plan": "true",
             "max_delay_days_for_promotion_section": "3"}


BULLET_IDS = {"tou": "com.bidgely.cloud.email.tou.rate.promotion.tou.bulletpoint.text",
              "non_tou": "com.bidgely.cloud.email.tou.rate.promotion.nontou.bulletpoint.text"}
PARAGRAPH_IDS = {"tou": "com.bidgely.cloud.email.tou.rate.promotion.tou.paragragh.text",
                 "non_tou": "com.bidgely.cloud.email.tou.rate.promotion.nontou.paragragh.text"}
HEADER_IDS = {"tou": PROMO_IDS["tou_header_text"], "non_tou": PROMO_IDS["non_tou_header_text"]}


def month_rate(rows: list[dict], month: int, tou: str | None) -> float | None:
    """DELIVERED consumption rate in `month` on a weekday: the touName row (tou) or the flat row (tou=None)."""
    for r in rows:
        if (r.get("chargeType") == "CONSUMPTION_BASED" and not r.get("isHoliday")
                and (r.get("attributes") or {}).get("data_stream", "DELIVERED") == "DELIVERED"
                and r["monthLow"] <= month <= r["monthHigh"] and r["weekLow"] <= 3 <= r["weekHigh"]
                and (r.get("touName") == tou if tou else not r.get("touName"))):
            return float(r["rate"])
    return None


def windows(rows: list[dict], month: int) -> dict[str, str]:
    """touName -> '10 PM-6 AM' as the pricing engine resolves the hours (lower bound), wrap-around merged."""
    import create_mds_nve_tou_rate_plan as rp
    cons = [r for r in rows if r.get("chargeType") == "CONSUMPTION_BASED"
            and (r.get("attributes") or {}).get("data_stream", "DELIVERED") == "DELIVERED"]
    runs = rp.runs(rp.hour_labels(cons, month, 3))
    if len(runs) > 1 and runs[0][2] == runs[-1][2] and runs[0][0] == 0 and runs[-1][1] == 24:
        runs = [(runs[-1][0], runs[0][1] + 24, runs[0][2])] + runs[1:-1]
    out: dict[str, list[str]] = {}
    for b, e, lab in runs:
        if lab:
            out.setdefault(lab, []).append(f"{rp.hour_txt(b % 24)}-{rp.hour_txt(e % 24)}")
    return {k: " and ".join(v) for k, v in out.items()}


def promotion_content(run: Run, a: argparse.Namespace) -> dict:
    """The section text for THIS user, from the stored rate comparison and the two plans' rows. The off-peak ratio is
    computed as TouRatePromotionDataProvider intends (current rate / best plan off-peak rate); the platform prices a
    flat current plan at 0 there ('Enjoy 0X cheaper', getRateForComparison reads touName/tierName rows only), so the
    number is written into the user-scope string instead of ${offPeakRatesCompareRatio}."""
    rc = run.m.get("rateComparisonCheck") or {}
    cur_n, best_n = rc.get("current"), rc.get("best")
    if not (cur_n and best_n):
        raise SetupError("no rate comparison result in the manifest: run the ratecomparison phase first")
    month = datetime.fromtimestamp(run.m["cycles"][-1]["startTs"], ZoneInfo(run.m["timezone"])).month
    cur_rows, best_rows = (mc.plan_rows_from_db(n) or mc.plan_rows_from_local_post(n) or [] for n in (cur_n, best_n))
    cur_tou = any(r.get("touName") for r in cur_rows)
    cur_rate = month_rate(cur_rows, month, "Off-Peak" if cur_tou else None)
    off, peak = month_rate(best_rows, month, "Off-Peak"), month_rate(best_rows, month, "On-Peak")
    win = windows(best_rows, month)
    if not (cur_rate and off):
        raise SetupError(f"cannot read the rates for the promotion text (current {cur_rate}, off-peak {off}; rows read: "
                         f"plan {cur_n}={len(cur_rows)}, plan {best_n}={len(best_rows)})")
    ratio = f"{cur_rate / off:.1f}".rstrip("0").rstrip(".")
    costs = {int(c["planNumber"]): c["cost"] for c in run.m["rateComparison"]["result"].get("candidates", [])}
    cur_cost, best_cost = round(costs.get(cur_n) or 0), round(costs.get(best_n) or 0)
    what = "your current off-peak rate" if cur_tou else "your current rate"
    bullets = [f"Off-peak electricity is {ratio}X cheaper than {what}: ${off:.2f} instead of ${cur_rate:.2f} per kWh",
               f"Off-peak hours: {win.get('Off-Peak', '-')} every day"
               + (f". Peak hours: {win['On-Peak']} at ${peak:.2f} per kWh" if win.get("On-Peak") and peak else ""),
               "Run the dishwasher, laundry and EV charging in off-peak hours to save the most"]
    kind = "tou" if cur_tou else "non_tou"
    # the plan name is written into the USER-scope strings: ${planName} reads rate_resources.planName, a pilot-level
    # row that something on the target pilot resets to the plan code (906 went back to MDS_TOU_RATE_PLAN_D_R on 2026-09-30)
    shown = a.plan_display_name or "${planName}"
    content = {
        HEADER_IDS[kind]: f"Save ${{savingAmount}} a year by switching to the {shown} plan",
        BULLET_IDS[kind]: json.dumps(bullets),
        PARAGRAPH_IDS[kind]: (f"Based on your last 12 months of energy use, you would have paid about ${best_cost} on "
                              f"the {shown} plan instead of ${cur_cost} on your current plan."),
    }
    facts = {"kind": kind, "month": month, "currentRate": cur_rate, "offPeak": off, "peak": peak, "ratio": ratio,
             "windows": win, "currentCost": cur_cost, "bestCost": best_cost, "saving": rc.get("saving"),
             "planName": a.plan_display_name or erc.rate_resources_name(best_n)}
    return {"strings": content, "facts": facts}


def ensure_display_name(run: Run, a: argparse.Namespace, plan: int) -> str | None:
    """${planName} is rate_resources.planName. An internal code (MDS_TOU_RATE_PLAN_D_R) reads badly in an email:
    --plan-display-name replaces it (that plan's rate_resources row only)."""
    name = erc.rate_resources_name(plan)
    rr = payload(api("GET", f"v3.0/rate-info/utilities/{mc.PILOT_ID}/{plan}/rate-resources", allow=(404, 500))) or {}
    if a.plan_display_name and name != a.plan_display_name and (not name or re.fullmatch(r"[A-Z0-9_\-]+", name)):
        body = {"planName": a.plan_display_name, "description": rr.get("description") or a.plan_display_name}
        r = api("POST", f"v3.0/rate-info/utilities/{mc.PILOT_ID}/{plan}/rate-resources", body, allow=(400, 404, 500))
        run.mutate("rate_resources_display_name", plan=plan, before=name, after=a.plan_display_name, status=mc.status_of(r))
        log(f"  rate-resources plan {plan} display name {name!r} -> {a.plan_display_name!r} (HTTP {mc.status_of(r)})")
        name = a.plan_display_name
    return name


def promotion_sections(uuid: str) -> list[dict]:
    """DCX MS list with TOU_RATE_PROMOTION right after the itemization; the TOU coaching sections removed."""
    base = [s for s in mt.dcx_ms_sections(uuid)
            if s.get("emailTemplateSection") not in (SECTION, *mt.TOU_MS_SECTIONS)]
    names = [s.get("emailTemplateSection") for s in base]
    at = next((i + 1 for i, n in enumerate(names) if n in ("ITEMIZATION_V2", "ITEMIZATION", "DF_ITEMIZATION")), len(base))
    return base[:at] + [mt.sec(SECTION)] + base[at:]


def configure(run: Run, a: argparse.Namespace) -> None:
    uuid = run.m["uuid"]
    step(f"CONFIGURE TOU Rate Promotion in the Monthly Summary (USER scope) for {uuid}")
    ms = json.dumps(promotion_sections(uuid), separators=(",", ":"))
    cfgs = {"email_template_sections_config": {f"{MS}.OPT_IN": ms, f"{MS}.OPT_OUT": ms}}
    if a.nve_promotion_configs:
        cfgs[erc.PROMO_CT] = NVE_PROMO
    mc.apply_user_configs(run, cfgs)
    best = (run.m.get("rateComparisonCheck") or {}).get("best")
    if best and a.rename_plan:
        ensure_display_name(run, a, best)
    content = promotion_content(run, a)
    name = content["facts"]["planName"]
    for rid, text in content["strings"].items():
        for entity in (f"{uuid}.{MS}", f"{uuid}.{MS}.ELECTRIC", uuid):
            mc.put_user_string(run, entity, rid, text)
    run.m["touPromotionContent"] = {**content["facts"], "planName": name, "strings": content["strings"]}
    run.save()
    log(f"  promotion text: {content['facts']}")
    for key, rid in PROMO_IDS.items():
        cur = (payload(api("GET", f"2.1/stringResources/user/{uuid}/resource/{rid}?locale=en_US",
                           allow=(400, 404, 500))) or {}).get(rid)
        log(f"  string {key}: {cur!r}" if cur else f"  string {key} ({rid}) MISSING: the section renders it blank",
            "INFO" if cur else "WARN")
    run.m["touPromotionConfigured"] = {"at": time.time(), "nvePromotionConfigs": a.nve_promotion_configs}
    run.save()


def email(run: Run, a: argparse.Namespace) -> list[mt.Check]:
    m, uuid = run.m, run.m["uuid"]
    step(f"TOU RATE PROMOTION = Monthly Summary with {SECTION} (variant MS_TOU_PROMOTION) for {uuid}")
    checks: list[mt.Check] = []

    def add(cid, name, status, detail):
        checks.append(mt.Check(cid, name, status, detail))
        log(f"  {cid} {status:4} {name}: {detail}", "ERROR" if status == "FAIL" else "INFO")

    rc = m.get("rateComparisonCheck") or {}
    mc.rewind_ms_state(run, "ELECTRIC")
    mc.config_cache_settle(run, a.settle_config)
    since = int(time.time())
    mc.sqs_send(run, mc.aws(a.aws_profile).client("sqs"), mc.NOTIF_QUEUE,
                json.dumps({"userId": uuid, "homeOrdinal": mc.HID, "metaData": {"EventName": MS}}), MS)
    m["touPromotionTriggeredAt"] = since
    run.save()
    rows = mt.wait_rows(uuid, MS, since, a.email_wait, a.poll)
    if not rows:
        add("P01", "Monthly Summary generated", "FAIL", f"no {MS} Email row after {a.email_wait}s; logs below")
        mt.diagnose(run, MS)
        return report(run, checks)
    row = rows[0]
    htmlb, text, f = mt.save_email(run, row, "TOU_PROMOTION_MS")
    variant = (row.get("eventData") or {}).get("email_variant_code")
    m["touPromotionEmail"] = {k: row.get(k) for k in ("notificationId", "notificationTitle", "deliveryDestination",
                                                      "generationTimestamp")} | {"variant": variant, "file": str(f)}
    add("P01", "Monthly Summary generated", "PASS", f"{row.get('notificationTitle')!r} -> {f.name}")
    codes = [x.strip() for x in str(variant or "").split(",") if x.strip()]
    add("P02", "variant MS_TOU_PROMOTION", "PASS" if "MS_TOU_PROMOTION" in codes else "FAIL", f"email_variant_code={variant}")
    low = text.lower()
    saving = rc.get("saving")
    vals = mh.money(text)
    header = re.search(r"save[^.]{0,40}\$\s?[\d,]+[^.]{0,120}", text, re.I)
    add("P03", "promotion header with the RC savings",
        "PASS" if header and (saving is None or mh.has_money(vals, saving, tol=1.0)) else "FAIL",
        f"{header.group(0)[:140] if header else 'no savings header'}; RC savings ${saving}")
    best = rc.get("best")
    name = erc.rate_resources_name(best) if best else None
    facts = m.get("touPromotionContent") or {}
    shown_name = facts.get("planName") or name
    add("P04", "best plan name", "PASS" if shown_name and shown_name.lower() in low else "FAIL",
        f"plan {best} shown as {shown_name!r}")
    bullets = [x for x in ("off-peak", "cheaper", "peak hours") if x in low]
    add("P05", "promotion bullets rendered", "PASS" if len(bullets) >= 2 and "${" not in htmlb else "FAIL", f"{bullets}")
    ratio = re.search(r"([\d.]+)x cheaper", low)
    want = facts.get("ratio")
    add("P09", "off-peak ratio", "PASS" if ratio and want and ratio.group(1) == want else "FAIL",
        f"shown {ratio.group(0) if ratio else None!r}, expected {want}X (current ${facts.get('currentRate')} / "
        f"off-peak ${facts.get('offPeak')}); '0X' = the platform's flat-plan defect")
    costs_ok = facts and mh.has_money(vals, facts.get("currentCost"), 1.0) and mh.has_money(vals, facts.get("bestCost"), 1.0)
    add("P10", "annual costs = rate comparison", "PASS" if costs_ok else "FAIL",
        f"expected ${facts.get('bestCost')} (best) and ${facts.get('currentCost')} (current); $ shown {vals[:12]}")
    wins = facts.get("windows") or {}
    win_ok = all(w.lower().replace(" ", "") in low.replace(" ", "") for w in wins.values())
    add("P11", "TOU hours = best plan rows", "PASS" if wins and win_ok else "FAIL", f"{wins}")
    cta = "see how you can save" in low
    add("P06", "CTA", "PASS" if cta else "WARN", f"CTA label present={cta}")
    mt.tmpl(add, "P07", htmlb)
    img_rx = mds_target.chart_image_host_regex()
    issues = [x for x in mh.branding_issues(htmlb, text) if not (img_rx and re.search(img_rx, x))]
    add("P08", "generic branding + links", "WARN" if issues else "PASS", "; ".join(issues[:6]) or "clean")
    if any(c.status == "FAIL" for c in checks):
        mt.diagnose(run, MS)
    return report(run, checks)


def report(run: Run, checks: list[mt.Check]) -> list[mt.Check]:
    run.m["touPromotionValidation"] = [c.__dict__ for c in checks]
    run.save()
    e = run.m.get("touPromotionEmail", {})
    rc = run.m.get("rateComparisonCheck", {})
    lines = [f"# TOU Rate Promotion (Monthly Summary) -- {run.m.get('uuid')}", "",
             f"email: {e.get('file', '-')}  subject: {e.get('notificationTitle', '-')!r}  variant: {e.get('variant')}",
             f"rate comparison: current plan {rc.get('current')} -> best {rc.get('best')}, savings ${rc.get('saving')} "
             f"({rc.get('pct')}%)", "", "| id | check | status | detail |", "|---|---|---|---|"]
    lines += [f"| {c.id} | {c.name} | {c.status} | {c.detail.replace('|', '/')} |" for c in checks]
    (run.dir("reports") / "tou_rate_promotion_validation_report.md").write_text("\n".join(lines) + "\n")
    worst = "FAIL" if any(c.status == "FAIL" for c in checks) else "PASS"
    log(f"TOU RATE PROMOTION VALIDATION {worst}: " + " ".join(f"{c.id}={c.status}" for c in checks))
    return checks


def create_user(a: argparse.Namespace, rest: list[str]) -> Path | None:
    """New single fuel MDS user (ELECTRIC AMI solar D&R, 13 cycles) on the flat --rate-plan through the launchpad
    files + ingestion + disagg + aggregation of mds_common. --hold-ms marks the MS cycle as sent first, so the
    aggregation-driven pass does not send a plain Monthly Summary: the promotion one is the only MS of the run."""
    step(f"USER: new MDS user on plan {a.rate_plan} (files, ingestion, disagg, aggregation; automatic MS held)")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    argv = ["--phases", "preflight,generate,upload,configure,aggregate", "--output-dir", str(OUT_DIR),
            "--rate-plan", a.rate_plan, "--hold-ms"]
    for flag, val in (("--token-file", a.token_file), ("--aws-profile", a.aws_profile)):
        if val:
            argv += [flag, val]
    before = set(OUT_DIR.glob("run_*"))
    rc = mc.main(__doc__, ("ELECTRIC",), str(OUT_DIR), argv + rest)
    manifest = mh.new_run_manifest(OUT_DIR, before)
    if rc != 0 or manifest is None:
        print(f"user creation did not complete (exit {rc}, run {manifest.parent if manifest else '?'}); resume with "
              f"--uuid <uuid> --phases ratecomparison,configure,email once the user exists", file=sys.stderr)
        return None
    return manifest


def main(argv: list[str] | None = None, default_phases: str = "ratecomparison,configure,email") -> int:
    argv = mds_target.bootstrap(argv)          # PROFILE_REQUIRED before anything touches a target
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                allow_abbrev=False)
    p.add_argument("--uuid", help="existing MDS user with a run manifest under results/ (omit with the user phase)")
    p.add_argument("--phases", default=default_phases, help=f"comma list of {','.join(PHASES)} or all")
    p.add_argument("--rate-plan", default=mc.DEFAULT_RATE_PLAN,
                   help="user phase: the user's (flat) current plan, by name (default 905 MDS_SOLAR_TOU_D_R_2026)")
    p.add_argument("--token-file")
    p.add_argument("--aws-profile")
    p.add_argument("--nve-promotion-configs", action="store_true",
                   help=f"also copy NVE's {erc.PROMO_CT} values at user scope ({NVE_PROMO})")
    p.add_argument("--email-wait", type=int, default=600)
    p.add_argument("--plan-display-name", default="Time-of-Use Saver",
                   help="plan name written into the user's promotion text ('' = the platform's ${planName}, i.e. the "
                        "pilot-level rate_resources planName)")
    p.add_argument("--rename-plan", action="store_true",
                   help="also set the best plan's PILOT-level rate_resources planName to --plan-display-name")
    p.add_argument("--force", action="store_true", help="send even when a rate comparison gate FAILs")
    erc.add_args(p)
    a, rest = p.parse_known_args(argv)
    a.scope, a.dry_run = "pilot", False
    phases = list(PHASES) if a.phases == "all" else [x.strip() for x in a.phases.split(",") if x.strip()]
    bad = [x for x in phases if x not in PHASES]
    if bad:
        p.error(f"unknown phases {bad}")
    if rest and "user" not in phases:
        p.error(f"unknown arguments {rest}")
    if "user" in phases:
        if a.uuid:
            p.error("--uuid and the user phase are exclusive (the user phase creates a new user)")
        manifest = create_user(a, rest)
        if not manifest:
            return 2
    else:
        if not a.uuid:
            p.error("--uuid is required without the user phase")
        manifest = mh.find_manifest(a.uuid, HERE / "results")
        if not manifest:
            print(f"no manifest for {a.uuid} under results/ (the MS resend needs its cycles)", file=sys.stderr)
            return 2
    run = Run(manifest.parent, json.loads(manifest.read_text()))
    a.uuid = run.m["uuid"]
    mc.set_uuid_tag(a.uuid)
    mc._LOG_FH = open(run.dir("logs") / "run.log", "a")
    try:
        mc._TOKEN = mc.get_token(a.token_file)
        failed = False
        if "ratecomparison" in phases:
            live = erc.live_plans()
            erc.configure(run, a, live)
            res = erc.compute(run, a)
            gate = erc.check(run, a, live, res)
            failed = any(c[2] == "FAIL" for c in gate)
            if failed and not a.force and "email" in phases:
                log("a rate comparison gate FAILed: the promotion section would be dropped; not sending "
                    "(--force sends anyway)", "ERROR")
                return 1
        if "configure" in phases:
            configure(run, a)
        checks = email(run, a) if "email" in phases else []
        return 1 if failed or any(c.status == "FAIL" for c in checks) else 0
    except SetupError as exc:
        log(str(exc), "ERROR")
        return 2
    finally:
        if mc._LOG_FH:
            mc._LOG_FH.close()
            mc._LOG_FH = None


if __name__ == "__main__":
    sys.exit(main())
