"""TOU Onboarding + TOU Coaching emails for MDS NON-SOLAR users on the target pilot -- shared engine for
create_mds_tou_user.py, send_mds_tou_onboarding_email.py and send_mds_tou_coaching_email.py (MDS_NON_SOLAR copy).

NON-SOLAR: the home is not solar, so every TOU section takes the regular branch (SectionConditionValidator:
touUser && !home.solarUser): meta_data.enable_tou_chart_for_solar stays false, the coaching section list carries no
SOLAR_ENERGY_FLOW, and home.solarUser is never set true by this engine (the solar decouple/restore paths below are
inert on a non-solar home). The TOU plan still needs rateStructureImageUrl on its weekday energy rows. The notes below
are the solar engine's history.

Rate plan  the target pilot plan 907 MDS_TOU_EVRR_D_R = NVE UAT <source pilot> plan 198 OD-1-TOU_EVRR (create_mds_nve_tou_rate_plan.py).
           The user starts on flat MDS_SOLAR_TOU_D_R_2026 (905) and moves to 907 through a METERENROLL rate transition
           effective at the start of the last --transition-cycles completed cycles, so
             * TOU Onboarding sees a NEW TOU user (TouRateStructureDataProvider.isNewTouUser: every earlier schedule
               plan is non-TOU) -- a user created directly on a TOU plan is dropped as OLD_TOU_USER, and
             * the Monthly Summary cycle is TOU-priced (invoice + BILLING_COST), which TOU Coaching reports on.
           RAW gets an evening load (--peak-kw) so the 18-21 On-Peak window carries real usage.

TOU Onboarding  event TOU_ONBOARDING (generic event data flow; the target pilot has generic_event_data_processor_enabled and
           TOU_ONBOARDING in both supported-events lists). USER scope: event_subscriptions.TOU_ONBOARDING delivery
           modes, the section list (DCX serves an EMPTY TOU_ONBOARDING list on the target pilot), the subject string.
           Trigger = NotificationsProcessorEvent {EventName TOU_ONBOARDING, PlanNumber 907}; one send per user
           (sentCount > 0 blocks), so a resend resets the state first.
TOU Coaching  there is no such event: it is MONTHLY_SUMMARY whose variant resolves to MS_TOU_COACHING because
           TOU_USAGE or TOU_BILL_SAVING_RECO rendered (MonthlySummaryVariantResolver; TOU_RATE_PROMOTION would outrank
           it, so it is taken out of the DCX list). USER scope MS section list = the user's DCX list with
           TOU_RATE_PROMOTION replaced by TOU_USAGE + TOU_BILL_SAVING_RECO.
Solar      every TOU section is gated by SectionConditionValidator: touUser && !home.solarUser, or solar &&
           meta_data.enable_tou_chart_for_solar (read at USER scope). MDS homes are solar, so the flag is set on the
           user. For TOU_USAGE the flag also switches to the tou_usage_solar_* templates (U+200E filename defect on
           some builds, kills the whole MS). The default --solar-tou-chart flag keeps the requested solar account as
           it is (home.solarUser untouched). --solar-tou-chart decouple (explicit only) sets home.solarUser=false for
           the Monthly Summary pass (GLOBAL_SOLAR still drives every solar section on this DCX pilot) and restores it;
           it changes the account under test, so it is never chosen automatically: a solar-template failure is
           reported, not retried with decouple.
Layouts    the TOU_ONBOARDING section list is the TARGET's own (its DCX TOU_ONBOARDING list for the user) or
           --onboarding-sections; the PILOT TOU keys (--pilot-tou-config CT.KEY=VAL) are only written when missing
           and given explicitly. Nothing is copied from another pilot.
Caches     plan 907 was created after the running JVMs loaded the target pilot's rates (SQLRatesDao / eUtilityPlans are
           never-invalidated per-JVM caches). The TOU Onboarding data path reads the uncached ERateDao; the ingester's
           plan-name lookup and the pricing path do not: the transition falls back to the home API, and E11 (BILLING_COST
           == RAW x plan) says whether pricing has picked the plan up.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import mds_common as mc
import mds_her as mh
import mds_target
from mds_common import Run, SetupError, api, log, payload, step

HERE = Path(__file__).resolve().parent
# the "before" (flat) and TOU plans come from the profile's email.rate_plans (never assumed)
BASE_PLAN = TOU_PLAN = None


def _retarget(v: dict) -> None:
    globals().update(BASE_PLAN=v["rate_plans"]["flat"], TOU_PLAN=v["rate_plans"].get("tou"))


def _target_view() -> dict:
    return {"rate_plans": {"flat": BASE_PLAN, "gas": mc.DEFAULT_GAS_RATE_PLAN, "tou": TOU_PLAN}}
ONB = "TOU_ONBOARDING"
MS = "MONTHLY_SUMMARY"
PHASES = ("user", "configure", "onboarding", "reaggregate", "coaching", "validate")
# TOU_ONBOARDING sections that carry the email (product semantics: without them it is not a TOU onboarding email);
# marked mandatory in an explicit --onboarding-sections list. The LAYOUT itself is the target's (onboarding_sections).
ONBOARDING_MANDATORY = ("TOU_ONBOARDING_WELCOME", "TOU_RATE_STRUCTURE")
# (no MS_FALLBACK: the Monthly Summary layout is always read from the target; an unreadable list is NEEDS_INPUT)
TOU_MS_SECTIONS = ["TOU_USAGE", "TOU_BILL_SAVING_RECO"]
# TOU_BILL_SAVING_RECO in a Monthly Summary: generateParagraphText does stringMap.get(<id>).getText() with no guard, and
# these two ids exist on NO <deployment> pilot (<pilot A>/the target pilot/<pilot C>) -> NPE kills the whole MS render (emailer
# 2026-09-29 11:57 UTC, TouBillSavingRecoSectionContentProvider:209). Copy from pilot 10105 (db/ string scripts).
# Seeded on the user's MONTHLY_SUMMARY string hierarchy (StringResourceStore: <uuid>.MONTHLY_SUMMARY[.ELECTRIC]).
TOU_MS_STRINGS = {
    "com.bidgely.cloud.email.tou.bill.saving.reco.ms.positive.paragraph.topic.text.first.month":
        "Remember that shifting away from On-Peak hours helps you save money.",
    "com.bidgely.cloud.email.tou.bill.saving.reco.ms.positive.paragraph.topic.text.other.month":
        "Your usage patterns indicate that you could reduce your usage during On-Peak hours to achieve bill savings.",
}
SUBJECT_ID = "com.bidgely.cloud.email.subject.tou.onboarding"
SUBJECT_TEXT = "Your new Time of Use rate can give you great bill savings"
SOLAR_TEMPLATE_ERR = r"tou_usage_solar|Missing required templates"


@dataclass
class Check:
    id: str
    name: str
    status: str = "PASS"
    detail: str = ""


def sec(name: str, mandatory: bool = False) -> dict:
    return {"emailTemplateSection": name, "mandatory": mandatory}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def state_path(uuid: str, event: str, fuel: str = "ELECTRIC") -> str:
    return f"notification/notificationStatus/{uuid}/{mc.HID}/{event}/{fuel}/Email"


def rows_of(uuid: str, event: str, since: int) -> list[dict]:
    r = payload(api("GET", f"2.1/utility_notifications/users/{uuid}?from={since - 3600}&to={int(time.time()) + 3600}",
                    allow=(404,))) or {}
    out = [n for n in (r.get("notificationsList") or []) if n.get("notificationType") == event
           and n.get("deliveryMode") == "Email" and mh.epoch_s(n.get("generationTimestamp")) >= since]
    return sorted(out, key=lambda n: -mh.epoch_s(n.get("generationTimestamp")))


def home(uuid: str) -> dict:
    return payload(api("GET", f"meta/users/{uuid}/homes/{mc.HID}")) or {}


def dcx_ms_sections(uuid: str) -> list[dict]:
    try:
        r = payload(api("GET", f"entities/user/{uuid}/configs/email_template_sections_config"
                               f"?context={MS}&measurement-type=ELECTRIC", allow=(404, 500))) or {}
        kv = {x["key"]: x for x in json.loads(r.get("email_template_sections_config") or "{}").get("kvs", [])}
        return json.loads(kv[f"{MS}.OPT_OUT"]["val"])
    except (KeyError, TypeError, ValueError, SetupError) as exc:
        # never a silent default layout: a list the target does not have would test something else
        raise SetupError(f"NEEDS_INPUT: the target's {MS} section list could not be read ({type(exc).__name__}); "
                         "nothing was written. Fix the read (profile/API) or configure the layout on the target first") from exc


def coaching_sections(uuid: str) -> list[dict]:
    """DCX MS list, TOU_RATE_PROMOTION out (it outranks MS_TOU_COACHING and is for non-TOU users), TOU_USAGE +
    TOU_BILL_SAVING_RECO in, right after the itemization block. Idempotent over an already-edited list."""
    base = [s for s in dcx_ms_sections(uuid)
            if s.get("emailTemplateSection") not in ("TOU_RATE_PROMOTION", *TOU_MS_SECTIONS)]
    names = [s.get("emailTemplateSection") for s in base]
    item = next((i for i, n in enumerate(names) if n in ("ITEMIZATION_V2", "ITEMIZATION", "DF_ITEMIZATION")), None)
    # non-solar: no SOLAR_ENERGY_FLOW block (the solar engine inserts it after the itemization)
    base = [x for x in base if x.get("emailTemplateSection") != "SOLAR_ENERGY_FLOW"]
    names = [x.get("emailTemplateSection") for x in base]
    item = next((i for i, n in enumerate(names) if n in ("ITEMIZATION_V2", "ITEMIZATION", "DF_ITEMIZATION")), None)
    at = item + 1 if item is not None else len(base)
    return base[:at] + [sec(x) for x in TOU_MS_SECTIONS] + base[at:]


def onboarding_sections(uuid: str, explicit: str | None) -> list[dict]:
    """The TOU_ONBOARDING section list: --onboarding-sections (JSON list, or a comma list of section names), else the
    target's own DCX TOU_ONBOARDING list for this user. No other pilot's layout is assumed: none -> NEEDS_INPUT."""
    if explicit:
        try:
            val = json.loads(explicit)
        except ValueError:
            val = [x.strip() for x in explicit.split(",") if x.strip()]
        out = [x if isinstance(x, dict) else sec(str(x), str(x) in ONBOARDING_MANDATORY) for x in val or []]
        if not out or not all(isinstance(x, dict) and x.get("emailTemplateSection") for x in out):
            raise SetupError(f"--onboarding-sections {explicit!r}: no usable section list")
        return out
    r = payload(api("GET", f"entities/user/{uuid}/configs/email_template_sections_config"
                           f"?context={ONB}&measurement-type=ELECTRIC", allow=(404, 500))) or {}
    try:
        kv = {x["key"]: x for x in json.loads(r.get("email_template_sections_config") or "{}").get("kvs", [])}
        for key in (f"{ONB}.OPT_IN", f"{ONB}.OPT_OUT"):
            lst = json.loads((kv.get(key) or {}).get("val") or "[]")
            if lst:
                return lst
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    raise SetupError(f"NEEDS_INPUT: the target serves no {ONB} section list for this user/pilot; pass "
                     "--onboarding-sections (JSON list or comma list of section names) -- no other pilot's layout is assumed")


def tou_configs(uuid: str, solar_flag: bool, onboarding: list[dict] | None = None) -> dict[str, dict[str, str]]:
    onb = json.dumps(onboarding if onboarding is not None else onboarding_sections(uuid, None), separators=(",", ":"))
    ms = json.dumps(coaching_sections(uuid), separators=(",", ":"))
    return {
        # V1 subscription path for this user (the pilot has no event_subscriptions.TOU_ONBOARDING.<type> key)
        f"event_subscriptions.{ONB}": {"delivery_modes": '["Email"]'},
        "email_template_sections_config": {f"{ONB}.OPT_IN": onb, f"{ONB}.OPT_OUT": onb,
                                           f"{MS}.OPT_IN": ms, f"{MS}.OPT_OUT": ms},
        # SectionConditionValidator.isSolarUserEligible -- UserPreferenceSettings.getMetaDataConfig(uuid) is USER scope
        "meta_data": {"enable_tou_chart_for_solar": "true" if solar_flag else "false"},
    }


def set_solar_user(run: Run, a: argparse.Namespace, value: bool) -> None:
    """Flip home.solarUser AND re-evaluate GLOBAL.SOLAR (SolarEvaluator = home.solarUser): on this DCX pilot the
    emails read the stored attribute, not the home row."""
    if value:
        log("non-solar: home.solarUser is never set true (restore of a solar decouple is a no-op here)")
        return
    path = f"meta/users/{run.m['uuid']}/homes/{mc.HID}"
    api("POST", path, {"solarUser": value})          # only the changed field (echoing the GET 500s on 'path')
    run.mutate("home_solarUser", path=path, value=value)
    got = home(run.m["uuid"]).get("solarUser")
    log(f"home.solarUser -> {value} (read back {got})", "INFO" if got == value else "WARN")
    mc.raise_user_attributes(run, a, ["SOLAR_EVALUATOR", "TOU_RATE_EVALUATOR"], a.attr_settle)


# TOU_ONBOARDING itemizes a 1-second "cycle": on a solar home populateItemization leaves an EMPTY fuel map and
# EmailDataProvider.populateSolarEnergyFlowBlock NPEs on .get(ELECTRIC) (deployed line 980). Config-only fix: itemize
# the last invoiced cycle instead -- scoped to the TOU_ONBOARDING sublevel so the Monthly Summary is untouched -- with
# the rate transition arriving after disagg has produced that itemization.
ONBOARDING_ITEMIZATION_CONFIG = {"email_itemization_section_configs": {"use_previous_billing_cycle": "true"}}


def apply_user_sublevel_config(run: Run, entity: str, configs: dict[str, dict[str, str]]) -> None:
    for ct, vals in configs.items():
        mc.post_config(run, entity, ct, vals, "USER_SUBLEVEL")
        cur = mc.read_config("user", entity, ct)
        for k, v in vals.items():
            got = mc.cfg(cur, k)
            log(f"  readback {entity}:{ct}.{k} = {got} ({(cur.get(k) or {}).get('configSource')})",
                "INFO" if got == v else "WARN")


def tou_info(m: dict) -> dict:
    """The TOU plan facts the checks use: the rate transition, or -- for a user enrolled directly on the TOU plan
    (create_mds_tou_coaching_user.py) -- the whole history priced on it."""
    if m.get("transition"):
        return m["transition"]
    if m.get("touDirect") and m.get("cycles"):
        return {"plan": m.get("ratePlan"), "planNumber": (m.get("planNumbers") or {}).get("ELECTRIC"),
                "fromTs": m["cycles"][0]["startTs"], "from": m["cycles"][0]["start"], "direct": True}
    return {}


def set_home_solar(run: Run, value: bool) -> None:
    """home.solarUser only -- the stored GLOBAL.SOLAR attribute is NOT re-evaluated, so the solar email path,
    solar sections and units stay on while the TOU sections take their non-solar branch (TOU_USAGE non-solar
    templates, TOU_BILL_SAVING_RECO eligible). Test-user workaround for the undeployed U+200E tou_usage_solar_*
    template rename; see dcx-solar-attribute-vs-home-solaruser."""
    if value:
        log("non-solar: home.solarUser is never set true (restore of a solar decouple is a no-op here)")
        run.m["solarDecoupledForMs"] = False
        run.save()
        return
    path = f"meta/users/{run.m['uuid']}/homes/{mc.HID}"
    api("POST", path, {"solarUser": value})
    run.mutate("home_solarUser_only", path=path, value=value)
    run.m["solarDecoupledForMs"] = not value
    run.save()
    log(f"home.solarUser -> {value} (read back {home(run.m['uuid']).get('solarUser')}); GLOBAL.SOLAR left as stored")


def settle(run: Run, a: argparse.Namespace) -> None:
    mc.config_cache_settle(run, a.settle_config)


def envlogs_pass(run: Run, service: str, uuid: str) -> str:
    try:
        txt = mh.envlogs(["pass", service, uuid], timeout=900)
    except (SetupError, OSError, Exception) as exc:  # noqa: BLE001
        return f"(envlogs {service} unavailable: {str(exc)[:200]})"
    f = run.dir("logs") / f"{service}_pass_{datetime.now():%H%M%S}.log"
    f.write_text(txt)
    log(f"  {service} pass for {uuid} -> {f}")
    return txt


def save_email(run: Run, row: dict, tag: str) -> tuple[str, str, Path]:
    body = payload(api("GET", f"2.1/utility_notifications/notifications/{row['notificationId']}")) or {}
    htmlb = body.get("notificationBody") or ""
    f = run.dir("email") / f"{tag}_{row['notificationId']}.html"
    f.write_text(htmlb)
    return htmlb, mc.email_text(htmlb), f


def wait_rows(uuid: str, event: str, since: int, wait: int, every: int) -> list[dict]:
    deadline = time.time() + wait
    while time.time() < deadline:
        rows = rows_of(uuid, event, since)
        if rows:
            return rows
        time.sleep(every)
    return []


def plan_rates(m: dict) -> dict[str, float]:
    """touName -> DELIVERED rate for the MS cycle's month (non-holiday)."""
    t = tou_info(m)
    rows = mc.plan_rows_from_db(int(t.get("planNumber") or 0)) or mc.plan_rows_from_local_post(int(t.get("planNumber") or 0)) or []
    month = datetime.fromtimestamp(m["cycles"][-1]["startTs"] + 86400 * 5).month
    return {r["touName"]: float(r["rate"]) for r in rows if r.get("touName") and not r.get("isHoliday")
            and r["monthLow"] <= month <= r["monthHigh"]}


def cycle_tou_split(run: Run) -> dict[str, float]:
    """kWh DELIVERED per touName in the MS cycle, from the RAW file and the plan rows (engine hour semantics)."""
    import create_mds_nve_tou_rate_plan as rp
    m = run.m
    t = tou_info(m)
    cyc = m["cycles"][-1]
    if not t or cyc["startTs"] < t.get("fromTs", 2 ** 31):
        return {}
    rows = payload(api("GET", f"v3.0/rates/configuration/utilityId/{mc.PILOT_ID}?planNumber={t['planNumber']}")) or []
    rates = [r for p in rows for c in p.get("components", []) for r in c.get("rates", [])]
    tz = ZoneInfo(m["timezone"])
    vals, _ = mc.parse_raw_file(m["files"]["RAW_D"], tz)
    scale = 1.0 if m["units"]["ELECTRIC"].lower() == "wh" else 1000.0
    labels: dict[tuple[int, int], list] = {}
    out: dict[str, float] = {}
    for ts, v in vals.items():
        if not (cyc["startTs"] <= ts < cyc["endTs"]):
            continue
        loc = datetime.fromtimestamp(ts, tz)
        key = (loc.month, loc.isoweekday())
        if key not in labels:
            labels[key] = rp.hour_labels([{**r, "attributes": {"data_stream": "DELIVERED"}} for r in rates
                                          if r.get("chargeType") == "CONSUMPTION_BASED" and r.get("touName")],
                                         loc.month, loc.isoweekday())
        lab = labels[key][loc.hour] or "?"
        out[lab] = out.get(lab, 0.0) + v * scale / 1000
    return {k: round(v, 1) for k, v in out.items()}


# PILOT-level TOU enablement for the solar TOU chart (TOU Coaching). Additive only: existing keys keep their values;
# the pre-change config is backed up to pingpong/backups/. The KEYS are what the product reads; their VALUES are the
# target's own: a missing key is written only from --pilot-tou-config CT.KEY=VAL (nothing is copied from another pilot).
#   meta_data.enable_tou_chart_for_solar          SectionConditionValidator solar branch for every TOU section; "true"
#                                                 is the product value that opens the branch (not pilot data)
#   tou_usage_section_configs.min_peak_usage_for_rendering_in_kwh_solar   solar TOU_USAGE render threshold
#   tou_usage_section_configs_palette color301-304  the tou_usage_solar_* chart colours (a missing ${colorNN}
#                                                   aborts the whole Monthly Summary render)
PILOT_TOU_KEYS = {
    "meta_data": ("enable_tou_chart_for_solar",),
    "tou_usage_section_configs": ("min_peak_usage_for_rendering_in_kwh_solar",),
    "tou_usage_section_configs_palette": ("color301", "color302", "color303", "color304"),
}
PILOT_TOU_PRODUCT_VALUES = {"meta_data.enable_tou_chart_for_solar": "true"}


def parse_kv_args(items: list[str] | None) -> dict[str, str]:
    """--pilot-tou-config CT.KEY=VAL items -> {"CT.KEY": "VAL"}."""
    out: dict[str, str] = {}
    for it in items or []:
        k, sep, v = it.partition("=")
        if not sep or "." not in k or not v:
            raise SetupError(f"--pilot-tou-config {it!r}: expected CONFIGTYPE.KEY=VALUE")
        out[k.strip()] = v
    return out


def ensure_pilot_tou(run: Run | None, explicit: dict[str, str] | None = None) -> dict[str, dict[str, str]]:
    """Write the missing PILOT_TOU_KEYS on the target pilot from explicit values (never overwrites a set value; never
    copies another pilot's values). Every missing value is checked BEFORE the first write. Returns what was written."""
    explicit = dict(PILOT_TOU_PRODUCT_VALUES, **(explicit or {}))
    plan: dict[str, dict[str, str]] = {}
    need: list[str] = []
    for ct, keys in PILOT_TOU_KEYS.items():
        cur = mc.read_config("pilot", mc.PILOT_ID, ct)
        for k in keys:
            if mc.cfg(cur, k):
                continue
            if explicit.get(f"{ct}.{k}"):
                plan.setdefault(ct, {})[k] = explicit[f"{ct}.{k}"]
            else:
                need.append(f"{ct}.{k}")
    if need:
        raise SetupError(f"NEEDS_INPUT: pilot {mc.PILOT_ID} lacks {need}; pass --pilot-tou-config CT.KEY=VAL for each "
                         "(the target's own values; nothing is copied from another pilot) or --no-pilot-config")
    written: dict[str, dict[str, str]] = {}
    for ct, todo in plan.items():
        cur = mc.read_config("pilot", mc.PILOT_ID, ct)
        todo = {k: v for k, v in todo.items() if not mc.cfg(cur, k)}
        if not todo:
            log(f"  pilot {mc.PILOT_ID} {ct}: TOU keys already set")
            continue
        raw = api("GET", f"entities/pilot/{mc.PILOT_ID}/configs/{ct}", allow=(404,))
        bk = HERE.parent / "backups" / f"pilot{mc.PILOT_ID}_{ct}_before_tou_{datetime.now():%Y%m%d_%H%M%S}.json"
        bk.parent.mkdir(exist_ok=True)
        bk.write_text(json.dumps(raw, indent=2))
        kvs = [{"configKey": k, "configVal": v, "configDataType": "TEXT", "configRegex": "(?s).*",
                "configDocumentation": f"TOU coaching (solar TOU chart), set by mds_tou.py on {datetime.now():%Y-%m-%d}"}
               for k, v in todo.items()]
        api("POST", f"entities/{mc.PILOT_ID}/configs", {"configType": ct, "configKVs": kvs})
        after = mc.read_config("pilot", mc.PILOT_ID, ct)
        bad = {k: mc.cfg(after, k) for k in todo if mc.cfg(after, k) != todo[k]}
        log(f"  PILOT {mc.PILOT_ID} {ct} <- {todo} (backup {bk.name})" + (f" READBACK MISMATCH {bad}" if bad else ""),
            "WARN" if bad else "INFO")
        written[ct] = todo
        if run:
            run.mutate("PILOT_config", entity=str(mc.PILOT_ID), configType=ct, values=todo, backup=str(bk))
            run.m["lastConfigWriteAt"] = time.time()
    return written


# ---------------------------------------------------------------------------
# phases
# ---------------------------------------------------------------------------
def solar_tou_chart_flag(a: argparse.Namespace) -> bool:
    """USER-scope meta_data.enable_tou_chart_for_solar. Always false on a non-solar home: the TOU sections render
    through touUser && !home.solarUser, and the flag would only switch TOU_USAGE to the solar templates."""
    return False


def configure(run: Run, a: argparse.Namespace) -> None:
    uuid = run.m["uuid"]
    step(f"CONFIGURE TOU Onboarding + TOU Coaching (pilot TOU enablement + USER-scope email configs) for {uuid}")
    onboarding = onboarding_sections(uuid, getattr(a, "onboarding_sections", None))   # NEEDS_INPUT before any write
    if not a.no_pilot_config:
        ensure_pilot_tou(run, parse_kv_args(getattr(a, "pilot_tou_config", None)))
    mc.apply_user_configs(run, mc.user_configs(tuple(run.m.get("fuels") or ("ELECTRIC",))))
    # system mode keeps the flag on: the onboarding render runs on the solar home (solar branch of the TOU sections),
    # and the decoupled Monthly Summary pass (solarUser=false) takes the non-solar branch regardless of the flag
    # all-in-one system flow keeps the solar TOU branch on for the onboarding render; the separate coaching /
    # onboarding flows keep it off so an early (non-decoupled) Monthly Summary cannot load the U+200E solar templates
    # system flows: the onboarding render (all / onboarding) needs the solar TOU branch on a solar home; the coaching
    # flow keeps it off and decouples home.solarUser for its one Monthly Summary pass instead
    mc.apply_user_configs(run, tou_configs(uuid, solar_flag=solar_tou_chart_flag(a), onboarding=onboarding))
    if not a.manual_trigger:
        apply_user_sublevel_config(run, f"{uuid}.{ONB}.ELECTRIC", ONBOARDING_ITEMIZATION_CONFIG)
    cur = (payload(api("GET", f"2.1/stringResources/user/{uuid}/resource/{SUBJECT_ID}?locale=en_US",
                       allow=(400, 404, 500))) or {}).get(SUBJECT_ID)
    if cur:
        log(f"  subject string present: {cur!r}")
    else:
        mc.put_user_string(run, uuid, SUBJECT_ID, SUBJECT_TEXT)
    for rid, text in TOU_MS_STRINGS.items():
        cur = (payload(api("GET", f"2.1/stringResources/user/{uuid}/resource/{rid}?locale=en_US",
                           allow=(400, 404, 500))) or {}).get(rid)
        if cur:
            log(f"  string {rid} present: {cur!r}")
            continue
        for entity in (f"{uuid}.{MS}", f"{uuid}.{MS}.ELECTRIC", uuid):
            mc.put_user_string(run, entity, rid, text)
    run.m["touConfigured"] = {"at": time.time(), "solarTouChart": a.solar_tou_chart}
    run.save()


def eligibility(run: Run, add) -> dict:
    """The server-side facts TOU Onboarding decides on, read back before the trigger."""
    m, uuid = run.m, run.m["uuid"]
    t = tou_info(m)
    h = home(uuid)
    sched = mc.schedule_entries(h)
    plans = [str((e.get("metaData") or {}).get("planNumber")) for e in sched]
    ok = str(h.get("plannumber")) == str(t.get("planNumber")) and len(plans) >= 2 and plans[-1] == str(t.get("planNumber"))
    add("T01", "rate transition on the home", "PASS" if ok else "FAIL",
        f"home {h.get('ratePlanId')}/{h.get('plannumber')} schedule {[(p, e.get('startTime')) for p, e in zip(plans, sched)]} "
        f"(via {(m.get('transitionApplied') or {}).get('via')})")
    plan = next((p for p in payload(api("GET", f"v3.0/rates/configuration/utilityId/{mc.PILOT_ID}?planNumber="
                                           f"{t.get('planNumber')}", allow=(404,))) or [] if isinstance(p, dict)), {})
    tou = sorted({r.get("touName") for c in plan.get("components", []) for r in c.get("rates", []) if r.get("touName")})
    add("T02", "TOU plan (ERateDao view)", "PASS" if {"On-Peak", "Off-Peak"} <= set(tou) else "FAIL",
        f"plan {t.get('planNumber')} {plan.get('planName')} periods {tou}")
    return h


def tou_pricing(run: Run, add) -> bool:
    """T03: BILLING_COST of the TOU cycles == RAW x plan 907 (generator). The aggregation JVMs price from
    SQLRatesDao's per-JVM rate cache; a plan created after they loaded the target pilot is priced at defaultRate 0.1 and gets NO
    TOU aggregate -- then TOU_USAGE has no touCostAndUsageDetail and the MS bill-saving data is empty (TOU Coaching
    cannot render). Only a restart of aggregation + aggregation-priority reloads the cache."""
    m = run.m
    t = tou_info(m)
    cyc = [c for c in m["cycles"] if c["startTs"] >= t.get("fromTs", 2 ** 31)]
    if not cyc:
        return True
    bc = mc.streams_of(mc.ctype(m["uuid"], "billing_cost", "/all-streams", "ELECTRIC", cyc[0]["startTs"] - 86400,
                                cyc[-1]["endTs"] + 86400))
    bad, shown = [], []
    for c in cyc:
        d = mc.near(bc.get("DELIVERED", {}), c["startTs"])
        got = float(d[1].get("energyCharges") or 0) if d else None
        shown.append(f"{c['start']} ${got if got is None else round(got, 2)} (want ${c['E']['dCost']})")
        if got is None or not mc.close(got, c["E"]["dCost"], mc.REL_TOL, 0.05):
            bad.append(c["start"])
    tou = mc.ctype(m["uuid"], "tou", "", "ELECTRIC", cyc[0]["startTs"] - 86400, cyc[-1]["endTs"] + 86400)
    ok = not bad and bool(tou)
    add("T03", f"TOU cycles priced on plan {t.get('planNumber')} + TOU aggregate", "PASS" if ok else "FAIL",
        "; ".join(shown) + f"; TOU aggregate {'present' if tou else 'EMPTY'}" + ("" if ok else
        " -- aggregation JVM rate cache predates the plan (priced at defaultRate): restart aggregation + "
        "aggregation-priority on the target deployment, then --phases reaggregate,coaching"))
    return ok


def priced_peak_window(t: dict) -> str | None:
    """On-Peak window of the current month as the pricing engine resolves it, e.g. '6 PM-9 PM'."""
    import create_mds_nve_tou_rate_plan as rp
    rows = mc.plan_rows_from_db(int(t.get("planNumber") or 0)) or mc.plan_rows_from_local_post(int(t.get("planNumber") or 0)) or []
    month = datetime.now().month
    lab = rp.hour_labels([r for r in rows if r.get("chargeType") == "CONSUMPTION_BASED"], month, 3)
    run_ = [(b, e) for b, e, x in rp.runs(lab) if x == "On-Peak"]
    return f"{rp.hour_txt(run_[0][0])}-{rp.hour_txt(run_[0][1])}" if run_ else None


def onboarding(run: Run, a: argparse.Namespace) -> list[Check]:
    m, uuid = run.m, run.m["uuid"]
    t = tou_info(m)
    step(f"TOU ONBOARDING email for {uuid} (plan {t.get('plan')} {t.get('planNumber')})")
    checks: list[Check] = []

    def add(cid, name, status, detail):
        checks.append(Check(cid, name, status, detail))
        log(f"  {cid} {status:4} {name}: {detail}", "ERROR" if status == "FAIL" else "INFO")
    eligibility(run, add)
    if not m.get("touConfigured"):
        configure(run, a)
    path = state_path(uuid, ONB)
    if not a.manual_trigger:
        since = int(m.get("systemSince") or time.time() - 7200)
        log(f"SYSTEM-generated TOU_ONBOARDING: not sending anything, waiting {a.email_wait}s for the row since {since}")
        rows = wait_rows(uuid, ONB, since, a.email_wait, a.poll)
        m["touOnboardingMode"] = "system (ingestion rate-transition event)"
        run.save()
        return onboarding_validate(run, a, rows, checks, add, t, path)
    before = api("GET", path, allow=(404, 500))
    api("POST", path, {"sentAt": 0, "sentCount": 0})          # sentCount > 0 blocks (isEmailToBeSent)
    run.mutate("tou_onboarding_state_reset", path=path, before=before)
    # DCX solar home: EmailDataProvider.getEmailData always builds the solar energy-flow block when GLOBAL.SOLAR is
    # true, and for TOU_ONBOARDING (a 1-second "cycle", no itemization) populateSolarEnergyFlowBlock:980 NPEs on
    # itemization.get(ELECTRIC) -- the whole event dies (seen 2026-09-29 11:09 UTC). Decouple: solarUser=false with
    # GLOBAL.SOLAR re-evaluated for the send, restored afterwards (TOU sections then take the non-solar branch).
    decouple = a.onboarding_solar == "decouple" and bool(home(uuid).get("solarUser"))
    rows: list[dict] = []
    try:
        if decouple:
            set_solar_user(run, a, False)
        else:
            mc.raise_user_attributes(run, a, ["TOU_RATE_EVALUATOR"], a.attr_settle)
        settle(run, a)
        since = int(time.time())
        snapshot_before_send(run, ONB, since, False if decouple else home(uuid).get("solarUser"))
        body = json.dumps({"userId": uuid, "homeOrdinal": mc.HID,
                           "metaData": {"EventName": ONB, "PlanNumber": int(t.get("planNumber") or 0)}})
        mc.sqs_send(run, mc.aws(a.aws_profile).client("sqs"), mc.NOTIF_QUEUE, body, ONB)
        m["touOnboardingTriggeredAt"] = since
        m["touOnboardingMode"] = "decouple (solarUser=false for the send)" if decouple else "solar"
        run.save()
        rows = wait_rows(uuid, ONB, since, a.email_wait, a.poll)
    finally:
        if decouple:
            set_solar_user(run, a, True)
    return onboarding_validate(run, a, rows, checks, add, t, path)


def onboarding_validate(run: Run, a: argparse.Namespace, rows: list[dict], checks: list[Check], add, t: dict,
                        path: str) -> list[Check]:
    m = run.m
    if not rows:
        add("O01", "email generated", "FAIL", f"no {ONB} Email row after {a.email_wait}s; logs below")
        for i, (what, kind, fix) in enumerate(diagnose(run, ONB), 1):
            add(f"OX{i}", f"known issue [{kind}]", "FAIL", f"{what} -> {fix}")
        return finish(run, "tou_onboarding", checks)
    htmlb, text, f = save_email(run, rows[0], "TOU_ONBOARDING")
    low = text.lower()
    m["touOnboardingEmail"] = {k: rows[0].get(k) for k in ("notificationId", "notificationTitle", "deliveryDestination",
                                                        "generationTimestamp")} | {"file": str(f)}
    add("O01", "email generated", "PASS", f"{rows[0].get('notificationTitle')!r} to={rows[0].get('deliveryDestination')} -> {f.name}")
    add("O02", "subject", "PASS" if (rows[0].get("notificationTitle") or "").strip() else "FAIL",
        repr(rows[0].get("notificationTitle")))
    imgs = re.findall(r'<img[^>]+src="([^"]*rate/mds' + str(mc.PILOT_ID) + r'[^"]*)"', htmlb)
    add("O03", f"rate structure image (plan {TOU_PLAN})", "PASS" if imgs else "FAIL", f"{imgs[:2] or 'plan image not in the email'}")
    periods = [p for p in ("on-peak", "off-peak", "mid-peak", "peak") if p in low]
    want = priced_peak_window(t)
    shown = re.search(r"peak hours (\d{1,2}\s*[ap]m) to (\d{1,2}\s*[ap]m)", low)
    shown_txt = f"{shown.group(1).upper()}-{shown.group(2).upper()}" if shown else None
    same = bool(shown and want and shown_txt.replace(" ", "") == want.replace(" ", ""))
    add("O04", "peak window shown == priced", "PASS" if same else ("WARN" if periods else "FAIL"),
        f"email says {shown_txt or 'no peak window'}; plan prices On-Peak {want} (engine lower-bound hours)"
        + ("" if same or not shown else " -- the rate-structure text reads timeOfDayHigh as inclusive (18-21 -> "
           "6 PM-10 PM) while pricing treats it as the next row's start"))
    words = {"welcome": ("time of use", "save"), "bill saving reco": ("peak",), "motivation": ("grid", "carbon", "blackout")}
    for i, (sname, ws) in enumerate(words.items(), 5):
        hit = [w for w in ws if w in low]
        add(f"O0{i}", f"section: {sname}", "PASS" if hit else "WARN", f"{hit} of {list(ws)}")
    tmpl(add, "O08", htmlb)
    issues = mh.branding_issues(htmlb, text)
    add("O09", "generic branding + links", "WARN" if issues else "PASS", "; ".join(issues[:6]) or "clean")
    st = payload(api("GET", path, allow=(404, 500))) or {}
    add("O10", "sent-state", "PASS" if int((st or {}).get("sentCount") or 0) >= 1 else "WARN", json.dumps(st)[:160])
    return finish(run, "tou_onboarding", checks)


def coaching(run: Run, a: argparse.Namespace) -> list[Check]:
    m, uuid = run.m, run.m["uuid"]
    step(f"TOU COACHING = Monthly Summary with TOU sections (variant MS_TOU_COACHING) for {uuid}")
    checks: list[Check] = []

    def add(cid, name, status, detail):
        checks.append(Check(cid, name, status, detail))
        log(f"  {cid} {status:4} {name}: {detail}", "ERROR" if status == "FAIL" else "INFO")
    if not m.get("touConfigured"):
        configure(run, a)
    priced = tou_pricing(run, add)
    if not priced and not a.force and a.manual_trigger:
        log("TOU Coaching needs the TOU aggregate; not sending an MS that cannot carry TOU sections (--force sends anyway)",
            "ERROR")
        return finish(run, "tou_coaching", checks)
    mode = a.solar_tou_chart
    rows: list[dict] = []
    if not a.manual_trigger:
        since = int(m.get("systemSince") or time.time() - 7200)
        log(f"SYSTEM-generated Monthly Summary: not sending anything, waiting {a.email_wait}s for the row since {since}")
        try:
            rows = wait_rows(uuid, MS, since, a.email_wait, a.poll)
        finally:
            if m.get("solarDecoupledForMs"):
                set_home_solar(run, True)
        mode = "system" + (" (home.solarUser decoupled for the aggregation MS pass)" if a.solar_tou_chart == "decouple"
                           else "")
    # one send per run: a solar-template failure is REPORTED (C00 below), never retried with decouple -- decouple
    # changes the requested solar account and is used only when asked for explicitly (--solar-tou-chart decouple)
    if a.manual_trigger:
        decouple = mode == "decouple"
        if decouple:
            mc.apply_user_configs(run, {"meta_data": {"enable_tou_chart_for_solar": "false"}})
            set_solar_user(run, a, False)
        else:
            # stored GLOBAL.TOU_ELECTRIC / GLOBAL.SOLAR must reflect the TOU plan and the solar home
            mc.raise_user_attributes(run, a, ["TOU_RATE_EVALUATOR", "SOLAR_EVALUATOR"], a.attr_settle)
        try:
            mc.rewind_ms_state(run, "ELECTRIC")
            settle(run, a)
            since = int(time.time())
            snapshot_before_send(run, MS, since, False if decouple else home(uuid).get("solarUser"))
            mc.sqs_send(run, mc.aws(a.aws_profile).client("sqs"), mc.NOTIF_QUEUE,
                        json.dumps({"userId": uuid, "homeOrdinal": mc.HID, "metaData": {"EventName": MS}}), MS)
            m["touCoachingTriggeredAt"] = since
            run.save()
            rows = wait_rows(uuid, MS, since, a.email_wait, a.poll)
        finally:
            if decouple:
                set_solar_user(run, a, True)          # PyAMI/aggregation need the solar home back
                mc.apply_user_configs(run, {"meta_data": {"enable_tou_chart_for_solar": "true"}})
        if not rows and not decouple and re.search(SOLAR_TEMPLATE_ERR, envlogs_pass(run, "emailer", uuid)):
            add("C00", "solar TOU templates", "FAIL",
                "the emailer failed on the tou_usage_solar templates for this solar home; NOT retried with "
                "--solar-tou-chart decouple (that would change home.solarUser of the account under test). Fix the "
                "templates, or re-run with an explicit --solar-tou-chart decouple if a decoupled account is acceptable")
    m["touCoachingMode"] = mode
    if not rows:
        add("C01", "Monthly Summary generated", "FAIL", f"no {MS} Email row after {a.email_wait}s; logs below")
        for i, (what, kind, fix) in enumerate(diagnose(run, MS), 1):
            add(f"CX{i}", f"known issue [{kind}]", "FAIL", f"{what} -> {fix}")
        return finish(run, "tou_coaching", checks)
    row = rows[0]
    htmlb, text, f = save_email(run, row, "TOU_COACHING_MS")
    low = text.lower()
    variant = (row.get("eventData") or {}).get("email_variant_code")
    m["touCoachingEmail"] = {k: row.get(k) for k in ("notificationId", "notificationTitle", "deliveryDestination",
                                                    "generationTimestamp")} | {"variant": variant, "file": str(f), "mode": mode}
    add("C01", "Monthly Summary generated", "PASS", f"{row.get('notificationTitle')!r} -> {f.name} (solar TOU chart: {mode})")
    # email_variant_code is a comma list when several variants apply (e.g. "MS_TOU_COACHING,MS_SOLAR")
    codes = [x.strip() for x in str(variant or "").split(",") if x.strip()]
    add("C02", "variant MS_TOU_COACHING", "PASS" if "MS_TOU_COACHING" in codes else "FAIL", f"email_variant_code={variant}")
    split = cycle_tou_split(run)
    m["touCycleSplit"] = split
    has_usage = "on-peak" in low or "peak hours" in low
    add("C03", "TOU usage section", "PASS" if has_usage else "FAIL",
        f"TOU words present={has_usage}; RAW split for the cycle {split}")
    # the TOU usage chart is an html-to-image PNG (peak/non-peak kWh + $): its numbers are not in the email text.
    # Print what it must show, from RAW x plan, next to the chart URL for a visual check.
    img_rx = mds_target.chart_image_host_regex()             # the target's chart service host (profile), never assumed
    charts = re.findall(rf'<img[^>]+src="(https://(?:{img_rx})[^"]+)"', htmlb) if img_rx else []
    peak = split.get("On-Peak")
    if peak:
        rates = plan_rates(m)
        cost = {k: round(v * rates.get(k, 0), 2) for k, v in split.items()}
        non = round(sum(v for k, v in split.items() if k != "On-Peak"), 1)
        add("C04", "TOU chart (expected values)", ("PASS" if charts else "FAIL") if img_rx else "WARN",
            f"chart must show Peak {round(peak)} kWh ${round(cost.get('On-Peak', 0))}, Non-Peak {round(non)} kWh "
            f"${round(sum(v for k, v in cost.items() if k != 'On-Peak'))}; {len(charts)} chart image(s): {charts[:1]}")
    reco = [w for w in ("shift", "save", "peak") if w in low]
    add("C05", "TOU bill saving reco", "PASS" if len(reco) >= 2 else "WARN", f"{reco}")
    last = m["cycles"][-1]["E"]
    vals = mh.money(text)
    add("C06", "bill amount = invoice", "PASS" if mh.has_money(vals, last["total"]) or mh.has_money(vals, last["energy"])
        else "WARN", f"invoice total ${last['total']} energy ${last['energy']}; $ shown {vals[:8]}")
    tmpl(add, "C07", htmlb)
    issues = [x for x in mh.branding_issues(htmlb, text) if not (img_rx and re.search(img_rx, x))]
    add("C08", "generic branding + links", "WARN" if issues else "PASS", "; ".join(issues[:6]) or "clean")
    return finish(run, "tou_coaching", checks)


def reaggregate(run: Run, a: argparse.Namespace) -> None:
    """Re-run disagg-free aggregation (deleteBeforeRun) over the whole history so BILLING_COST and the TOU aggregate
    are rebuilt with plan 907 -- needed once, after the aggregation services reloaded their rate cache."""
    step("REAGGREGATE (mds_common aggregate phase, deleteBeforeRun) so the TOU cycles are priced on the TOU plan")
    argv = ["--manifest", str(run.out / "manifest.json"), "--phases", "aggregate"]
    for flag, val in (("--token-file", a.token_file), ("--aws-profile", a.aws_profile)):
        if val:
            argv += [flag, val]
    fh = mc._LOG_FH
    rc = mc.main("reaggregate", ("ELECTRIC",), str(run.out.parent), argv)
    mc._LOG_FH = fh if fh and not fh.closed else open(run.dir("logs") / "run.log", "a")
    run.m.update(json.loads((run.out / "manifest.json").read_text()))
    if rc != 0:
        raise SetupError(f"re-aggregation exited {rc}")


def data_validate(run: Run, a: argparse.Namespace) -> list[Check]:
    """mds_common's E01-E21 data checks (RAW, billing cycles, invoice, ENERGY_CONSUMPTION, BILLING_COST = RAW x the
    plan in force, itemization, widgets, MS email) on this user -- read-only, run after the emails (solar restored)."""
    step("DATA VALIDATION (mds_common E01-E21): RAW, invoice, billing cost, itemization, widgets")
    argv = ["--manifest", str(run.out / "manifest.json"), "--phases", "validate"]
    for flag, val in (("--token-file", a.token_file), ("--aws-profile", a.aws_profile)):
        if val:
            argv += [flag, val]
    fh = mc._LOG_FH
    rc = mc.main("validate", ("ELECTRIC",), str(run.out.parent), argv)
    mc._LOG_FH = fh if fh and not fh.closed else open(run.dir("logs") / "run.log", "a")
    run.m.update(json.loads((run.out / "manifest.json").read_text()))
    return [Check("E00", "data validation (reports/validation_report.md)", "PASS" if rc == 0 else "FAIL",
                  f"mds_common validate exit {rc}")]


def tmpl(add, cid: str, htmlb: str) -> None:
    raw = re.findall(r"\$\{[A-Za-z0-9_.]+\}", htmlb)
    nulls = re.findall(r'href="null"|>\s*null\s*<|src="null"', htmlb)
    add(cid, "templating clean", "PASS" if not raw and not nulls else "FAIL",
        f"unrendered {sorted(set(raw))[:5]} null {len(nulls)}")


# Known failure signatures. A generic signature establishes a SYMPTOM (the failing stage), not a unique cause: every
# entry names its candidate causes and the cheapest confirming check. kind: HYPOTHESIS (cause unconfirmed until the
# check is done) | CONFIG / DATA (the line itself names the missing setting / data condition) | ENV (an observed outage).
# events: the notification events a signature applies to (a pass log can hold lines of other events)
KNOWN_ISSUES = [
    (r"populateSolarEnergyFlowBlock|ItemizationWithCostAndHighBill\.getItemizationWithCost",
     "TOU_ONBOARDING NPE in EmailDataProvider.populateSolarEnergyFlowBlock (symptom); candidates: empty ELECTRIC itemization "
     "map for the cycle | other null input of that block", "HYPOTHESIS",
     "check the user's itemization for the bill cycle (disagg ran before the rate change?) before applying the "
     "use_previous_billing_cycle workaround; the unguarded Map.get is a code defect either way", ("TOU_ONBOARDING",)),
    (r"tou_usage_solar|Could not load template", "Monthly Summary could not load a template (symptom); candidates: not "
     "published for the lookup scope | wrong entity/locale scope | file name differs on the deployed build | stale cache",
     "HYPOTHESIS", "check the template store rows for that templateName + scope (read-only SQL) and the deployed build's "
     "file names before renaming/publishing; --solar-tou-chart decouple is only a workaround", ("MONTHLY_SUMMARY",)),
    (r"TouBillSavingRecoSectionContentProvider.*(NullPointer|getText)", "TOU_BILL_SAVING_RECO NPE (symptom); candidate: "
     "unseeded string ids read without a null guard", "HYPOTHESIS",
     "read the two ...bill.saving.reco.ms.positive.paragraph.topic.text.* strings at user/pilot scope", ("MONTHLY_SUMMARY",)),
    (r"tou rate structure data not available|TOU_RATE_STRUCTURE_NOT_AVAILABLE",
     "TOU onboarding dropped TOU_RATE_STRUCTURE (symptom); candidates: solar eligibility gate on the deployed build | plan "
     "rows without rateStructureImageUrl | rate-structure lookup failure", "HYPOTHESIS",
     "check home.solarUser AT EVENT TIME (timestamped evidence), the deployed source of the gate, the plan's "
     "rateStructureImageUrl (usage-chart-details endpoint) and whether the fetch line ran (only with complete logs)",
     ("TOU_ONBOARDING",)),
    (r"\$\{color\d+\}|Missing required templates", "Velocity colour/template missing (candidate: palette config)",
     "CONFIG", "tou_usage_section_configs_palette color301-304 (pilot, ensure_pilot_tou)", ("TOU_ONBOARDING", "MONTHLY_SUMMARY")),
    (r"OLD_TOU_USER", "user not a NEW TOU user (the drop reason names it)", "DATA",
     "the schedule before the transition must be a non-TOU plan", ("TOU_ONBOARDING",)),
    (r"INSUFFICIENT_TOU_MONTHS", "TOU months below minimum (the drop reason names it)", "DATA",
     "transition effective date earlier, or minimum_tou_months_for_tou_onboarding_email", ("TOU_ONBOARDING",)),
    (r"DELIVERY_MODES_NOT_AVAILABLE|deliveryModes: \[\]", "no Email delivery mode resolved (candidate: subscription scope)",
     "CONFIG", "event_subscriptions.<EVENT>.delivery_modes=[\"Email\"] (user scope, configure does)", ("TOU_ONBOARDING", "MONTHLY_SUMMARY")),
    (r"TOU_RATE_PROMOTION", "candidate: TOU_RATE_PROMOTION outranks MS_TOU_COACHING", "CONFIG",
     "remove TOU_RATE_PROMOTION from the user's MONTHLY_SUMMARY section list (configure does)", ("MONTHLY_SUMMARY",)),
    (r"StatusCode: 503|503 Service Temporarily Unavailable|Connection retries limit exceeded",
     "observed: api-server answered HTTP 503 to the emailer/notifications at that time", "ENV",
     "the outage is observed, not inferred; whether it caused THIS miss needs the send attempt in the same pass. Re-check "
     "later or re-run once the API is healthy", ("TOU_ONBOARDING", "MONTHLY_SUMMARY")),
]


def pull_user_lines(run: Run, service: str, uuid: str) -> list[str] | None:
    """Every line of `service` that mentions the user (current + rotated logs); None = logs could not be pulled."""
    try:
        txt = mh.envlogs(["grep", service, uuid, "20000"], timeout=900)
    except Exception as exc:  # noqa: BLE001 -- missing password, host churn, timeout: report as missing logs
        log(f"  [{service}] logs unavailable: {str(exc)[:200]}", "WARN")
        return None
    lines = [ln for ln in txt.splitlines() if ln and not ln.startswith("-->")]
    (run.dir("logs") / f"{service}_{uuid[:8]}_{datetime.now():%H%M%S}.log").write_text("\n".join(lines) + "\n")
    return lines


def snapshot_before_send(run: Run, event: str, at: float, solar) -> None:
    """The state AT the manual send (taken right before it, after any decouple): diagnosis uses this, never a value
    recorded at an earlier transition/pass or read afterwards."""
    run.m.setdefault("sendSnapshot", {})[event] = {"at": float(at), "solarUser": None if solar is None else bool(solar)}
    run.save()


def event_window(run: Run, event: str) -> tuple[float, float]:
    """From the moment the system could have raised the event (the manual send / the plan switch / the Monthly Summary
    pass) to now."""
    m = run.m
    snap = (m.get("sendSnapshot") or {}).get(event)
    if snap and snap.get("at"):
        return float(snap["at"]) - 60, time.time() + 60
    if event == ONB:
        t0 = (m.get("transitionApplied") or {}).get("at") or m.get("systemSince") or time.time() - 7200
        t0 -= 900               # the publish happens inside the ingestion pass, before the API shows the new schedule
    else:
        t0 = (m.get("homeAtMsPass") or {}).get("at") or m.get("systemSince") or time.time() - 7200
        t0 -= 300
    return float(t0), time.time() + 60


def solar_at_event(run: Run, event: str) -> bool | None:
    """home.solarUser when the event was processed -- recorded at the time, never today's (possibly restored) value."""
    snap = (run.m.get("sendSnapshot") or {}).get(event) or run.m.get("homeAtTransition" if event == ONB else "homeAtMsPass") or {}
    v = snap.get("solarUser")
    return None if v is None else str(v).lower() == "true"


def diagnose(run: Run, event: str, fuel: str = "ELECTRIC") -> list[tuple[str, str, str]]:
    """No email row: rebuild the evidence chain for THIS event of THIS user in the event's time window (publication,
    processing, drop, hand-off, storage, provider acceptance) and classify only the lines bound to it."""
    import tou_evidence as te
    uuid = run.m["uuid"]
    window = event_window(run, event)
    log(f"DIAGNOSE {event}/{fuel}: evidence chain from {', '.join(te.SERVICES[event])} (config-fail-testcase-debugger skill)")
    raw = {svc: pull_user_lines(run, svc, uuid) for svc in te.SERVICES[event]}
    try:                                    # storage proof = the stored notification rows, not a log line
        stored = [dict(r, generationTimestamp=mh.epoch_s(r.get("generationTimestamp")))
                  for r in rows_of(uuid, event, int(window[0]))]
    except Exception as exc:  # noqa: BLE001
        log(f"  stored notification rows unavailable: {str(exc)[:160]}", "WARN")
        stored = None
    chain = te.classify(raw, uuid, event, fuel, window, KNOWN_ISSUES, solar_at_event(run, event), stored_rows=stored)
    for ln in te.report(chain):
        log("  " + ln, "WARN")
    found = [(w, k, f) for w, k, f, _ in chain.issues]
    if chain.verdict == "LOGS_MISSING":
        found.append(("host logs unavailable: diagnosis not run", "ENV",
                      "export BOPS_PW_FILE=$TMPDIR/bops_pw_$USER (jumphost password) and re-run "
                      "send_mds_tou_onboarding_email.py / send_mds_tou_coaching_email.py --uuid <uuid>"))
    elif chain.verdict == "DELIVERY_FAILURE":
        found.append(("email built but not stored/sent by the emailer (symptom)", "HYPOTHESIS",
                      "candidates: rejected cached OAuth token | storage API error | rejected payload. Only an "
                      "invalid_token response in the same pass confirms the token cause; check it before restarting anything"))
    elif not found:
        found.append((f"no classified cause ({chain.verdict})", "UNKNOWN",
                      "raise a ticket with the saved per-service logs in logs/"))
    for w, k, f in found:
        log(f"  KNOWN ISSUE [{k}] {w} -> {f}", "ERROR" if k in ("TICKET", "UNKNOWN") else "WARN")
    run.m.setdefault("evidence", {})[event] = {
        "verdict": chain.verdict, "window": list(window), "solarAtEvent": solar_at_event(run, event),
        "logs": chain.logs, "published": chain.published, "publishedFuel": chain.published_fuel,
        "processed": chain.processed, "dropped": chain.dropped, "storage": chain.storage, "stored": chain.stored,
        "storeFailure": chain.store_failure, "providerAccepted": chain.provider_accepted,
        "attempts": [{"service": x.service, "start": x.start, "fuel": x.fuel, "outcome": x.outcome}
                     for x in sorted(chain.processing + chain.sends, key=lambda x: x.start)],
        "recoveredFrom": [x.outcome for x in chain.recovered_from],
        "excludedOtherFuel": len(chain.excluded_other_fuel),
        "issues": [{"issue": w, "kind": k, "fix": f, "evidence": e} for w, k, f, e in chain.issues]}
    run.m.setdefault("knownIssues", {})[event] = [{"issue": w, "kind": k, "fix": f} for w, k, f in found]
    run.save()
    return found


def finish(run: Run, tag: str, checks: list[Check]) -> list[Check]:
    run.m[f"{tag}Validation"] = [c.__dict__ for c in checks]
    run.save()
    email = run.m.get("touOnboardingEmail" if tag == "tou_onboarding" else "touCoachingEmail", {})
    lines = [f"# {tag.replace('_', ' ').title()} -- {run.m.get('uuid')}", "",
             f"email: {email.get('file', '-')}  subject: {email.get('notificationTitle', '-')!r}", "",
             "| id | check | status | detail |", "|---|---|---|---|"]
    lines += [f"| {c.id} | {c.name} | {c.status} | {c.detail.replace('|', '/')} |" for c in checks]
    (run.dir("reports") / f"{tag}_validation_report.md").write_text("\n".join(lines) + "\n")
    worst = "FAIL" if any(c.status == "FAIL" for c in checks) else "PASS"
    log(f"{tag.upper()} VALIDATION {worst}: " + " ".join(f"{c.id}={c.status}" for c in checks))
    return checks


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def parse(doc: str, default_phases: str, argv: list[str] | None,
          default_flow: str = "all") -> tuple[argparse.Namespace, list[str]]:
    p = argparse.ArgumentParser(description=doc, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    p.add_argument("--phases", default=default_phases, help=f"comma list of {','.join(PHASES)} or all")
    p.add_argument("--uuid", help="existing TOU user created by create_mds_tou_user.py")
    p.add_argument("--manifest")
    p.add_argument("--output-dir", default=str(HERE / "results" / "tou"))
    p.add_argument("--token-file")
    p.add_argument("--aws-profile")
    p.add_argument("--zip", default="11691")
    p.add_argument("--base-rate-plan", default=BASE_PLAN)
    p.add_argument("--tou-rate-plan", default=TOU_PLAN)
    p.add_argument("--transition-cycles", type=int, default=2, help="completed cycles on the TOU plan (MS cycle included)")
    p.add_argument("--peak-kw", type=float, default=2.2)
    # flag by default: the requested (solar) account stays as it is. decouple (explicit only) sets home.solarUser=false
    # for the Monthly Summary pass: TOU_BILL_SAVING_RECO checks only isUserEligible (touUser && !home.solarUser) and
    # TOU_USAGE then takes the non-solar templates -- but it changes the account under test.
    p.add_argument("--solar-tou-chart", choices=("flag", "decouple"), default="flag",
                   help="non-solar: no effect (enable_tou_chart_for_solar stays false, home.solarUser stays false). "
                        "Solar engine: flag (default): keep home.solarUser, enable the solar TOU branch; decouple (explicit only): "
                        "home.solarUser=false for the Monthly Summary pass, restored afterwards")
    p.add_argument("--onboarding-sections",
                   help="TOU_ONBOARDING section list (JSON list or comma list of names); default: the target's own list")
    p.add_argument("--pilot-tou-config", action="append", metavar="CT.KEY=VAL",
                   help="value for a PILOT TOU key the target lacks (written only when missing); repeatable")
    p.add_argument("--allow-auto-ms", action="store_true",
                   help="let the aggregation pass send its own (plain MS_SOLAR) Monthly Summary as well")
    p.add_argument("--manual-trigger", action="store_true",
                   help="legacy: send TOU_ONBOARDING / MONTHLY_SUMMARY NotificationsProcessorEvents by hand. Default: the "
                        "system generates both (ingestion's rate-transition event + the aggregation-driven MS pass); the "
                        "run only configures the user up front, then waits and validates")
    p.add_argument("--flow", choices=("all", "coaching", "onboarding"), default=default_flow,
                   help="all = flat->TOU + both emails; coaching = user enrolled on TOU, Monthly Summary only (fast); "
                        "onboarding = flat history, plan switch, 2 rounds of disagg + aggregation")
    p.add_argument("--coaching-settle", type=int, default=45,
                   help="coaching flow: seconds to let ingestion-triggered aggregation settle before disagg")
    p.add_argument("--onboarding-hook-wait", type=int, default=420,
                   help="system mode: seconds to wait for the system's TOU_ONBOARDING email before the MS pass")
    p.add_argument("--restore-pilot-flag", action="store_true",
                   help="repair an interrupted run: restore launchpad_ingestion_configs.handle_meter_enrollment from "
                        "the manifest's pilotFlagWindow and home.solarUser, then exit")
    p.add_argument("--force", action="store_true", help="coaching: send even when T03 (TOU pricing) fails")
    p.add_argument("--onboarding-solar", choices=("decouple", "keep"), default="keep",
                   help="keep (default) sends on the requested solar home; decouple (explicit only) sends with "
                        "home.solarUser=false and restores it, which tests a DIFFERENT account than requested")
    p.add_argument("--attr-settle", type=int, default=60, help="seconds to wait after a UserAttributesEvent")
    p.add_argument("--no-pilot-config", action="store_true",
                   help="skip the PILOT-level TOU enablement (PILOT_TOU_CONFIGS; additive, backed up)")
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


def main(doc: str, default_phases: str, argv: list[str] | None = None, flow: str = "all") -> int:
    argv = mds_target.bootstrap(argv)          # PROFILE_REQUIRED before anything touches a target
    if TOU_PLAN is None:
        raise SetupError("the profile states no email.rate_plans.tou: TOU flows need an explicit TOU plan")
    a, rest = parse(doc, default_phases, argv, flow)
    manifest = Path(a.manifest) if a.manifest else None
    if a.uuid and not manifest:
        manifest = mh.find_manifest(a.uuid, HERE / "results")
        if not manifest:
            print(f"no manifest for {a.uuid} under results/; pass --manifest", file=sys.stderr)
            return 2
    if "user" in a.phase_list:
        out_dir = Path(a.output_dir).expanduser()
        common = ["--output-dir", str(out_dir), "--zip", a.zip, "--peak-kw", str(a.peak_kw)]
        if a.flow == "coaching":
            # enrolled directly on the TOU plan: TOU-priced RAW + INVOICE, no transition, one disagg + aggregation
            base = ["--phases", "preflight,generate,upload,configure,aggregate", "--rate-plan", a.tou_rate_plan,
                    "--settle", str(a.coaching_settle)] + common
        else:
            phases = "preflight,generate,upload,configure,aggregate" + (",transition" if a.flow == "onboarding" else "")
            base = ["--phases", phases, "--rate-plan", a.base_rate_plan, "--transition-rate-plan", a.tou_rate_plan,
                    "--transition-cycles", str(a.transition_cycles)] + common
        if a.manual_trigger:
            base += ["--transition-via", "meterenroll"]
        if not a.allow_auto_ms and a.manual_trigger:
            base.append("--hold-ms")          # one Monthly Summary per run: the TOU Coaching one
        for flag, val in (("--token-file", a.token_file), ("--aws-profile", a.aws_profile)):
            if val:
                base += [flag, val]
        before = set(out_dir.glob("run_*"))
        if not a.manual_trigger:
            if a.flow == "all":
                base += ["--transition-via", "userenroll-flag", "--transition-at", "before-final-agg",
                         "--no-transition-home-api-fallback"]
            elif a.flow == "onboarding":
                # round 1 (flat) disagg + aggregation, then the plan switch, then round 2 on the TOU plan
                base += ["--transition-via", "userenroll-flag", "--transition-at", "phase", "--no-transition-home-api-fallback",
                         "--reaggregate-after-transition"]

            def _configure_first(r: Run) -> None:
                r.m["systemSince"] = int(time.time()) - 60
                r.m["touDirect"] = a.flow == "coaching"
                r.m["touFlow"] = a.flow
                configure(r, a)
            mc.AFTER_USER_CREATED = _configure_first

            def _before_final_agg(r: Run) -> None:
                if a.flow == "all":
                    # the transition just published TOU_ONBOARDING: let it render on the real (solar) home first
                    log(f"waiting up to {a.onboarding_hook_wait}s for the system-generated TOU_ONBOARDING email "
                        "before the Monthly Summary pass (nothing is sent)")
                    rows = wait_rows(r.m["uuid"], ONB, int(r.m.get("systemSince") or time.time()),
                                     a.onboarding_hook_wait, a.poll)
                    r.m["touOnboardingSeenBeforeMs"] = bool(rows)
                    log(f"TOU_ONBOARDING row {'present' if rows else 'NOT present'} before the Monthly Summary pass",
                        "INFO" if rows else "WARN")
                if a.solar_tou_chart == "decouple" and r.m.get("touDirect", a.flow != "onboarding"):
                    set_home_solar(r, False)
                r.m["homeAtMsPass"] = {"at": time.time(), "solarUser": home(r.m["uuid"]).get("solarUser")}
                r.save()
            # the onboarding flow does not decouple: its Monthly Summaries are by-products, not under test
            mc.BEFORE_FINAL_AGG = _before_final_agg if a.flow != "onboarding" else None

            def _after_transition(r: Run) -> None:
                # onboarding renders seconds after the switch, on the solar home: it needs the solar TOU branch
                # (enable_tou_chart_for_solar=true). Before round 2 re-runs the Monthly Summary pass, turn it off again
                # so a by-product MS does not load the U+200E tou_usage_solar_* templates.
                rows = wait_rows(r.m["uuid"], ONB, int(r.m.get("systemSince") or time.time()), a.onboarding_hook_wait,
                                 a.poll)
                r.m["touOnboardingSeenBeforeRound2"] = bool(rows)
                log(f"TOU_ONBOARDING row {'present' if rows else 'NOT present'} after the plan switch",
                    "INFO" if rows else "WARN")
                mc.apply_user_configs(r, {"meta_data": {"enable_tou_chart_for_solar": "false"}})
                mc.config_cache_settle(r, a.settle_config)
            mc.AFTER_TRANSITION = _after_transition if a.flow == "onboarding" else None
        rc = mc.main(doc, ("ELECTRIC",), str(out_dir), base + rest)
        mf = (mc.LAST_RUN_DIR / "manifest.json") if mc.LAST_RUN_DIR else None
        manifest = mf if mf and mf.exists() else mh.new_run_manifest(out_dir, before)
        if rc != 0 and manifest is not None:
            r0 = Run(manifest.parent, json.loads(manifest.read_text()))
            if r0.m.get("solarDecoupledForMs") or ("pilotFlagWindow" in r0.m and not r0.m["pilotFlagWindow"].get("closedAt")):
                try:
                    mc.restore_pilot_flag(r0)
                    if r0.m.get("solarDecoupledForMs"):
                        set_home_solar(r0, True)
                except SetupError as exc:
                    print(f"RESTORE FAILED: {exc}", file=sys.stderr)
                    return 2
        if rc != 0 or manifest is None:
            print(f"user creation did not complete (exit {rc}, run {manifest.parent if manifest else '?'}); resume with "
                  "--manifest <run>/manifest.json --phases configure,onboarding,coaching", file=sys.stderr)
            return rc or 2
    if not manifest:
        print("nothing to do: pass --uuid/--manifest or include the user phase", file=sys.stderr)
        return 2
    run = Run(manifest.parent, json.loads(manifest.read_text()))
    if not run.m.get("transition") and not run.m.get("touDirect"):
        print(f"{manifest} has no rate transition: create the user with create_mds_tou_user.py", file=sys.stderr)
        return 2
    mc.set_uuid_tag(run.m["uuid"])
    mc._LOG_FH = open(run.dir("logs") / "run.log", "a")
    if a.restore_pilot_flag:
        try:
            mc._TOKEN = mc.get_token(a.token_file)
            mc.restore_pilot_flag(run)
            if run.m.get("solarDecoupledForMs"):
                set_home_solar(run, True)
            return 0
        except SetupError as exc:
            log(f"RESTORE FAILED: {exc}", "ERROR")
            return 2
    log(f"TOU run for {run.m['uuid']} phases {a.phase_list} dir {run.out}")
    checks: list[Check] = []
    rc = 2
    try:
        mc._TOKEN = mc.get_token(a.token_file)
        if "configure" in a.phase_list:
            configure(run, a)
        if "onboarding" in a.phase_list:
            checks += onboarding(run, a)
        if "reaggregate" in a.phase_list:
            reaggregate(run, a)
        if "coaching" in a.phase_list:
            checks += coaching(run, a)
        if "validate" in a.phase_list:
            checks += data_validate(run, a)
        rc = 1 if any(c.status == "FAIL" for c in checks) else 0
    except SetupError as exc:
        log(str(exc), "ERROR")
        run.m.setdefault("errors", []).append(str(exc))
        run.save()
        rc = 2
    finally:
        try:                                    # never leave the pilot flag open or the test home decoupled
            mc.restore_pilot_flag(run)
            if run.m.get("solarDecoupledForMs"):
                set_home_solar(run, True)
        except Exception as exc:  # noqa: BLE001 -- an unresolved restore fails the run, whatever the checks said
            log(f"RESTORE FAILED: {exc} -- run with --manifest {run.out / 'manifest.json'} --restore-pilot-flag", "ERROR")
            rc = 2
        if mc._LOG_FH:
            mc._LOG_FH.close()
            mc._LOG_FH = None
    return rc
