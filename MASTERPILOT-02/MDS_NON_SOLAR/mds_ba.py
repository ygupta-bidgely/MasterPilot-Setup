"""BUDGET ALERT email for MDS users on target pilot the target pilot -- shared engine for
create_mds_budget_alert_user.py and send_mds_budget_alert_email.py.

How Budget Alert really fires (established on the target pilot, 2026-10-01)
  * It is NOT served by the named-event path: a NotificationsProcessorEvent {EventName BUDGET_ALERT} is consumed and
    produces no log line at all. BudgetAlertEventProcessor runs inside the aggregation-driven notification pass, i.e. it
    is triggered by an AggregationReadyUsers message with sendNotifications=true.
  * Gates, in the order the processor checks them: BUDGET_ALERT in the pilot's supported (solar) events list ->
    open billing cycle exists -> not a monthly user -> delivery modes include Email -> home budget > 0 ->
    cost / budget crosses a threshold of notification_event_config.budget_alert_thresholds (0.75, 1.0) ->
    the crossing happened within max_threshold_days_for_budget_alert days (2) -> first touch point sent -> not already
    sent for this cycle at an equal/higher threshold (state notification/.../BUDGET_ALERT/<fuel>/Email).
  * Solar home: GLOBAL.SOLAR makes EmailDataProvider.getEmailData build the solar energy-flow block, and
    populateSolarEnergyFlowBlock:980 NPEs on itemization.get(ELECTRIC) (the open cycle has no itemization) -> the whole
    Budget Alert dies. Same defect as TOU Onboarding; same workaround: home.solarUser=false for the send, restored after.

What this does
  user       create an MDS user (mds_common, MS held) whose open cycle already carries cost
  configure  USER scope: event_subscriptions.BUDGET_ALERT delivery_modes ["Email"]; home budget amount (computed live)
  send       per alert type: set the budget so cost/budget lands INSIDE the threshold and the 2-day window, reset the
             state, send one aggregation message, wait for the BUDGET_ALERT row, validate it. 'both' = the 75%
             MidCycleBudgetAlert, then the 100% UsageExceedBudgetAlert (budget lowered; 100 > 75 so it is allowed)
  The budget is derived from the live aggregates:  B = (cost - c2/2) / threshold, c2 = cost of the last two day
  buckets. cost >= threshold*B (crossed) and threshold*B >= cost - c2 (crossed inside the delayed-alert window).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import mds_common as mc
import mds_her as mh
import mds_target
import mds_tou as mt
from mds_common import Run, SetupError, api, log, payload, step

HERE = Path(__file__).resolve().parent
EVENT = "BUDGET_ALERT"
PHASES = ("user", "configure", "send")
KINDS = {  # name -> (threshold fraction, EventType, bar image marker)
    "midcycle": (0.75, "MidCycleBudgetAlert", "budget-bar-75"),
    "exceed": (1.0, "UsageExceedBudgetAlert", "budget-bar-100"),
}


@dataclass
class Check:
    id: str
    name: str
    status: str = "PASS"
    detail: str = ""


def norm(t: str) -> str:
    return re.sub(r"\s+", " ", t.replace("\u2019", "'").replace("\u2018", "'")).strip()


def state_path(uuid: str) -> str:
    return f"notification/notificationStatus/{uuid}/{mc.HID}/{EVENT}/ELECTRIC/Email"


def cycle_costs(run: Run, window_days: int = 2) -> dict:
    """Open-cycle cost + day buckets, read the way the processor reads them (default stream). c2 = cost of the last
    `window_days` day buckets = what the delayed-alert window (max_threshold_days_for_budget_alert) subtracts."""
    uuid, now = run.m["uuid"], int(time.time())
    cur = run.m["cycles"][-1]["endTs"]                       # start of the open cycle
    mon = mc.ctype(uuid, "billing_cost", "", "ELECTRIC", cur, now)
    bucket = next((v for k, v in sorted(mon.items()) if int(k) >= cur), None)
    days = mc.ctype(uuid, "billing_cost", "", "ELECTRIC", cur, now, mode="day")      # whole open cycle: data may end days ago
    dv = [(int(k), float(v.get("costWithAllChargeTypeCharges") or 0)) for k, v in sorted(days.items()) if int(k) >= cur]
    return {"curStart": cur, "cost": float((bucket or {}).get("costWithAllChargeTypeCharges") or 0),
            "days": dv, "c2": sum(c for _, c in dv[-window_days:]), "window": window_days}


def budget_for(cc: dict, thr: float, days_over: int = 0) -> float:
    """Budget that makes the alert fire. 75%: 0.75*B sits in the middle of the window. 100% with days_over D: the budget
    is crossed during the day D days before the last data day (cumulative cost before that day < B <= cumulative after)."""
    if thr < 1.0:
        return round((cc["cost"] - cc["c2"] / 2) / thr, 2)
    days = cc["days"]
    x = len(days) - 1 - days_over
    if x < 0:
        raise SetupError(f"--days-over {days_over} needs {days_over + 1} day buckets in the open cycle, found {len(days)}")
    before = cc["cost"] - sum(c for _, c in days[x:])
    if days[x][1] <= 0:
        raise SetupError(f"no positive cost on the day {days_over} day(s) before the last data day: the budget cannot be crossed on it")
    return round(before + days[x][1] / 2, 2)


def days_over_of(cc: dict, budget: float, tz: ZoneInfo) -> tuple[int, str] | None:
    """Days between the day cumulative cost first reached the budget and the last data day."""
    cum, last = 0.0, datetime.fromtimestamp(cc["days"][-1][0], tz).date()
    for ts, c in cc["days"]:
        cum += c
        if cum >= budget:
            d = datetime.fromtimestamp(ts, tz).date()
            return (last - d).days, d.isoformat()
    return None


def set_budget(run: Run, amount: float) -> None:
    path = f"meta/users/{run.m['uuid']}/homes/{mc.HID}"
    api("POST", path, {"budgetThresholdAmount": amount, "electricBudgetThresholdAmount": amount})
    got = mt.home(run.m["uuid"])
    ok = abs(float(got.get("electricBudgetThresholdAmount") or 0) - amount) < 0.005
    run.mutate("home_budget", path=path, value=amount)
    log(f"home budget -> ${amount} (read back {got.get('budgetThresholdAmount')}/{got.get('electricBudgetThresholdAmount')})",
        "INFO" if ok else "WARN")


def configure(run: Run, a: argparse.Namespace) -> None:
    uuid = run.m["uuid"]
    step(f"CONFIGURE Budget Alert (USER scope) for {uuid}")
    mc.apply_user_configs(run, mc.user_configs(tuple(run.m.get("fuels") or ("ELECTRIC",))))
    mc.apply_user_configs(run, {f"event_subscriptions.{EVENT}": {"delivery_modes": '["Email"]'}})
    if a.user_kind == "returning":
        # the target pilot sets use_different_subject_line_for_new_users=true + welcome_section_hybrid_mails_threshold=1 at PILOT scope
        # and EmailSubjectProvider.userHasMailAsNewFeature reads the Budget Alert state WITHOUT a fuel type while the
        # processor writes it per fuel, so every MDS user would get "See your new upgrade!" forever. A normal (returning)
        # user gets "You've reached 75% of your budget amount."
        mc.apply_user_configs(run, {"email_template": {"use_different_subject_line_for_new_users": "false"}})
    if a.days_over > 1:
        # the processor drops an alert whose threshold was crossed more than max_threshold_days_for_budget_alert days ago
        mc.apply_user_configs(run, {"notification_event_config": {"max_threshold_days_for_budget_alert": str(a.days_over + 1)}})
    run.m["baConfigured"] = {"at": time.time()}
    run.save()


def prerequisites(run: Run, a: argparse.Namespace, add, cc: dict) -> None:
    uuid = run.m["uuid"]
    md = mc.read_config("pilot", mc.PILOT_ID, "meta_data")
    for key in ("supported_communication_events", "supported_solar_communication_events"):
        v = mc.cfg(md, key)
        add("P01", f"pilot {key} lists {EVENT}", "PASS" if EVENT in v.split(",") else "FAIL",
            v or "unset: every registry event dies at DEBUG (pilot-level write, ask the owner)")
    nec = mc.read_config("user", uuid, "notification_event_config")
    ths = mc.cfg(nec, "budget_alert_thresholds")
    add("P02", "budget_alert_thresholds", "PASS" if "0.75" in ths and "1" in ths else "WARN", ths or "unset")
    add("P03", "open cycle has cost and two recent day buckets", "PASS" if cc["cost"] > 0 and cc["c2"] > 0 else "FAIL",
        f"cost ${cc['cost']:.2f}, last two days ${cc['c2']:.2f}, buckets {len(cc['days'])} "
        "(the delayed-alert window needs a positive recent cost; a cycle that just rolled over has none)")
    rows = mt.rows_of(uuid, "USER_WELCOME", 0)
    add("P04", "first touch point (USER_WELCOME) sent", "PASS" if rows else "WARN",
        f"{len(rows)} USER_WELCOME Email row(s)" if rows else "none: FIRST_TOUCH_POINT_NOT_SENT drops the email")
    sub = mc.read_config("user", uuid, f"event_subscriptions.{EVENT}")
    add("P05", "user subscription delivery_modes has Email", "PASS" if "Email" in mc.cfg(sub, "delivery_modes") else "FAIL",
        mc.cfg(sub, "delivery_modes"))
    add("P06", "home is solar -> the send decouples solarUser", "INFO", f"solarUser={mt.home(uuid).get('solarUser')}")


def verify(run: Run, a: argparse.Namespace, add, kind: str, row: dict, budget: float, cc: dict, rows: list) -> None:
    thr, etype, _bar = KINDS[kind]
    tag = "M" if kind == "midcycle" else "X"
    m, uuid = run.m, run.m["uuid"]
    htmlb, text, f = mt.save_email(run, row, f"BA_{kind.upper()}")
    low = text.lower()
    m.setdefault("baEmails", {})[kind] = {k: row.get(k) for k in ("notificationId", "notificationTitle", "deliveryDestination",
                                                                 "generationTimestamp")} | {"file": str(f), "budget": budget}
    add(f"{tag}01", f"{kind}: Budget Alert email generated", "PASS",
        f"{row.get('notificationTitle')!r} to={row.get('deliveryDestination')} -> {f.name}")
    sid = {"midcycle": ("mid.cycle.subject", "new.service.budget.alert.mid.cycle.subject"),
           "exceed": ("usage.exceed.subject", "new.service.budget.alert.usage.exceed.subject")}[kind]
    ids = [f"com.bidgely.cloud.email.budget.alert.{sid[0]}", f"com.bidgely.cloud.email.{sid[1]}"]
    want = {}
    for rid in ids:
        txt = (payload(api("GET", f"2.1/stringResources/user/{uuid}/resource/{rid}?locale=en_US", allow=(400, 404, 500))) or {}).get(rid)
        want[rid] = norm(str(txt or "").replace("${budgetAlertThreshold}", str(int(thr * 100))))
    got = norm(row.get("notificationTitle") or "")
    which = next((("new-service" if "new.service" in r else "normal") for r, t in want.items() if t and t == got), None)
    expect = "new-service" if a.user_kind == "new" else "normal"
    add(f"{tag}02", f"{kind}: subject is the {expect} user variant", "PASS" if which == expect else ("WARN" if which else "FAIL"),
        f"subject {row.get('notificationTitle')!r} matches the {which or 'NO'} string (expected {expect}); "
        "new-service subject = pilot use_different_subject_line_for_new_users=true; returning users get it switched off at user scope")
    # the bar is the <img> tagged data-configs usage_bar_image_url_*; its src is the EFFECTIVE config (the target pilot sets
    # ILLUSTRATION-ELEMENT-2.png for 75 and ILLUSTRATION-ELEMENT.png for 100 at PILOT scope), so compare to that
    bars = [u for tag_, u in re.findall(r'(<img\b[^>]*usage_bar_image_url[^>]*?)src="([^"]*)"', htmlb, flags=re.S)]
    cfg = mc.read_config("user", uuid, "email_budget_alert_section_configs")
    want_url = mc.cfg(cfg, f"usage_bar_image_url_{int(thr * 100)}")
    add(f"{tag}03", f"{kind}: usage bar image == configured usage_bar_image_url_{int(thr * 100)}",
        "PASS" if bars and want_url and bars[0] == want_url else ("WARN" if bars else "FAIL"),
        f"email {bars[:1] or 'no bar image'}; config {want_url or 'unset'} ({etype})")
    vals = mh.money(text)
    add(f"{tag}04", f"{kind}: budget ${budget:.0f} shown", "PASS" if mh.has_money(vals, round(budget)) or mh.has_money(vals, budget) else "FAIL",
        f"$ amounts in the email {vals[:10]}")
    add(f"{tag}05", f"{kind}: current cost ${cc['cost']:.0f} shown", "PASS" if mh.has_money(vals, round(cc["cost"])) or mh.has_money(vals, cc["cost"]) else "FAIL",
        f"$ amounts in the email {vals[:10]}")
    # days left = dayEnd(billEnd - 1) - dayEnd(today) in whole days (BudgetAlertDataProvider); today = last data day
    tz = ZoneInfo(m.get("timezone") or mc.need("DEFAULT_TZ"))
    last_day = datetime.fromtimestamp(cc["days"][-1][0], tz).date()
    cyc_last = datetime.fromtimestamp(m["ongoing"]["endTs"] - 1, tz).date()
    want_left = (cyc_last - last_day).days
    mdl = re.search(r"(\d+)\s+days?\s+left", low)
    shown_left = int(mdl.group(1)) if mdl else None
    add(f"{tag}06", f"{kind}: days left == cycle end - last data day ({want_left})",
        "PASS" if shown_left == want_left else ("FAIL" if mdl else "WARN"),
        f"email '{mdl.group(0) if mdl else 'absent'}'; last data day {last_day}, cycle last day {cyc_last}"
        + ("; 0 reads '0 Day' (singular label for <=1, copy defect BA7); mid-cycle runs show a larger number" if want_left <= 1 else ""))
    if kind == "exceed":
        got_over = days_over_of(cc, budget, tz)
        add(f"{tag}15", f"{kind}: budget crossed {a.days_over} day(s) before the alert (from daily cost)",
            "PASS" if got_over and got_over[0] == a.days_over else "FAIL",
            f"cumulative cost reached ${budget} on {got_over[1] if got_over else '?'}, {got_over[0] if got_over else '?'} day(s) before "
            f"the last data day {last_day}; the email text has no 'days over' line (the event carries only daysLeft)")
    add(f"{tag}07", f"{kind}: billing-cycle dates shown", "PASS" if re.search(r"\b\d{1,2}/\d{1,2}\b", text) or re.search(r"[A-Z][a-z]{2}\s+\d{1,2}", text) else "WARN",
        "date range present" if re.search(r"\b\d{1,2}/\d{1,2}\b", text) else "no date range found")
    add(f"{tag}08", f"{kind}: update-budget section present", "PASS" if "budget" in low and ("update" in low or "set" in low) else "WARN",
        "UPDATE_BUDGET_AMOUNT is in the DCX list")
    mt.tmpl(add, f"{tag}09", htmlb)
    # hosts the profile lists in email.provisioner.target.link_hosts (e.g. the environment's settings/survey pages) are expected
    issues = [x for x in mh.branding_issues(htmlb, text) if not any(h in x for h in mc.LINK_HOSTS)]
    add(f"{tag}10", f"{kind}: generic branding + links", "WARN" if issues else "PASS",
        "; ".join(issues[:6]) or "clean (links only to the target dashboard and the profile's link_hosts)")
    bad = []
    for u in sorted(set(bars)):
        try:
            with urllib.request.urlopen(urllib.request.Request(u.replace("&amp;", "&"), method="GET"), timeout=60) as r:
                if r.status != 200 or not str(r.headers.get("Content-Type", "")).startswith("image/"):
                    bad.append(f"{r.status} {r.headers.get('Content-Type')}")
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{type(exc).__name__}: {str(exc)[:70]}")
    add(f"{tag}11", f"{kind}: bar image fetches", "PASS" if bars and not bad else "WARN", f"{len(bars)} image(s), problems {bad or 'none'}")
    ident = m["identity"]["email"]
    add(f"{tag}12", f"{kind}: delivered to the generated user", "PASS" if row.get("deliveryDestination") == ident and row.get("deliveryMode") == "Email" else "WARN",
        f"to={row.get('deliveryDestination')} want {ident}")
    add(f"{tag}13", f"{kind}: one email for this trigger", "PASS" if len(rows) == 1 else "WARN", f"{len(rows)} BUDGET_ALERT Email row(s) since the trigger")
    st = payload(api("GET", state_path(uuid), allow=(404, 500))) or {}
    sent = str((st or {}).get("budgetThresholdsCSV") or "")
    ok = int((st or {}).get("sentCount") or 0) >= 1 and (st or {}).get("billStartTs") == cc["curStart"] \
        and any(abs(float(x) - thr * 100) < 0.01 for x in re.findall(r"[\d.]+", sent))
    add(f"{tag}14", f"{kind}: sent-state = open cycle, threshold {thr * 100:g}", "PASS" if ok else "WARN", json.dumps(st)[:200])


def send(run: Run, a: argparse.Namespace) -> list[Check]:
    m, uuid = run.m, run.m["uuid"]
    step(f"SEND Budget Alert ({a.alert}) for {uuid}")
    checks: list[Check] = []

    def add(cid, name, status, detail):
        checks.append(Check(cid, name, status, detail))
        log(f"  {cid} {status:4} {name}: {detail}", "ERROR" if status == "FAIL" else "INFO")
    if not m.get("baConfigured"):
        configure(run, a)
    window = max(2, a.days_over + 1)
    cc = cycle_costs(run, window)
    prerequisites(run, a, add, cc)
    if any(c.status == "FAIL" for c in checks):
        return finish(run, checks)
    kinds = ["midcycle", "exceed"] if a.alert == "both" else [a.alert]
    t0 = m["cycles"][-2]["startTs"]
    sqs = mc.aws(a.aws_profile).client("sqs")
    decouple = bool(mt.home(uuid).get("solarUser"))
    before = api("GET", state_path(uuid), allow=(404, 500))
    # billStartTs 0 = an older cycle, so nothing is blocked (the subject variant is selected by configure, not by this state)
    api("POST", state_path(uuid), {"billStartTs": 0, "budgetThresholdsCSV": "0", "sentCount": 0, "sentWithSurveyCount": 0})
    run.mutate("ba_state_reset", path=state_path(uuid), before=before)
    try:
        if decouple:
            mt.set_solar_user(run, a, False)
        mt.settle(run, a)
        for kind in kinds:
            thr = KINDS[kind][0]
            budget = budget_for(cc, thr, a.days_over if kind == "exceed" else 0)
            log(f"{kind}: cost ${cc['cost']:.2f}, last-two-day cost ${cc['c2']:.2f} -> budget ${budget} "
                f"(crosses {thr * 100:g}% at ${thr * budget:.2f}; window floor ${cc['cost'] - cc['c2']:.2f})")
            set_budget(run, budget)
            time.sleep(15)
            since = int(time.time())
            mc.sqs_send(run, sqs, mc.AGG_QUEUE, mc.agg_xml(uuid, "ELECTRIC", mc.FUEL_STREAMS["ELECTRIC"], t0, since), f"AGG_BA_{kind}")
            m.setdefault("baTriggeredAt", {})[kind] = since
            run.save()
            rows = mt.wait_rows(uuid, EVENT, since, a.email_wait, a.poll)
            if not rows:
                add(f"{'M' if kind == 'midcycle' else 'X'}01", f"{kind}: Budget Alert email generated", "FAIL",
                    f"no {EVENT} Email row after {a.email_wait}s; logs below")
                diagnose(run)
                break
            verify(run, a, add, kind, rows[0], budget, cc, rows)
    finally:
        if decouple:
            mt.set_solar_user(run, a, True)
    add("R01", "solarUser restored", "PASS" if mt.home(uuid).get("solarUser") in (True, "true") or not decouple else "FAIL",
        f"solarUser={mt.home(uuid).get('solarUser')}")
    return finish(run, checks)


def diagnose(run: Run) -> None:
    uuid = run.m["uuid"]
    log("DIAGNOSE: the aggregation host holds the Budget Alert decision (config-fail-testcase-debugger skill)")
    out = mt.envlogs_pass(run, "aggregation", uuid)
    for ln in [x for x in out.splitlines() if re.search(r"BudgetAlert|BUDGET_ALERT|Exception|drop", x, re.I)][-8:]:
        log(f"  [aggregation] {ln[:320]}", "WARN")


def finish(run: Run, checks: list[Check]) -> list[Check]:
    m = run.m
    m["baValidation"] = [c.__dict__ for c in checks]
    run.save()
    fails = [c for c in checks if c.status == "FAIL"]
    warns = [c for c in checks if c.status == "WARN"]
    lines = [f"# Budget Alert emails on pilot {mc.PILOT_ID}", "",
             f"- uuid: `{m.get('uuid')}`   email: `{m.get('identity', {}).get('email')}`"]
    for k, e in (m.get("baEmails") or {}).items():
        lines.append(f"- {k}: {e.get('notificationTitle')!r} (budget ${e.get('budget')}) -> `{e.get('file')}`")
    lines += [f"- verdict: {'FAIL' if fails else 'PASS'}  ({len(checks)} checks, {len(fails)} FAIL, {len(warns)} WARN)", "",
              "| id | check | status | detail |", "|---|---|---|---|"]
    lines += [f"| {c.id} | {c.name} | {c.status} | {c.detail.replace('|', '/')} |" for c in checks]
    (run.dir("reports") / "BA_SUMMARY.md").write_text("\n".join(lines) + "\n")
    log(f"BUDGET ALERT {'FAIL' if fails else 'PASS'}: {len(checks)} checks, {len(fails)} FAIL, {len(warns)} WARN -> "
        f"{run.dir('reports') / 'BA_SUMMARY.md'}")
    return checks


def parse(doc: str, default_phases: str, argv: list[str] | None) -> tuple[argparse.Namespace, list[str]]:
    p = argparse.ArgumentParser(description=doc, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    p.add_argument("--phases", default=default_phases, help=f"comma list of {','.join(PHASES)} or all")
    p.add_argument("--uuid", help="existing MDS user (any create_mds_* user; needs its run manifest under results/)")
    p.add_argument("--manifest")
    p.add_argument("--output-dir", default=str(HERE / "results" / "budget_alert"))
    p.add_argument("--token-file")
    p.add_argument("--aws-profile")
    p.add_argument("--zip", default="11691")
    p.add_argument("--alert", choices=("both", "midcycle", "exceed"), default="both")
    p.add_argument("--user-kind", choices=("returning", "new"), default="returning",
                   help="returning (default) = a normal user who already received Budget Alert emails (normal subject); "
                        "new = first emails, 'See your new upgrade!' subject")
    p.add_argument("--days-left", type=int, help="days remaining shown in both emails (user phase): the data ends so that "
                                                 "cycle last day - last data day = N. Feasible range today: see the error text")
    p.add_argument("--days-over", type=int, default=0,
                   help="100%% alert: the budget was crossed this many days before the last data day (0 = on the last day). "
                        "The email has no 'days over' text. Above 1 the user-scope alert window is widened to days_over+1")
    p.add_argument("--mid-cycle-days", type=float, default=8.0,
                   help="user phase: end the generated data this many days before now (>= 3 days into the cycle) so the alert "
                        "is MID-cycle with days left; 0 = data up to now (on the last cycle day: '0 Day Left'). Must stay < 10 "
                        "(maxEmailDelayPeriodInSecs)")
    p.add_argument("--allow-auto-ms", action="store_true", help="let the aggregation pass send its own Monthly Summary too")
    p.add_argument("--attr-settle", type=int, default=60)
    p.add_argument("--settle-config", type=int, default=90)
    p.add_argument("--email-wait", type=int, default=600)
    p.add_argument("--poll", type=int, default=20)
    a, rest = p.parse_known_args(argv)
    a.phase_list = list(PHASES) if a.phases == "all" else [x.strip() for x in a.phases.split(",") if x.strip()]
    bad = [x for x in a.phase_list if x not in PHASES]
    if bad:
        p.error(f"unknown phases {bad}")
    if (a.uuid or a.manifest) and "user" in a.phase_list:
        a.phase_list.remove("user")
    return a, rest


def main(doc: str, default_phases: str, argv: list[str] | None = None) -> int:
    argv = mds_target.bootstrap(argv)          # PROFILE_REQUIRED before anything touches a target
    a, rest = parse(doc, default_phases, argv)
    manifest = Path(a.manifest) if a.manifest else None
    if a.uuid and not manifest:
        manifest = mh.find_manifest(a.uuid, HERE / "results")
        if not manifest:
            print(f"no manifest for {a.uuid} under results/; pass --manifest", file=sys.stderr)
            return 2
    if "user" in a.phase_list:
        out_dir = Path(a.output_dir).expanduser()
        base = ["--phases", "preflight,generate,upload,configure,aggregate", "--output-dir", str(out_dir), "--zip", a.zip]
        d_now = datetime.now(ZoneInfo(mc.DEFAULT_TZ)).date()
        if (d_now - d_now.replace(day=1)).days < 3:
            print(f"today is {d_now}: the open cycle has under 3 days of data, so there is no daily cost to cross a budget "
                  "inside the platform's alert window. Run from the 4th of the month (cycle = calendar month on this pilot)",
                  file=sys.stderr)
            return 2
        if a.days_left is not None:
            tz = ZoneInfo(mc.DEFAULT_TZ)
            d0 = datetime.now(tz).date()
            cl = mc.add_months(d0.replace(day=1), 1) - timedelta(days=1)          # last day of the open (calendar-month) cycle
            k = a.days_left - (cl - d0).days
            if not 0 <= k <= 9:
                print(f"--days-left {a.days_left} is not reachable today ({d0}, cycle ends {cl}): the platform skips users whose "
                      f"last data is more than 10 days old, so choose {(cl - d0).days}..{(cl - d0).days + 9}", file=sys.stderr)
                return 2
            base += ["--raw-end-days-ago", str(k)]
        elif a.mid_cycle_days:
            base += ["--raw-end-days-ago", str(a.mid_cycle_days)]
        if not a.allow_auto_ms:
            base.append("--hold-ms")
        for flag, val in (("--token-file", a.token_file), ("--aws-profile", a.aws_profile)):
            if val:
                base += [flag, val]
        before = set(out_dir.glob("run_*"))
        rc = mc.main(doc, ("ELECTRIC",), str(out_dir), base + rest)
        manifest = mh.new_run_manifest(out_dir, before)
        if rc != 0 or manifest is None:
            print(f"user creation did not complete (exit {rc}); resume with --manifest <run>/manifest.json --phases configure,send",
                  file=sys.stderr)
            return rc or 2
    if not manifest:
        print("nothing to do: pass --uuid/--manifest or include the user phase", file=sys.stderr)
        return 2
    run = Run(manifest.parent, json.loads(manifest.read_text()))
    mc.set_uuid_tag(run.m["uuid"])
    mc._LOG_FH = open(run.dir("logs") / "run.log", "a")
    log(f"Budget Alert run for {run.m['uuid']} phases {a.phase_list} dir {run.out}")
    checks: list[Check] = []
    try:
        mc._TOKEN = mc.get_token(a.token_file)
        if "configure" in a.phase_list:
            configure(run, a)
        if "send" in a.phase_list:
            checks += send(run, a)
        return 1 if any(c.status == "FAIL" for c in checks) else 0
    except SetupError as exc:
        log(str(exc), "ERROR")
        run.m.setdefault("errors", []).append(str(exc))
        run.save()
        return 2
    finally:
        if mc._LOG_FH:
            mc._LOG_FH.close()
            mc._LOG_FH = None
