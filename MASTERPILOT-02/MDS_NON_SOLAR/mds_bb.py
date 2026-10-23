"""Budget Billing (BB) user + Monthly Summary BB section + web BB info for MDS users on target pilot the target pilot --
shared engine for create_mds_budget_billing_user.py and send_mds_budget_billing_email.py.

What makes a user a Budget Billing user (core-lib BudgetBillingManager; verified against the code, see BUGS_budget_billing_mds.md):
  1. budget_billing_configs.bb_program_ids_map names the programme per fuel (ELECTRIC -> BB_PROGRAM_1). Already set on the target pilot.
  2. A userProgramData row, written by a launchpad file named USERPROGRAM_<utility_name>_<stamp>_T<n>.txt
         customer|account|premise|BB_PROGRAM_1|<start>|<end or blank>|true|true||{"BB_TrueUp_Period": 12}
     with a POSITIVE BB_TrueUp_Period (null/0 -> isValidCumulativeData false, the cumulative section never renders).
  3. INVOICE rows with chargeType literally BB_AMOUNT and a POSITIVE cost on every cycle of the BB period
     (<chargeName>|BB_AMOUNT|0.000|<amount>). They are excluded from TOTAL; the latest invoice MUST carry one or the
     user silently reads as non-BB (BudgetBillingManager.isValid).
  The ongoing BB period = the active invoices modulo BB_TrueUp_Period (getNoOfPreviousInvoicesToBeSkipped), so
  --bb-elapsed-cycles N puts the programme start at the N-th last cycle: N cycles of actual-vs-budget in the chart, the rest
  of the period projected from the average BB_AMOUNT.

Once the user is BB:
  * WEB     usage-widget-data -> budgetBillingInfo {isBBUser, bbAmount, actualBillAmount, trueUpPeriod, trueUpDate}
  * EMAIL   DCX adds BB_CUMULATIVE_BALANCE to the Monthly Summary section list; MonthlySummaryVariantResolver maps it to the
            variant MS_BB. The section is an html-to-image chart (needs bb_cumulative_balance_section_configs_palette
            colours 1/4/43/86-90 and 9 bb.* strings: all present on the target pilot) plus an insight line and a True-up disclaimer.
  * BillProjection is NOT generated on MDS (known), so no projection check is made.

Phases   user       new ELECTRIC AMI solar D&R user + BB files (mds_common, --budget-billing, --hold-ms)
         configure  pilot BB keys (additive, backed up; no-op when present) + USER-scope configs + MS section list with BB
         verify     B01-B05: programs API row, web budgetBillingInfo, passwordless dashboard link
         email      rewind MS state, send MONTHLY_SUMMARY, E01-E10 on the saved HTML
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import mds_common as mc
import mds_her as mh
import mds_target
import mds_tou as mt
from mds_common import Run, SetupError, api, log, payload, step

HERE = Path(__file__).resolve().parent
PROGRAM_ID = "BB_PROGRAM_1"
MS = "MONTHLY_SUMMARY"
BB_SECTION = "BB_CUMULATIVE_BALANCE"
PHASES = ("user", "pilot", "program", "aggregate", "configure", "verify", "email")
DEFAULT_UTILITY = None                    # the profile's email.provisioner.target.utility_name


def _retarget(v: dict) -> None:
    globals()["DEFAULT_UTILITY"] = v.get("utility_name")


def _target_view() -> dict:
    return {"utility_name": DEFAULT_UTILITY}
CHARGE_NAME = "BUDGET_BILLING_ADJUSTMENT"
PALETTE_ID = "bb_cumulative_balance_section_configs_palette"
# PILOT-level BB enablement, identical to <pilot A>/<pilot C> (read 2026-10-01). Additive only: an existing value is never overwritten.
PILOT_BB_CONFIGS = {
    "budget_billing_configs": {"bb_program_ids_map": '{"ELECTRIC": "BB_PROGRAM_1", "WATER": "BB_PROGRAM_3"}',
                               "no_of_bcs_for_average": "2", "average_bc_duration": "30",
                               "bb_invoice_after_program_start": "false"},
    "bb_cumulative_balance_section_configs": {"color_palette_id": PALETTE_ID},
    PALETTE_ID: {"color1": "#333333", "color4": "#FFFFFF", "color43": "#F6F6F6", "color86": "#FE8E25",
                 "color87": "#088090", "color88": "#1F73E3", "color89": "#2E384D", "color90": "#616F89"},
}
# program_part_launchpad (the USERPROGRAM launchpad file's field map) is Launchpad ingestion configuration: the kit
# forbids writing it (email.forbidden_config), so it is a READ-ONLY prerequisite here, never written. Without it the
# ingester NPEs on every USERPROGRAM file (LaunchpadUserProgramParticipationTransformer.validateDates:337, marker
# 500.2001). The field positions below are the product's USERPROGRAM file layout (the file mds_common writes), used only
# to CHECK the target's map.
PROGRAM_PART_CT = "program_part_launchpad"
PROGRAM_PART_FIELDS = {"customer_id": 0, "account_id": 1, "premise_id": 2, "program_id": 3, "user_part_start_date": 4,
                       "user_part_end_date": 5, "is_eligible": 6, "is_participating": 7, "task_id": 8, "program_attr": 9}
BLOCKED_PROGRAM_PART = ("BLOCKED: pilot {pilot} has no complete program_part_launchpad (USERPROGRAM field map{detail}). It is "
                        "Launchpad ingestion configuration, which this tool never writes; ask the environment owner to "
                        "configure it, then re-run. Nothing was created.")


def program_part_problems(cfg: dict) -> tuple[list[str], list[str]]:
    """-> (missing keys, keys whose fieldPosition differs from the USERPROGRAM layout). Read-only."""
    miss, wrong = [], []
    for k, pos in PROGRAM_PART_FIELDS.items():
        v = mc.cfg(cfg, k)
        if not v:
            miss.append(k)
            continue
        try:
            if json.loads(v).get("fieldPosition") != pos:
                wrong.append(k)
        except (ValueError, AttributeError):
            wrong.append(k)
    return miss, wrong


def require_program_part() -> None:
    """Read-only prerequisite: stop BEFORE any upload when the target cannot ingest a USERPROGRAM file."""
    miss, wrong = program_part_problems(mc.read_config("pilot", mc.PILOT_ID, PROGRAM_PART_CT))
    if miss or wrong:
        raise SetupError(BLOCKED_PROGRAM_PART.format(pilot=mc.PILOT_ID, detail=(f"; missing {miss}" if miss else "")
                                                     + (f"; positions differ {wrong}" if wrong else "")))


# Strings the BB section dereferences with no null guard (a missing one NPEs the whole MS render). Present on the target pilot.
BB_STRINGS = {
    "com.bidgely.cloud.bb.your.cumulative.balance.title": "Your Cumulative Balance",
    "com.bidgely.cloud.bb.over.budget.insight.text": "As of now, you're maintaining the <strong>negative balance of "
        "<span style=\"color:#1F73E3;\">${bbCumulativeCostDifference}</span></strong> due to more than budgeted consumption.",
    "com.bidgely.cloud.bb.under.budget.insight.text": "As of now, you're maintaining the <strong>positive balance of "
        "<span style=\"color:#1F73E3;\">${bbCumulativeCostDifference}</span></strong> due to less than budgeted consumption.",
    "com.bidgely.cloud.bb.equal.to.budget.insight.text": "As of now, you're consumption is matching the Budget Bill.",
    "com.bidgely.cloud.bb.actual.cost.legend.text": "Cumulative Actual Cost",
    "com.bidgely.cloud.bb.budget.bill.legend.text": "Cumulative Budget Bill",
    "com.bidgely.cloud.bb.projected.dashed.line.legend.text": "Projected",
    "com.bidgely.cloud.bb.true.up.text": "True-up*",
    "com.bidgely.cloud.bb.true.up.disclaimer.text": "*The cumulative difference between the actual cost and the budget bill "
        "is settled at the true-up.",
}


class Check:
    def __init__(self, cid: str, name: str, status: str, detail: str):
        self.id, self.name, self.status, self.detail = cid, name, status, detail


# ---------------------------------------------------------------------------
# file generation (called from mds_common.generate when --budget-billing)
# ---------------------------------------------------------------------------
def add_budget_billing(run: Run, a: argparse.Namespace, ctx: dict, fdir: Path, names: dict, stamp: str) -> None:
    m = run.m
    cyc, ident = m["cycles"], m["identity"]
    n = max(1, min(a.bb_elapsed_cycles, len(cyc)))
    if a.bb_trueup_period <= n:
        raise SetupError(f"--bb-trueup-period ({a.bb_trueup_period}) must exceed --bb-elapsed-cycles ({n}), otherwise the "
                         "period is already complete and nothing is projected")
    bb_cycles = cyc[-n:]
    totals = [float(c["E"]["total"]) for c in bb_cycles]
    amount = round(a.bb_amount, 2) if a.bb_amount > 0 else max(10.0, math.ceil(sum(totals) / n * 1.1 / 5) * 5.0)
    start = date.fromisoformat(bb_cycles[0]["start"])
    meter = ident["meters"]["ELECTRIC"]
    inv = Path(m["files"]["INVOICE"])
    rows = []
    for c in bb_cycles:
        pre = (f"{ident['customer']}|{ident['account']}|{ident['premise']}|{meter}|ELECTRIC|{m['billCycleCode']}|"
               f"{c['start']}|{c['end']}|{(date.fromisoformat(c['end']) - date.fromisoformat(c['start'])).days}")
        rows.append(f"{pre}|{CHARGE_NAME}|BB_AMOUNT|0.000|{amount:.2f}|AMI|")
    inv.write_text(inv.read_text() + "\n".join(rows) + "\n")
    try:
        utility = mc.cfg(mc.read_config("pilot", mc.PILOT_ID, "launchpad_ingestion_configs"), "utility_name") or DEFAULT_UTILITY
    except (SetupError, OSError):
        utility = DEFAULT_UTILITY or "dry-run-utility"   # dry run: offline
    name = f"USERPROGRAM_{utility}_{stamp}_T1.txt"
    row = "|".join([ident["customer"], ident["account"], ident["premise"], PROGRAM_ID, start.strftime(a.bb_date_format), "",
                    "true", "true", "", json.dumps({"BB_TrueUp_Period": a.bb_trueup_period})])
    (fdir / name).write_text(row + "\n")
    m["files"]["USERPROGRAM"] = str(fdir / name)
    m["fileOrder"] = m["fileOrder"] + ["USERPROGRAM"]
    # expectation the email/web checks compare against: the chart's cumulative series use per-cycle Math.round
    act = [int(math.floor(t + 0.5)) for t in totals]
    bud = int(math.floor(amount + 0.5))
    m["budgetBilling"] = {
        "programId": PROGRAM_ID, "amount": amount, "trueUpPeriod": a.bb_trueup_period, "elapsedCycles": n,
        "programStart": start.isoformat(), "utility": utility, "file": name,
        "cycles": [{"start": c["start"], "end": c["end"], "total": t} for c, t in zip(bb_cycles, totals)],
        "expectedCumulativeActual": sum(act), "expectedCumulativeBudget": bud * n,
        "expectedDifference": bud * n - sum(act),
    }
    log(f"BUDGET BILLING: BB_AMOUNT ${amount:.2f} on {n} cycles from {start} (true-up every {a.bb_trueup_period}); "
        f"actual ${sum(act)} vs budget ${bud * n} -> expected balance {m['budgetBilling']['expectedDifference']:+d}; "
        f"program file {name}")


# ---------------------------------------------------------------------------
# readers
# ---------------------------------------------------------------------------
def programs(uuid: str) -> list[dict]:
    r = payload(api("GET", f"v3.0/internal/users/{uuid}/homes/{mc.HID}/account/programs", allow=(404, 500))) or {}
    return [p for p in (r.get("program-data") or []) if isinstance(p, dict)] if isinstance(r, dict) else []


def find_key(obj, key: str):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            if (hit := find_key(v, key)) is not None:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            if (hit := find_key(v, key)) is not None:
                return hit
    return None


def widget_bb(uuid: str) -> dict:
    r = api("GET", f"v2.0/dashboard/users/{uuid}/usage-widget-data?measurement-type=ELECTRIC"
                   "&date-format=MONTH_DAY_SPACE&locale=en_US", allow=(404, 500))
    return find_key(payload(r), "budgetBillingInfo") or {}


def ingest_program(run: Run, a: argparse.Namespace, s3, bucket: str) -> None:
    """USERPROGRAM goes after the invoices: the programme row needs the user and the BB cycles must already exist."""
    m, uuid, bb = run.m, run.m["uuid"], run.m["budgetBilling"]
    step("UPLOAD USERPROGRAM (Budget Billing enrolment), confirmed through the programs API + the web widget")
    mc.upload(run, s3, bucket, Path(m["files"]["USERPROGRAM"]))

    def prog_ok():
        rows = [p for p in programs(uuid) if p.get("programId") == PROGRAM_ID]
        return bool(rows), f"{len(rows)} {PROGRAM_ID} row(s)"
    mc.poll("USERPROGRAM -> programs API", prog_ok, a.invoice_wait, a.poll)

    def web_ok():
        w = widget_bb(uuid)
        return bool(w.get("isBBUser")), f"budgetBillingInfo={json.dumps(w)[:160]}"
    try:
        mc.poll("web budgetBillingInfo.isBBUser", web_ok, a.invoice_wait, a.poll)
    except mc.WaitTimeout as exc:
        log(f"{exc} -- continuing: the verify phase reports it", "WARN")
    m["bbIngestedAt"] = time.time()
    run.save()
    bb["ingested"] = True


# ---------------------------------------------------------------------------
# configure
# ---------------------------------------------------------------------------
def ensure_pilot_bb(run: Run | None) -> dict[str, dict[str, str]]:
    """Write MISSING PILOT_BB_CONFIGS keys on pilot the target pilot (never overwrites a set value; pre-change config backed up)."""
    written: dict[str, dict[str, str]] = {}
    for ct, vals in PILOT_BB_CONFIGS.items():
        cur = mc.read_config("pilot", mc.PILOT_ID, ct)
        todo = {k: v for k, v in vals.items() if not mc.cfg(cur, k)}
        if not todo:
            log(f"  pilot {mc.PILOT_ID} {ct}: BB keys already set ({len(vals)})")
            continue
        raw = api("GET", f"entities/pilot/{mc.PILOT_ID}/configs/{ct}", allow=(404,))
        bk = HERE.parent / "backups" / f"pilot{mc.PILOT_ID}_{ct}_before_bb_{datetime.now():%Y%m%d_%H%M%S}.json"
        bk.parent.mkdir(exist_ok=True)
        bk.write_text(json.dumps(raw, indent=2))
        kvs = [{"configKey": k, "configVal": v, "configDataType": "TEXT", "configRegex": "(?s).*",
                "configDocumentation": f"Budget Billing, set by mds_bb.py on {datetime.now():%Y-%m-%d}"} for k, v in todo.items()]
        api("POST", f"entities/{mc.PILOT_ID}/configs", {"configType": ct, "configKVs": kvs})
        after = mc.read_config("pilot", mc.PILOT_ID, ct)
        bad = {k: mc.cfg(after, k) for k in todo if mc.cfg(after, k) != todo[k]}
        log(f"  PILOT {mc.PILOT_ID} {ct} <- {sorted(todo)} (backup {bk.name})" + (f" READBACK MISMATCH {bad}" if bad else ""),
            "WARN" if bad else "INFO")
        written[ct] = todo
        if run:
            run.mutate("PILOT_config", entity=str(mc.PILOT_ID), configType=ct, values=todo, backup=str(bk))
            run.m["lastConfigWriteAt"] = time.time()
    return written


def bb_sections(uuid: str) -> list[dict]:
    """The user's DCX Monthly Summary list with BB_CUMULATIVE_BALANCE right after the itemization block (idempotent)."""
    base = [s for s in mt.dcx_ms_sections(uuid) if s.get("emailTemplateSection") != BB_SECTION]
    names = [s.get("emailTemplateSection") for s in base]
    at = next((i + 1 for i, x in enumerate(names) if x in ("ITEMIZATION_V2", "ITEMIZATION", "DF_ITEMIZATION")), len(base))
    return base[:at] + [mt.sec(BB_SECTION)] + base[at:]


def configure(run: Run, a: argparse.Namespace) -> None:
    uuid = run.m["uuid"]
    step(f"CONFIGURE Budget Billing (pilot BB keys + USER-scope configs + MS section list) for {uuid}")
    if not a.no_pilot_config:
        ensure_pilot_bb(run)
    mc.apply_user_configs(run, mc.user_configs(tuple(run.m.get("fuels") or ("ELECTRIC",))))
    for rid, text in BB_STRINGS.items():
        cur = (payload(api("GET", f"2.1/stringResources/user/{uuid}/resource/{rid}?locale=en_US", allow=(400, 404, 500))) or {})
        if isinstance(cur, dict) and cur.get(rid):
            continue
        log(f"  string {rid} missing at user scope: seeding", "WARN")
        for entity in (f"{uuid}.{MS}", f"{uuid}.{MS}.ELECTRIC", uuid):
            mc.put_user_string(run, entity, rid, text)
    lst = json.dumps(bb_sections(uuid), separators=(",", ":"))
    mc.apply_user_configs(run, {"email_template_sections_config": {f"{MS}.OPT_IN": lst, f"{MS}.OPT_OUT": lst}})
    run.m["bbConfigured"] = {"at": time.time()}
    run.save()


# ---------------------------------------------------------------------------
# verify (programs API + web) and email
# ---------------------------------------------------------------------------
def ms_list(uuid: str) -> list[str]:
    """Section names of the Monthly Summary list the platform resolves for this user (ELECTRIC)."""
    r = payload(api("GET", f"entities/user/{uuid}/configs/email_template_sections_config?context={MS}"
                           "&measurement-type=ELECTRIC", allow=(404, 500))) or {}
    try:
        kv = {x["key"]: x for x in json.loads(r.get("email_template_sections_config") or "{}").get("kvs", [])}
        return [s.get("emailTemplateSection") for s in json.loads(kv[f"{MS}.OPT_OUT"]["val"])]
    except (KeyError, TypeError, ValueError, AttributeError):
        return []


def marker_size(s3, key: str) -> int | None:
    try:
        return s3.head_object(Bucket=mc.MARKER_BUCKET, Key=mc.MARKER_PREFIX + key.rsplit(".", 1)[0] + ".processed")["ContentLength"]
    except Exception:  # noqa: BLE001
        return None


def prerequisites(run: Run, a: argparse.Namespace, add) -> None:
    """P01-P09: everything a BB user depends on that is NOT per-user (pilot config, strings, palette) + the ingestion
    markers of the two BB-specific files. Read-only."""
    m = run.m
    cfgs = {ct: mc.read_config("pilot", mc.PILOT_ID, ct) for ct in (*PILOT_BB_CONFIGS, PROGRAM_PART_CT)}
    pm = mc.cfg(cfgs["budget_billing_configs"], "bb_program_ids_map")
    try:
        ok = PROGRAM_ID in (json.loads(pm).get("ELECTRIC") or "")
    except ValueError:
        ok = False
    add("P01", "pilot budget_billing_configs.bb_program_ids_map has ELECTRIC", "PASS" if ok else "FAIL", pm or "absent")
    miss, wrong = program_part_problems(cfgs[PROGRAM_PART_CT])
    want = PROGRAM_PART_FIELDS
    add("P02", "pilot program_part_launchpad (USERPROGRAM field positions; read-only, never written)",
        "PASS" if not miss and not wrong else "FAIL",
        f"{len(want) - len(miss)}/{len(want)} keys" + (f"; missing {miss} -> ingester NPE validateDates:337 (500.2001)" if miss else "")
        + (f"; positions differ {wrong}" if wrong else ""))
    pal = cfgs[PALETTE_ID]
    miss = [k for k in PILOT_BB_CONFIGS[PALETTE_ID] if not mc.cfg(pal, k)]
    add("P03", "pilot BB chart palette (colour 1/4/43/86-90)", "PASS" if not miss else "FAIL",
        "all 8 colours set" if not miss else f"missing {miss}: an undefined ${{colorNN}} aborts the whole MS render")
    pid = mc.cfg(cfgs["bb_cumulative_balance_section_configs"], "color_palette_id")
    add("P04", "pilot bb_cumulative_balance_section_configs.color_palette_id", "PASS" if pid == PALETTE_ID else "WARN",
        f"{pid!r} (want {PALETTE_ID!r})")
    absent = []
    for rid in BB_STRINGS:
        r = payload(api("GET", f"2.1/stringResources/pilot/{mc.PILOT_ID}/resource/{rid}?locale=en_US", allow=(400, 404, 500))) or {}
        if not (isinstance(r, dict) and r.get(rid)):
            absent.append(rid.split("com.bidgely.cloud.")[-1])
    add("P05", "pilot BB strings (9, dereferenced without a null guard)", "PASS" if not absent else "WARN",
        "all 9 present" if not absent else f"absent at pilot scope {absent} (configure seeds them at user scope)")
    try:
        s3 = mc.aws(a.aws_profile).client("s3")
    except SetupError as exc:
        add("P06", "ingestion marker USERPROGRAM", "WARN", f"no AWS access: {exc}")
        add("P07", "ingestion marker INVOICE", "WARN", "no AWS access")
    else:
        for cid, key, label in (("P06", "USERPROGRAM", "USERPROGRAM"), ("P07", "INVOICE", "INVOICE")):
            size = marker_size(s3, Path(m["files"][key]).name)
            # a same-name re-upload does not rewrite the marker: an old rejection stays; the API row is then the proof
            reuploaded = sum(1 for u in m.get("uploads", []) if u["key"] == Path(m["files"][key]).name) > 1
            proven = key == "USERPROGRAM" and bool([x for x in programs(m["uuid"]) if x.get("programId") == PROGRAM_ID])
            st = "PASS" if size == 0 else ("WARN" if size is None or (reuploaded and proven) else "FAIL")
            add(cid, f"ingestion marker {label} (0 bytes = accepted)", st,
                "not written yet (the marker job can lag hours)" if size is None else
                ("accepted" if size == 0 else f"{size}-byte report in s3://{mc.MARKER_BUCKET}/{mc.MARKER_PREFIX}"
                 + (" -- STALE: file was re-uploaded after the pilot fix and the programs API row exists (marker is not rewritten)"
                    if st == "WARN" else " -- REJECTION")))
    inv = Path(m["files"]["INVOICE"]).read_text().splitlines()
    rows = [ln.split("|") for ln in inv if "|BB_AMOUNT|" in ln]
    pos = all(float(r[-3]) > 0 for r in rows)
    add("P08", "generated BB_AMOUNT rows (positive, one per BB cycle)", "PASS" if rows and pos and len(rows) == m["budgetBilling"]["elapsedCycles"] else "FAIL",
        f"{len(rows)} rows, all positive={pos}, expected {m['budgetBilling']['elapsedCycles']}")
    add("P09", "BB_AMOUNT excluded from TOTAL (file identity)", "PASS" if _total_excludes_bb(m) else "FAIL",
        "TOTAL == CONSUMPTION_BASED + FEED_IN + FIXED on every BB cycle")


def _total_excludes_bb(m: dict) -> bool:
    ok = True
    for c in m["cycles"][-m["budgetBilling"]["elapsedCycles"]:]:
        e = c["E"]
        ok &= abs(e["total"] - (e["dCost"] + e["rCost"] + e["fixed"])) < 0.02
    return ok


def verify(run: Run, a: argparse.Namespace) -> list[Check]:
    m, uuid, bb = run.m, run.m["uuid"], run.m["budgetBilling"]
    step(f"VERIFY Budget Billing state for {uuid}: programs API + web budgetBillingInfo")
    checks: list[Check] = []

    def add(cid, name, status, detail):
        checks.append(Check(cid, name, status, detail))
        log(f"  {cid} {status:4} {name}: {detail}", "ERROR" if status == "FAIL" else "INFO")
    prerequisites(run, a, add)
    rows = [p for p in programs(uuid) if p.get("programId") == PROGRAM_ID]
    row = rows[0] if rows else {}
    attrs = row.get("programAttributes") or {}
    tu = attrs.get("BB_TrueUp_Period")
    add("B01", f"{PROGRAM_ID} enrolment row", "PASS" if row and row.get("isParticipating") is not False else "FAIL",
        f"row={json.dumps({k: row.get(k) for k in ('programId', 'isEligible', 'isParticipating', 'userParticipationStartDate')})}")
    add("B02", "BB_TrueUp_Period positive and as requested", "PASS" if tu is not None and int(float(tu)) == bb["trueUpPeriod"] else "FAIL",
        f"programAttributes.BB_TrueUp_Period={tu} want {bb['trueUpPeriod']} (a missing/zero value hides the cumulative section)")
    w = widget_bb(uuid)
    m["bbWidget"] = w
    add("B03", "web: budgetBillingInfo.isBBUser", "PASS" if w.get("isBBUser") else "FAIL", json.dumps(w)[:200])
    if w.get("isBBUser"):
        amt = float(w.get("bbAmount") or 0)
        add("B04", "web: bbAmount == BB_AMOUNT invoice rows", "PASS" if abs(amt - bb["amount"]) < 0.51 else "FAIL",
            f"bbAmount {amt} want {bb['amount']}")
        last = bb["cycles"][-1]["total"]
        got = float(w.get("actualBillAmount") or 0)
        add("B05", "web: actualBillAmount == latest invoice total", "PASS" if abs(got - last) < 1.01 else "WARN",
            f"actualBillAmount {got} invoice total {last}")
        add("B06", "web: trueUpPeriod / trueUpDate", "PASS" if w.get("trueUpPeriod") == bb["trueUpPeriod"] and w.get("trueUpDate") else "WARN",
            f"trueUpPeriod={w.get('trueUpPeriod')} trueUpDate={w.get('trueUpDate')}")
    # ---- extra BB state checks -------------------------------------------------------------------------------------
    start_ep = row.get("userParticipationStartDate")
    tzl = ZoneInfo(m["timezone"])
    try:
        if isinstance(start_ep, str) and not start_ep.isdigit():          # API returns ISO-8601 UTC, e.g. 2026-03-01T09:00:00Z
            got_start = datetime.fromisoformat(start_ep.replace("Z", "+00:00")).astimezone(tzl).date().isoformat()
        else:
            got_start = datetime.fromtimestamp(int(start_ep), tzl).date().isoformat() if start_ep else None
    except (TypeError, ValueError):
        got_start = str(start_ep)
    add("B09", "programme start date == requested", "PASS" if got_start == bb["programStart"] else "FAIL",
        f"stored {got_start} want {bb['programStart']} ")
    tu_date = w.get("trueUpDate")
    if tu_date:
        try:
            got = datetime.strptime(f"{tu_date} 2000", "%b %d %Y")
            exp = mc.add_months(date.fromisoformat(bb["programStart"]), bb["trueUpPeriod"])      # exclusive cycle end
            diff = min(abs((date(2000, exp.month, exp.day) - got.date()).days), 365 - abs((date(2000, exp.month, exp.day) - got.date()).days))
            add("B10", "web trueUpDate ~ programme start + true-up period", "PASS" if diff <= 1 else "WARN",
                f"widget {tu_date!r}; start {bb['programStart']} + {bb['trueUpPeriod']} cycles ends {exp} (exclusive): {diff} day(s) apart"
                + ("" if diff <= 1 else " -- see BUGS_budget_billing_mds.md L2"))
        except ValueError:
            add("B10", "web trueUpDate ~ programme start + true-up period", "WARN", f"unparseable {tu_date!r}")
    t0, t1 = m["cycles"][0]["startTs"] - 86400, int(time.time()) + 86400
    ud = mc.utility_data(uuid, "ELECTRIC", t0, t1)
    good = sum(1 for c in m["cycles"] if (h := mc.near(ud, c["startTs"])) and not h[1].get("bidgelyGeneratedInvoice")
               and abs(mc.inv_cost(h[1], "ELECTRIC") - c["E"]["total"]) < 0.02)
    add("B11", "all invoiced cycles carry the generated TOTAL (BB rows did not leak into it)", "PASS" if good == len(m["cycles"]) else "FAIL",
        f"{good}/{len(m['cycles'])} cycles match")
    lst = ms_list(uuid)
    add("B12", f"platform-resolved MS section list contains {BB_SECTION}", "PASS" if BB_SECTION in lst else "FAIL",
        f"{len(lst)} sections, BB at {lst.index(BB_SECTION) + 1 if BB_SECTION in lst else '-'}"
        + ("" if BB_SECTION in lst else " (run --phases configure: writes the USER-scope list)"))
    user_cfg = mc.read_config("user", uuid, "email_template_sections_config")
    mine = mc.cfg(user_cfg, f"{MS}.OPT_OUT")
    add("B13", "USER-scope MS list written by this script", "PASS" if BB_SECTION in mine else "WARN",
        "contains BB" if BB_SECTION in mine else "not written yet (configure phase)")
    home_row = mt.home(uuid)
    add("B14", "non-solar home (solarUser not true)", "FAIL" if str(home_row.get("solarUser")).lower() == "true" else "PASS",
        f"solarUser={home_row.get('solarUser')}")
    link = mc.dashboard_link(run, "budget billing user")
    add("B15", "passwordless web dashboard link", "PASS" if link.get("url") else "WARN",
        f"{link.get('url') or link.get('error') or link}")
    add("B16", "BillProjection", "INFO", "not generated on MDS (known platform limitation): intentionally not checked")
    return finish(run, "verify", checks)


def email(run: Run, a: argparse.Namespace) -> list[Check]:
    m, uuid, bb = run.m, run.m["uuid"], run.m["budgetBilling"]
    step(f"BUDGET BILLING Monthly Summary (variant MS_BB, section {BB_SECTION}) for {uuid}")
    checks: list[Check] = []

    def add(cid, name, status, detail):
        checks.append(Check(cid, name, status, detail))
        log(f"  {cid} {status:4} {name}: {detail}", "ERROR" if status == "FAIL" else "INFO")
    if a.no_send and m.get("bbEmailTriggeredAt"):
        since = int(m["bbEmailTriggeredAt"])           # re-validate the email already sent by an earlier run
        rows = mt.rows_of(uuid, MS, since)
    else:
        if not m.get("bbConfigured"):
            configure(run, a)
        mc.rewind_ms_state(run, "ELECTRIC")
        mt.settle(run, a)
        since = int(time.time())
        mc.sqs_send(run, mc.aws(a.aws_profile).client("sqs"), mc.NOTIF_QUEUE,
                    json.dumps({"userId": uuid, "homeOrdinal": mc.HID, "metaData": {"EventName": MS}}), MS)
        m["bbEmailTriggeredAt"] = since
        run.save()
        rows = mt.wait_rows(uuid, MS, since, a.email_wait, a.poll)
    if not rows:
        add("E01", "Monthly Summary generated", "FAIL", f"no {MS} Email row after {a.email_wait}s; logs below")
        mt.diagnose(run, MS)
        return finish(run, "email", checks)
    row = rows[0]
    htmlb, text, f = mt.save_email(run, row, "BB_MS")
    low = text.lower()
    variant = (row.get("eventData") or {}).get("email_variant_code")
    m["bbEmail"] = {k: row.get(k) for k in ("notificationId", "notificationTitle", "deliveryDestination",
                                            "generationTimestamp")} | {"variant": variant, "file": str(f)}
    add("E01", "Monthly Summary generated", "PASS", f"{row.get('notificationTitle')!r} to={row.get('deliveryDestination')} -> {f.name}")
    codes = [x.strip() for x in str(variant or "").split(",") if x.strip()]
    add("E02", "variant includes MS_BB", "PASS" if "MS_BB" in codes else "FAIL", f"email_variant_code={variant}")
    has_title = "your cumulative balance" in low
    img_rx = mds_target.chart_image_host_regex()             # the target's chart service host (profile), never assumed
    charts = re.findall(rf'<img[^>]+src="(https://(?:{img_rx})[^"]+)"', htmlb) if img_rx else []
    add("E03", "BB section rendered (title + chart image)", ("PASS" if has_title and charts else "FAIL") if img_rx else
        ("WARN" if has_title else "FAIL"), f"title present={has_title}; html-to-image charts {len(charts)}"
        + ("" if img_rx else " (chart host not configured: email.provisioner.target.chart_image_host_regex; charts not checked)"))
    ins = re.search(r"(positive|negative) balance of\s*\$?\s*([\d,]+)", low)
    equal = "matching the budget bill" in low
    want = bb["expectedDifference"]
    if ins:
        sign = 1 if ins.group(1) == "positive" else -1
        shown = sign * int(ins.group(2).replace(",", ""))
        add("E04", "balance insight == actual vs budget", "PASS" if shown == want else "WARN",
            f"email says {shown:+d}; expected {want:+d} = budget ${bb['expectedCumulativeBudget']} - actual "
            f"${bb['expectedCumulativeActual']} over {bb['elapsedCycles']} cycles (actual cost is read from the invoice cost)")
    elif equal:
        add("E04", "balance insight == actual vs budget", "PASS" if want == 0 else "WARN", f"email says matching budget; expected {want:+d}")
    else:
        add("E04", "balance insight == actual vs budget", "FAIL", f"no insight sentence found; expected {want:+d}")
    add("E05", "True-up disclaimer", "PASS" if "true-up" in low or "true up" in low else "WARN", "True-up text present" if "true-up" in low else "absent")
    add("E06", "Budget Billing disclaimer", "PASS" if "budget billing customer" in low else "WARN",
        "ms bbDisclaimer present" if "budget billing customer" in low else "absent (only on BB MS layouts that carry it)")
    vals = mh.money(text)
    last = m["cycles"][-1]["E"]
    add("E07", "bill amount = invoice", "PASS" if mh.has_money(vals, last["total"]) or mh.has_money(vals, last["energy"]) else "WARN",
        f"invoice total ${last['total']} energy ${last['energy']}; $ shown {vals[:8]}")
    mt.tmpl(add, "E08", htmlb)
    issues = [x for x in mh.branding_issues(htmlb, text) if not (img_rx and re.search(img_rx, x))]
    add("E09", "generic branding + links", "WARN" if issues else "PASS", "; ".join(issues[:6]) or "clean")
    st = payload(api("GET", mc.ms_state_path(uuid, "ELECTRIC"), allow=(404, 500))) or {}
    add("E10", "sent-state", "PASS" if int((st or {}).get("sentCount") or 0) >= 1 else "WARN", json.dumps(st)[:160])
    # ---- extra email checks ----------------------------------------------------------------------------------------
    bad_img = []
    for u in charts:
        try:
            with urllib.request.urlopen(urllib.request.Request(u, method="GET"), timeout=60) as resp:
                if resp.status != 200 or not str(resp.headers.get("Content-Type", "")).startswith("image/") or int(resp.headers.get("Content-Length") or 1) < 500:
                    bad_img.append(f"{resp.status} {resp.headers.get('Content-Type')} {resp.headers.get('Content-Length')}")
        except Exception as exc:  # noqa: BLE001
            bad_img.append(f"{type(exc).__name__}: {str(exc)[:80]}")
    add("E11", "every html-to-image chart (BB chart included) fetches as a real image", "PASS" if charts and not bad_img else "FAIL",
        f"{len(charts)} chart(s), problems: {bad_img or 'none'}")
    add("E12", "BB section appears exactly once", "PASS" if low.count("your cumulative balance") == 1 else "FAIL",
        f"title occurrences {low.count('your cumulative balance')}")
    ident_email = m["identity"]["email"]
    add("E13", "delivered to the generated user", "PASS" if row.get("deliveryDestination") == ident_email and row.get("deliveryMode") == "Email" else "WARN",
        f"to={row.get('deliveryDestination')} mode={row.get('deliveryMode')} want {ident_email}")
    add("E14", "one MS email for this trigger (no duplicates)", "PASS" if len(rows) == 1 else "WARN", f"{len(rows)} MS Email row(s) since the trigger")
    st_start = (st or {}).get("billStartTs")
    add("E15", "sent-state billStartTs == MS cycle start", "PASS" if st_start == m["cycles"][-1]["startTs"] else "WARN",
        f"state {st_start} cycle {m['cycles'][-1]['startTs']} ({m['cycles'][-1]['start']})")
    w = widget_bb(uuid)
    add("E16", "email bill amount matches the web widget's actualBillAmount", "PASS" if w.get("actualBillAmount") is not None and mh.has_money(vals, float(w["actualBillAmount"])) else "WARN",
        f"widget actualBillAmount {w.get('actualBillAmount')}; $ in email {vals[:8]}")
    add("E17", "BB amount consistent: widget bbAmount == generated", "PASS" if abs(float(w.get("bbAmount") or 0) - bb["amount"]) < 0.51 else "FAIL",
        f"widget {w.get('bbAmount')} generated {bb['amount']}")
    add("E18", "projection wording absent (BillProjection not generated on MDS)", "INFO",
        "present in email text" if "projected bill" in low else "no projected-bill wording (expected)")
    return finish(run, "email", checks)



def aggregate(run: Run, a: argparse.Namespace) -> None:
    """Disagg + aggregation (mds_common aggregate phase) for a run whose user phase stopped before it (resume path)."""
    step("AGGREGATE (mds_common aggregate phase: disagg, aggregation, itemization)")
    argv = ["--manifest", str(run.out / "manifest.json"), "--phases", "aggregate", "--budget-billing"]
    for flag, val in (("--token-file", a.token_file), ("--aws-profile", a.aws_profile)):
        if val:
            argv += [flag, val]
    fh = mc._LOG_FH
    rc = mc.main("bb-aggregate", ("ELECTRIC",), str(run.out.parent), argv)
    mc._LOG_FH = fh if fh and not fh.closed else open(run.dir("logs") / "run.log", "a")
    run.m.update(json.loads((run.out / "manifest.json").read_text()))
    if rc != 0:
        raise SetupError(f"aggregation exited {rc}")


def summary(run: Run, checks: list[Check]) -> None:
    """One page with everything the requester needs: the user, the web link, the email, the verdicts."""
    m = run.m
    em, dash, bb = m.get("bbEmail", {}), m.get("dashboard", {}), m.get("budgetBilling", {})
    fails = [c for c in checks if c.status == "FAIL"]
    warns = [c for c in checks if c.status == "WARN"]
    lines = [f"# Budget Billing user on pilot {mc.PILOT_ID}", "",
             f"- uuid: `{m.get('uuid')}`   email: `{m.get('identity', {}).get('email')}`   external id: `{m.get('identity', {}).get('externalUserId')}`",
             f"- Budget Billing: ${bb.get('amount')} per cycle, {bb.get('elapsedCycles')} cycles elapsed of {bb.get('trueUpPeriod')}, "
             f"programme start {bb.get('programStart')}, expected balance {bb.get('expectedDifference', 0):+d}",
             f"- WEB dashboard (passwordless, test user): {dash.get('url', '(not generated)')}",
             f"- WEB widget: {json.dumps(m.get('bbWidget', {}))}",
             f"- MS EMAIL: {em.get('notificationTitle', '(not sent)')!r}, variant {em.get('variant')}, saved at `{em.get('file', '-')}`",
             f"- verdict: {'FAIL' if fails else 'PASS'}  ({len(checks)} checks, {len(fails)} FAIL, {len(warns)} WARN)", ""]
    for c in fails + warns:
        lines.append(f"- {c.status} {c.id} {c.name}: {c.detail}")
    (run.dir("reports") / "BB_SUMMARY.md").write_text("\n".join(lines) + "\n")
    log(f"SUMMARY -> {run.dir('reports') / 'BB_SUMMARY.md'}: {'FAIL' if fails else 'PASS'}; email {em.get('file', 'not sent')}; "
        f"web link {'generated' if dash.get('url') else 'MISSING'}")


def finish(run: Run, tag: str, checks: list[Check]) -> list[Check]:
    run.m[f"bb_{tag}_validation"] = [c.__dict__ for c in checks]
    run.save()
    em = run.m.get("bbEmail", {})
    lines = [f"# Budget Billing {tag} -- {run.m.get('uuid')}", "",
             f"email: {em.get('file', '-')}  subject: {em.get('notificationTitle', '-')!r}", "",
             "| id | check | status | detail |", "|---|---|---|---|"]
    lines += [f"| {c.id} | {c.name} | {c.status} | {c.detail.replace('|', '/')} |" for c in checks]
    (run.dir("reports") / f"bb_{tag}_validation_report.md").write_text("\n".join(lines) + "\n")
    log(f"BB {tag.upper()} VALIDATION " + ("FAIL" if any(c.status == "FAIL" for c in checks) else "PASS") + ": "
        + " ".join(f"{c.id}={c.status}" for c in checks))
    return checks


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def parse(doc: str, default_phases: str, argv: list[str] | None) -> tuple[argparse.Namespace, list[str]]:
    p = argparse.ArgumentParser(description=doc, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    p.add_argument("--phases", default=default_phases, help=f"comma list of {','.join(PHASES)} or all")
    p.add_argument("--uuid", help="existing BB user created by create_mds_budget_billing_user.py")
    p.add_argument("--manifest")
    p.add_argument("--output-dir", default=str(HERE / "results" / "budget_billing"))
    p.add_argument("--token-file")
    p.add_argument("--aws-profile")
    p.add_argument("--allow-auto-ms", action="store_true", help="let the aggregation pass send its own Monthly Summary as well")
    p.add_argument("--no-send", action="store_true", help="email phase: re-validate the email sent by an earlier run, send nothing")
    p.add_argument("--bb-date-format", default="%Y-%m-%d", help=argparse.SUPPRESS)
    p.add_argument("--no-pilot-config", action="store_true", help="skip the (additive, backed-up) PILOT-level BB keys")
    p.add_argument("--invoice-wait", type=int, default=1200, help="seconds to wait for the programs API after USERPROGRAM")
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
    # mds_common options we need for the user phase / reading values (zip, bb-*) pass through `rest`
    a.rest = rest
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
        try:
            mc._TOKEN = mc.get_token(a.token_file)
            # BEFORE any file is uploaded: without program_part_launchpad the USERPROGRAM file dies in the ingester, and
            # that config is never written by this tool (read-only prerequisite)
            require_program_part()
            if not a.no_pilot_config:
                step(f"PILOT {mc.PILOT_ID} Budget Billing enablement (additive, backed up; no-op when already set)")
                written = ensure_pilot_bb(None)
                if written:
                    log(f"pilot config written ({sorted(written)}); waiting {a.settle_config}s for the services' config cache")
                    time.sleep(a.settle_config)
        except SetupError as exc:
            print(f"pilot prerequisites/enablement failed: {exc}", file=sys.stderr)
            return 2
        out_dir = Path(a.output_dir).expanduser()
        base = ["--phases", "preflight,generate,upload,configure,aggregate", "--output-dir", str(out_dir), "--budget-billing"]
        if not a.allow_auto_ms:
            base.append("--hold-ms")          # one Monthly Summary per run: the BB one
        for flag, val in (("--token-file", a.token_file), ("--aws-profile", a.aws_profile)):
            if val:
                base += [flag, val]
        before = set(out_dir.glob("run_*"))
        rc = mc.main(doc, ("ELECTRIC",), str(out_dir), base + rest)
        manifest = mh.new_run_manifest(out_dir, before)
        if rc != 0 or manifest is None:
            print(f"user creation did not complete (exit {rc}, run {manifest.parent if manifest else '?'}); resume with "
                  "--manifest <run>/manifest.json --phases configure,verify,email", file=sys.stderr)
            return rc or 2
    if not manifest:
        print("nothing to do: pass --uuid/--manifest or include the user phase", file=sys.stderr)
        return 2
    run = Run(manifest.parent, json.loads(manifest.read_text()))
    if not run.m.get("budgetBilling"):
        print(f"{manifest} has no Budget Billing data: create the user with create_mds_budget_billing_user.py", file=sys.stderr)
        return 2
    mc.set_uuid_tag(run.m["uuid"])
    mc._LOG_FH = open(run.dir("logs") / "run.log", "a")
    log(f"BB run for {run.m['uuid']} phases {a.phase_list} dir {run.out}")
    checks: list[Check] = []
    try:
        mc._TOKEN = mc.get_token(a.token_file)
        if "pilot" in a.phase_list:
            step(f"PILOT {mc.PILOT_ID} Budget Billing enablement (additive, backed up)")
            ensure_pilot_bb(run)
            mt.settle(run, a)
        if "program" in a.phase_list:
            require_program_part()                # read-only prerequisite, before the USERPROGRAM upload
            sess = mc.aws(a.aws_profile)
            ingest_program(run, a, sess.client("s3"), mc.DEFAULT_BUCKET)
        if "aggregate" in a.phase_list:
            aggregate(run, a)
        if "configure" in a.phase_list:
            configure(run, a)
        if "verify" in a.phase_list:
            checks += verify(run, a)
        if "email" in a.phase_list:
            checks += email(run, a)
        summary(run, checks)
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
