#!/usr/bin/env python3
"""Enable rate comparison for a user (USER scope by default; PILOT only with --scope pilot) on the --profile target, then run the rate
comparison (RC) API for a user and check that its result can drive the TOU Rate Promotion section of the Monthly
Summary.

CONFIG  config type `rate_comparison` written at USER scope (--scope user, the default; needs --uuid). PILOT scope
        is a shared mutation and needs an explicit --scope pilot. The 12 keys are the fixed values in PILOT_DEFAULTS
        below. rate_plans_comparison_map keeps the pilot's existing entries and adds the user's current plan -> the
        priceable TOU plans cheapest for this user's RAW (--auto-candidates), or --map CUR:CAND[,CAND], or (with
        --auto-candidates 0) the TOU plan: --tou-plan-number, else the profile's email.rate_plans.tou resolved by name
        on the target (never a built-in number).

RC API  POST v3.0/users/<uuid>/homes/1/rate-comparison/candidate-rates?measurement-type=ELECTRIC computes and stores
        (Cassandra candidate_rate_computation) the cost of every candidate plan. It takes ~5 minutes: the call is
        made with a 10 minute timeout and retried on a transient 400/5xx (code 2211, --rc-retries 3), then the
        script waits --rc-settle (default 900 s) before reading, and polls
        GET .../rate-comparison?measurement-type=ELECTRIC until the result is stored (--rc-wait, default 600 s).

CHECKS  (the gates TouRatePromotionDataProvider / TouRatePromotionSectionContentProvider apply)
        RC01 current plan is a key of rate_plans_comparison_map      RC02 RC result stored (computation type)
        RC03 current plan + candidates priced                         RC04 best plan is TOU (display_only_for_tou_rate_plan)
        RC05 savings >= minimum_saving_amount                         RC06 savings % <= max_saving_threshold_in_percentage
        RC07 rate-resources planName for the best plan (the email shows it; absent row = section dropped)

RUN
    export BIDGELY_TOKEN=<target token>            # or --token-file, else the profile's api.token_file
    python3 mds_user_automation/enable_mds_rate_comparison.py --uuid <uuid>                  # USER config + RC API + checks
    python3 mds_user_automation/enable_mds_rate_comparison.py --uuid <uuid> --phases config
    python3 mds_user_automation/enable_mds_rate_comparison.py --uuid <uuid> --phases compute,check
    python3 mds_user_automation/enable_mds_rate_comparison.py --scope pilot --phases config  # pilot only, no user
Exit code: 0 = every check PASS/WARN, 1 = a check FAILed, 2 = setup error.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mds_common as mc
import mds_target  # noqa: E402
from mds_common import Run, SetupError, api, log, payload, status_of, step  # noqa: E402

HERE = Path(__file__).resolve().parent
CT = "rate_comparison"
PHASES = ("config", "compute", "check")
PROMO_CT = "tou_rate_promotion_section_configs"


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
PILOT_DEFAULTS = {
    "rate_comparison_enabled": "true",
    "computation_method": "BILLING_CYCLE_ACTUAL",
    "rate_comparison_computation_type_duration_map": json.dumps({
        "BILLING_CYCLE_ACTUAL": {"min_data_days_threshold": "345", "max_data_days_threshold": "350"},
        "BILLING_CYCLE_PROJECTED": {"min_data_days_threshold": "350", "max_data_days_threshold": "2147483647"}},
        separators=(",", ":")),
    "duration_in_months_for_rate_comparison": "12",
    "rate_plans_comparison_map": "{}",
    "check_computation_type_based_on_user_data": "false",
    "max_saving_threshold_in_percentage": "100",
    "target_duration_in_days_for_rate_comparison": "350",
    "raise_event_on_invoice_update": "true",
    "number_of_candidates_for_rate_comparison": "12",
    "use_minimum_365_days_for_rate_comparison": "true",
    "decimal_rounding_place": "0",
}


def live_plans() -> dict[int, dict]:
    plans = payload(api("GET", f"v3.0/rates/configuration/utilityId/{mc.PILOT_ID}", allow=(404,))) or []
    return {p["planNumber"]: p for p in plans if any(c.get("rates") for c in p.get("components", []))}


def is_tou(plan: dict | None) -> bool:
    return bool(plan) and any(r.get("touName") for c in plan.get("components", []) for r in c.get("rates", []))


def user_plan(uuid: str) -> int:
    h = payload(api("GET", f"meta/users/{uuid}/homes/{mc.HID}")) or {}
    try:
        sched = json.loads(h.get("ratesSchedule") or "[]")
        now = int(time.time())
        for s in sched:
            if s.get("measurementType", "ELECTRIC") == "ELECTRIC" and s.get("startTime", 0) <= now < s.get("endTime", 2 ** 31):
                return int(s["metaData"]["planNumber"])
    except (TypeError, ValueError, KeyError):
        pass
    return int(h.get("plannumber") or h.get("planNumber") or 0)


def estimate_annual_costs(m: dict, live: dict[int, dict], plans: list[int]) -> dict[int, float]:
    """Annual import cost per plan for this user's RAW_D (last 12 cycles), engine hour semantics (largest
    timeOfDayLow <= hour, Monday=1), non-holiday CONSUMPTION_BASED rows + 12 x MONTHLY_FIXED. Tiered plans are
    skipped. Used only to pick candidates the RC API is likely to rank below the current plan; the RC API's own
    numbers decide the checks (it agreed within 0.2% for 905 on 2026-09-29)."""
    from datetime import datetime as dt
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(m["timezone"])
    vals, _ = mc.parse_raw_file(m["files"]["RAW_D"], tz)
    lo, hi = m["cycles"][-12]["startTs"], m["cycles"][-1]["endTs"]
    scale = 1000.0 if str(m.get("units", {}).get("ELECTRIC", "Wh")).lower() == "wh" else 1.0
    hourly: dict[tuple, float] = {}
    for ts, v in vals.items():
        if lo <= ts < hi:
            loc = dt.fromtimestamp(ts, tz)
            k = (loc.month, loc.isoweekday(), loc.hour)
            hourly[k] = hourly.get(k, 0.0) + v / scale
    out: dict[int, float] = {}
    for n in plans:
        rows = [r for c in live[n].get("components", []) for r in c.get("rates", [])]
        if any(float(r.get("consumptionLow") or 0) > 0 for r in rows):
            continue
        cons = [r for r in rows if r["chargeType"] == "CONSUMPTION_BASED" and not r.get("isHoliday")
                and (r.get("attributes") or {}).get("data_stream", "DELIVERED") == "DELIVERED"
                and "RECEIVED" not in str(r.get("rateBandName") or "")]
        total, ok = 0.0, True
        for (mo, wk, h), kwh in hourly.items():
            best, bl = None, -1
            for r in cons:
                if r["monthLow"] <= mo <= r["monthHigh"] and r["weekLow"] <= wk <= r["weekHigh"] and bl < r["timeOfDayLow"] <= h:
                    best, bl = float(r["rate"]), r["timeOfDayLow"]
            if best is None:
                ok = False
                break
            total += kwh * best
        if ok:
            fixed = sum(float(r["rate"]) for r in rows if r["chargeType"] == "MONTHLY_FIXED" and not r.get("isHoliday")
                        and r["monthLow"] <= 1 <= r["monthHigh"])
            out[n] = round(total + 12 * fixed, 2)
    return out


def streams(plan: dict) -> set[str]:
    """data_stream of a plan dict whose rows carry attributes (/structure, DB). The configuration GET drops them --
    use plan_streams_by_number() for live plans."""
    return {(r.get("attributes") or {}).get("data_stream") or "DELIVERED"
            for c in plan.get("components", []) for r in c.get("rates", [])}


def plan_streams_by_number(plans: list[int]) -> dict[int, set[str]]:
    from copy_nve_rates_to_mds import db_streams
    return db_streams(plans)


def cached(n: int) -> bool:
    st = api("GET", f"v3.0/rates/utilities/{mc.PILOT_ID}/plans/{n}/structure", allow=(404, 500))
    return status_of(st) == 200 and bool(payload(st))


def auto_candidates(m: dict, live: dict[int, dict], cur: int, k: int, min_saving: float) -> tuple[list[int], dict]:
    """The k cheapest TOU plans that the RC API will actually price for this user and that beat the current plan by
    >= min_saving a year. The RC computer skips a candidate that lacks any active stream of the user's current plan
    (AbstractBillingCycleCandidateRateValuesComputer:275 -- an MDS D&R solar user needs RECEIVED rows too), and a
    plan the data-server rate cache has not loaded (/structure 404) prices to an empty result."""
    tou = [n for n, p in live.items() if n != cur and is_tou(p)]
    st = plan_streams_by_number([cur, *tou])
    need = st[cur]
    pool, skipped = [], {}
    for n in tou:
        if not need <= st[n]:
            skipped.setdefault("missing streams " + ",".join(sorted(need - st[n])), []).append(n)
        elif not cached(n):
            skipped.setdefault("not in the data-server rate cache (/structure 404)", []).append(n)
        else:
            pool.append(n)
    for why, ns in skipped.items():
        log(f"  candidate pool: {len(ns)} TOU plans skipped -- {why} (e.g. {sorted(ns)[:6]})", "WARN")
    est = estimate_annual_costs(m, live, [cur, *pool])
    if cur not in est:
        raise SetupError(f"cannot estimate the current plan {cur} for this user (tiered or incomplete rows): pass --map")
    good = sorted((c, n) for n, c in est.items() if n != cur and est[cur] - c >= min_saving)
    return [n for _, n in good[:k]], {"current": est[cur], **{str(n): c for c, n in good[:k]}}


def tou_target(a: argparse.Namespace, live: dict[int, dict]) -> int:
    """The TOU candidate plan: --tou-plan-number, else the profile's email.rate_plans.tou (a NAME) resolved on the target."""
    if getattr(a, "tou_plan_number", None) is not None:
        if a.tou_plan_number not in live:
            raise SetupError(f"--tou-plan-number {a.tou_plan_number} does not exist on pilot {mc.PILOT_ID} (or has no rates)")
        return int(a.tou_plan_number)
    name = mc.TOU_RATE_PLAN
    if not name:
        raise SetupError("no TOU plan: the profile states no email.rate_plans.tou; pass --tou-plan-number or --map CUR:CAND")
    hits = [n for n, p in live.items() if str(p.get("planName")) == str(name)]
    if len(hits) != 1:
        raise SetupError(f"the profile's TOU plan {name!r} resolves to {hits or 'no plan'} on pilot {mc.PILOT_ID}: "
                         "pass --tou-plan-number or --map CUR:CAND")
    return int(hits[0])


def build_config(a: argparse.Namespace, live: dict[int, dict], m: dict | None = None) -> tuple[dict[str, str], dict]:
    vals = dict(PILOT_DEFAULTS)
    # keep the pilot's existing map entries: the map is keyed by current plan, so one pilot serves many users
    cmap: dict[str, list[int]] = json.loads(mc.cfg(mc.read_config("pilot", mc.PILOT_ID, CT), "rate_plans_comparison_map", "{}") or "{}")
    dropped: list[str] = []
    extra: dict[str, list[int]] = {}
    for item in a.map:
        cur, cands = item.split(":", 1)
        extra[str(int(cur))] = [int(x) for x in cands.split(",") if x.strip()]
    estimate = {}
    if a.uuid and not a.map:
        cur = user_plan(a.uuid)
        if a.auto_candidates and m and m.get("files", {}).get("RAW_D"):
            cands, estimate = auto_candidates(m, live, cur, a.auto_candidates, a.min_saving)
            if not cands:
                raise SetupError(f"no priceable TOU plan beats plan {cur} by ${a.min_saving:g}/yr for this user "
                                 f"(estimate {estimate}): pass --map CUR:CAND")
            extra[str(cur)] = cands
            log(f"  candidates for plan {cur} (estimated annual import cost, this user's RAW): {estimate}")
        else:
            extra[str(cur)] = [tou_target(a, live)]
    for k, v in extra.items():
        miss = [x for x in [int(k), *v] if x not in live]
        if miss:
            raise SetupError(f"--map {k}:{v}: plans {miss} do not exist on pilot {mc.PILOT_ID} (or have no rates)")
        cmap[k] = sorted(set(cmap.get(k, [])) | set(v))
    vals["rate_plans_comparison_map"] = json.dumps(cmap, separators=(",", ":"))
    if a.max_saving_pct is not None:
        # the savings-% cap drops the WHOLE rate comparison result when exceeded (code default 20)
        vals["max_saving_threshold_in_percentage"] = str(a.max_saving_pct)
    if a.computation_method:
        # BILLING_CYCLE_PROJECTED (what the data-duration rule picks for >= 350 days) stores the current plan's
        # DELIVERED stream for no MDS user seen so far (905 on 2026-09-29: RECEIVED only, so the current cost is
        # negative and the savings-% cap drops the whole result). ACTUAL prices both streams.
        vals["computation_method"] = a.computation_method
        vals["check_computation_type_based_on_user_data"] = "false"
    info = {"mapEntries": len(cmap), "extra": extra, "dropped": dropped,
            "estimate": estimate, "computationMethod": vals.get("computation_method")}
    return vals, info


def rate_resources_name(plan: int) -> str | None:
    """rate_resources.plan_name. Read from MySQL: every data-server pod caches rate_resources on its own, so the same
    GET returns the current name, an older name or an empty bean depending on the pod (seen 2026-09-30, 906). Without
    the DB tunnel, the most common non-empty answer of 7 GETs."""
    from create_mds_nve_tou_rate_plan import dbq
    rows = dbq(f"SELECT plan_name FROM rate_resources WHERE utility_id={mc.PILOT_ID} AND plan_number={int(plan)}")
    if rows is not None:
        return (rows[0].get("plan_name") if rows else None) or None
    seen: dict[str, int] = {}
    for _ in range(7):
        n = (payload(api("GET", f"v3.0/rate-info/utilities/{mc.PILOT_ID}/{plan}/rate-resources",
                         allow=(404, 500))) or {}).get("planName")
        if n:
            seen[n] = seen.get(n, 0) + 1
    return max(seen, key=seen.get) if seen else None


def ensure_rate_resources(run: Run, live: dict[int, dict], plans: list[int]) -> None:
    """TouRatePromotionDataProvider reads rate_resources.planName for the best plan (keyed by pilot + plan): a plan
    without a row NPEs there and the section is silently dropped. Register the eUtilityPlans name where it is missing
    (POST rate-resources is an upsert of that one plan's row)."""
    for n in plans:
        if rate_resources_name(n):
            continue
        name = live.get(n, {}).get("planName")
        if not name:
            continue
        body = {"planName": name, "description": live[n].get("planDescription") or name}
        r = api("POST", f"v3.0/rate-info/utilities/{mc.PILOT_ID}/{n}/rate-resources", body, allow=(400, 404, 500))
        run.mutate("rate_resources", plan=n, body=body, status=status_of(r))
        log(f"  rate-resources for plan {n} {name}: HTTP {status_of(r)}", "INFO" if status_of(r) == 200 else "WARN")


def write_config(run: Run, a: argparse.Namespace, vals: dict[str, str]) -> None:
    if a.scope == "pilot":
        cur = mc.read_config("pilot", mc.PILOT_ID, CT)
        bk = HERE.parent / "backups" / f"pilot{mc.PILOT_ID}_{CT}_before_rc_{datetime.now():%Y%m%d_%H%M%S}.json"
        bk.parent.mkdir(exist_ok=True)
        bk.write_text(json.dumps(cur, indent=1))
        log(f"backup pilot {CT} -> {bk}")
        mc.post_config(run, mc.PILOT_ID, CT, vals, "PILOT")
        got = mc.read_config("pilot", mc.PILOT_ID, CT)
    else:
        mc.post_config(run, a.uuid, CT, vals, "USER")
        got = mc.read_config("user", a.uuid, CT)
    bad = [k for k, v in vals.items() if mc.cfg(got, k) != v]
    for k in vals:
        log(f"  readback {CT}.{k} ({got.get(k, {}).get('configSource')}) "
            f"{'ok' if k not in bad else 'MISMATCH ' + mc.cfg(got, k)[:80]}", "WARN" if k in bad else "INFO")
    if bad:
        raise SetupError(f"{CT} readback mismatch for {bad}")


def configure(run: Run, a: argparse.Namespace, live: dict[int, dict]) -> dict:
    step(f"CONFIG {CT} -> {a.scope.upper()} " + (a.uuid or str(mc.PILOT_ID)))
    vals, info = build_config(a, live, run.m)
    for d in info["dropped"]:
        log(f"  map: dropped {d}", "WARN")
    log(f"  map: {info['mapEntries']} entries (+ {info['extra']})")
    (run.dir("config") / f"{CT}_{a.scope}.json").write_text(json.dumps({"values": vals, "info": info}, indent=1))
    if not a.dry_run:
        write_config(run, a, vals)
        cands = sorted({int(x) for v in info["extra"].values() for x in v} | {int(k) for k in info["extra"]})
        ensure_rate_resources(run, live, cands)
    run.m["rateComparisonConfig"] = {"scope": a.scope, "at": time.time(), **info}
    run.save()
    return vals


# ---------------------------------------------------------------------------
# RC API
# ---------------------------------------------------------------------------
def rc_get(uuid: str) -> dict | None:
    r = api("GET", f"v3.0/users/{uuid}/homes/{mc.HID}/rate-comparison?measurement-type=ELECTRIC", allow=(400, 404, 500))
    return payload(r) if status_of(r) == 200 else None


def compute(run: Run, a: argparse.Namespace) -> dict | None:
    uuid = run.m["uuid"]
    step(f"RC API: candidate rate computation for {uuid} (takes ~5 min)")
    mc.config_cache_settle(run, a.settle_config)
    before = rc_get(uuid)
    t0 = time.time()
    path = f"v3.0/users/{uuid}/homes/{mc.HID}/rate-comparison/candidate-rates?measurement-type=ELECTRIC"
    comp = None
    for attempt in range(1, a.rc_retries + 1):
        try:
            r = api("POST", path, allow=(400, 404, 500, 502, 503, 504), timeout=600)
        except SetupError as exc:
            if "timed out" not in str(exc).lower():
                raise
            log(f"  POST candidate-rates did not answer in time ({exc}); the computation keeps running server side", "WARN")
            break
        st = status_of(r)
        run.evidence(f"rc_post_{attempt}", r)
        comp = payload(r) if st == 200 else None
        log(f"  POST candidate-rates (attempt {attempt}/{a.rc_retries}) -> HTTP {st} in {int(time.time() - t0)}s"
            + (f": plans {sorted(comp)}" if isinstance(comp, dict) else f" {json.dumps(r)[:200]}"),
            "INFO" if st == 200 else "WARN")
        if st == 200:
            if a.rc_settle > 0:
                # the RC computation keeps writing Cassandra rows after the POST answers: give it the full window
                log(f"  waiting {a.rc_settle}s for the rate comparison to finish storing before reading it")
                time.sleep(a.rc_settle)
            break
        # code 2211 "Error while running rate comparison" is an exception caught in RateComparisonEndpoint; seen once
        # right after the user-scope config write (2026-09-30, c72b349b) and gone on the next call: retry
        if attempt == a.rc_retries:
            if not a.keep_polling:
                raise SetupError(f"RC API POST HTTP {st} after {attempt} attempts: {json.dumps(r)[:300]}")
            break
        log(f"  retrying in {a.rc_retry_wait}s (transient data-server error)", "WARN")
        time.sleep(a.rc_retry_wait)
    # the GET can answer with a partial result (current plan only, or a stale earlier computation) while the RC
    # computation is still storing: keep reading until every plan the POST priced is present and priced
    want = {int(k) for k in comp} if isinstance(comp, dict) else set()

    def complete(x: dict | None) -> bool:
        if not x:
            return False
        got = {int(c["planNumber"]): plan_cost(c) for c in x.get("comparedRates") or []}
        return want <= {n for n, v in got.items() if v} if want else bool(got)

    deadline = max(t0 + a.rc_wait, time.time() + a.rc_complete_wait)
    res = rc_get(uuid)
    while not complete(res) and time.time() < deadline:
        log(f"  stored RC result not complete yet (want plans {sorted(want)}; {int(deadline - time.time())}s left)")
        time.sleep(a.poll)
        res = rc_get(uuid) or res
    if res:
        run.evidence("rc_get", res)
    run.m["rateComparison"] = {"computedAt": time.time(), "had_before": bool(before), "result": summarize(res)}
    run.save()
    return res


def summarize(res: dict | None) -> dict:
    if not res:
        return {}
    return {"computationType": res.get("computationType"), "start": res.get("comparisonDurationStart"),
            "end": res.get("comparisonDurationEnd"), "billingCycles": res.get("billingCycles"),
            "candidates": [{"planNumber": c.get("planNumber"), "current": c.get("currentRate"), "cost": plan_cost(c),
                            "billEnd": c.get("computedDurationBillEnd")} for c in res.get("comparedRates") or []]}


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------
def plan_cost(c: dict) -> float | None:
    """RateComparisonManager.getCostForComparison: the load-shifted total when non-zero, else the total."""
    def total(b: Any) -> float | None:
        if not isinstance(b, dict):
            return None
        if isinstance(b.get("totalCost"), (int, float)):
            return float(b["totalCost"])
        parts = [b.get("consumptionBasedCharge") or 0] + [sum((b.get(k) or {}).values()) for k in
                                                          ("dailyFixedCharges", "monthlyFixedCharges", "demandBasedCharges")]
        return float(sum(parts))
    ls = total(c.get("loadShiftedCostBreakdown"))
    return ls if ls else (float(c["totalCost"]) if isinstance(c.get("totalCost"), (int, float)) else total(c.get("costBreakdown")))


def check(run: Run, a: argparse.Namespace, live: dict[int, dict], res: dict | None) -> list[tuple[str, str, str, str]]:
    uuid = run.m["uuid"]
    step(f"CHECK rate comparison result against the TOU Rate Promotion gates for {uuid}")
    out: list[tuple[str, str, str, str]] = []

    def add(cid: str, name: str, st: str, detail: str) -> None:
        out.append((cid, name, st, detail))
        log(f"  {cid} {st:4} {name}: {detail}", "ERROR" if st == "FAIL" else "INFO")

    rc = mc.read_config("user", uuid, CT)
    promo = mc.read_config("user", uuid, PROMO_CT)
    cur = user_plan(uuid)
    cmap = json.loads(mc.cfg(rc, "rate_plans_comparison_map", "{}") or "{}")
    add("RC01", "current plan in rate_plans_comparison_map", "PASS" if str(cur) in cmap else "FAIL",
        f"plan {cur} -> {cmap.get(str(cur))}")
    add("RC02", "RC result stored", "PASS" if res else "FAIL",
        f"computationType {res.get('computationType') if res else None}" if res else
        f"GET rate-comparison has nothing after {a.rc_wait}s")
    if not res:
        return out
    cands = res.get("comparedRates") or []
    priced = {int(c["planNumber"]): plan_cost(c) for c in cands if c.get("planNumber") is not None}
    current = next((int(c["planNumber"]) for c in cands if c.get("currentRate")), cur)
    add("RC03", "current plan + candidates priced", "PASS" if current in priced and len(priced) > 1 else "FAIL",
        f"{ {k: (round(v, 2) if v is not None else None) for k, v in priced.items()} } (current {current})")
    valid = {k: v for k, v in priced.items() if v is not None}
    if len(valid) < 2 or current not in valid:
        return out
    # TouRatePromotionDataProvider: best = cheapest over ALL compared rates (current included)
    best = min(valid, key=lambda k: valid[k])
    saving = round(valid[current]) - round(valid[best])
    add("RC04", "best plan is TOU", "PASS" if is_tou(live.get(best)) else "FAIL",
        f"best plan {best} {live.get(best, {}).get('planName')} ${valid[best]:.2f} vs current {current} ${valid[current]:.2f}"
        + ("" if best != current else " (user is already on the cheapest plan)"))
    min_save = float(mc.cfg(promo, "minimum_saving_amount", "5") or 5)
    add("RC05", "savings >= minimum_saving_amount", "PASS" if saving >= min_save else "FAIL",
        f"savings ${saving:g} a year, minimum ${min_save:g}")
    pct_cap = mc.cfg(rc, "max_saving_threshold_in_percentage", "20") or "20"
    pct = 100 * saving / round(valid[current]) if round(valid[current]) else 0
    add("RC06", "savings % <= max_saving_threshold_in_percentage", "PASS" if pct <= float(pct_cap) else "FAIL",
        f"{pct:.1f}% (cap {pct_cap}%)")
    bc_end = [c.get("computedDurationBillEnd") for c in cands if c.get("currentRate")]
    add("RC08", "current plan computedDurationBillEnd = last cycle end", "PASS" if bc_end and run.m.get("cycles") and
        abs(int(bc_end[0]) - int(run.m["cycles"][-1]["endTs"])) <= 86400 else "WARN",
        f"computedDurationBillEnd {bc_end} vs MS cycle end {run.m.get('cycles', [{}])[-1].get('endTs')} "
        "(the promotion section is dropped when they differ)")
    cur = current
    rr_name = rate_resources_name(best)
    add("RC07", "rate-resources planName for the best plan", "PASS" if rr_name else "FAIL",
        f"plan {best} planName {rr_name!r} (MySQL; the per-pod API cache can lag)")
    run.m["rateComparisonCheck"] = {"current": cur, "best": best, "saving": round(saving, 2), "pct": round(pct, 1),
                                    "checks": [dict(zip(("id", "name", "status", "detail"), c)) for c in out]}
    run.save()
    return out


# ---------------------------------------------------------------------------
def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--map", action="append", default=[], metavar="CUR:CAND[,CAND]",
                   help="extra rate_plans_comparison_map entry in target plan numbers (default: user's plan -> the TOU plan)")
    p.add_argument("--auto-candidates", type=int, default=3,
                   help="add the N priceable TOU plans estimated cheapest for this user (0 = current plan -> the TOU plan)")
    p.add_argument("--tou-plan-number", type=int,
                   help="TOU candidate plan number (default: the profile's email.rate_plans.tou resolved by name)")
    p.add_argument("--min-saving", type=float, default=10.0, help="auto candidates must beat the current plan by this $/yr")
    p.add_argument("--computation-method", default="BILLING_CYCLE_ACTUAL",
                   help="computation_method (+ check_computation_type_based_on_user_data=false)")
    p.add_argument("--max-saving-pct", type=float,
                   help="max_saving_threshold_in_percentage (default 100; code default 20)")
    p.add_argument("--rc-wait", type=int, default=600, help="seconds to wait for the RC API result (it takes ~5 min)")
    p.add_argument("--poll", type=int, default=30)
    p.add_argument("--keep-polling", action="store_true", help="poll even when the RC POST returns an error")
    p.add_argument("--rc-retries", type=int, default=3, help="RC POST attempts on a transient 400/5xx (code 2211)")
    p.add_argument("--rc-retry-wait", type=int, default=60)
    p.add_argument("--rc-settle", type=int, default=900,
                   help="seconds to wait after a successful RC POST before reading the stored result (default 15 min)")
    p.add_argument("--rc-complete-wait", type=int, default=600,
                   help="seconds to keep re-reading the stored RC result after the settle until every priced plan is in it")
    p.add_argument("--settle-config", type=int, default=90)


def load_run(uuid: str | None, out_root: Path) -> Run:
    if uuid:
        import mds_her as mh
        manifest = mh.find_manifest(uuid, HERE / "results")
        if manifest:
            return Run(manifest.parent, json.loads(manifest.read_text()))
    out = out_root / f"{datetime.now():%Y%m%d_%H%M%S}_{uuid or 'pilot'}"
    out.mkdir(parents=True, exist_ok=True)
    return Run(out, {"uuid": uuid})


def main(argv: list[str] | None = None) -> int:
    argv = mds_target.bootstrap(argv)          # PROFILE_REQUIRED before anything touches a target
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--uuid")
    p.add_argument("--scope", choices=("user", "pilot"), default="user",
                   help="user (default) or pilot; a PILOT write is shared and must be asked for explicitly")
    p.add_argument("--phases", default="all", help=f"comma list of {','.join(PHASES)} or all")
    p.add_argument("--token-file")
    p.add_argument("--dry-run", action="store_true", help="build the config only, no writes")
    add_args(p)
    a = p.parse_args(argv)
    phases = list(PHASES) if a.phases == "all" else [x.strip() for x in a.phases.split(",") if x.strip()]
    if a.scope == "user" and not a.uuid:
        p.error("--scope user (the default) needs --uuid; a pilot-level write needs an explicit --scope pilot")
    if a.scope == "pilot" and not a.uuid and ({"compute", "check"} & set(phases)):
        phases = [x for x in phases if x == "config"]
    if not a.uuid and ({"compute", "check"} & set(phases)):
        phases = [x for x in phases if x == "config"]
    run = load_run(a.uuid, HERE / "results" / "rate_comparison")
    mc._LOG_FH = open(run.dir("logs") / "run.log", "a")
    if a.uuid:
        mc.set_uuid_tag(a.uuid)
    try:
        mc._TOKEN = mc.get_token(a.token_file)
        live = live_plans()
        if "config" in phases:
            configure(run, a, live)
        res = compute(run, a) if "compute" in phases and not a.dry_run else (rc_get(a.uuid) if a.uuid else None)
        checks = check(run, a, live, res) if "check" in phases and a.uuid else []
        (run.dir("reports") / "rate_comparison_report.md").write_text(
            "| id | check | status | detail |\n|---|---|---|---|\n"
            + "".join(f"| {c[0]} | {c[1]} | {c[2]} | {c[3].replace('|', '/')} |\n" for c in checks))
        worst = "FAIL" if any(c[2] == "FAIL" for c in checks) else "PASS"
        log(f"RATE COMPARISON {worst}: " + " ".join(f"{c[0]}={c[2]}" for c in checks) + f"  ({run.out})")
        return 1 if worst == "FAIL" else 0
    except SetupError as exc:
        log(str(exc), "ERROR")
        return 2
    finally:
        if mc._LOG_FH:
            mc._LOG_FH.close()
            mc._LOG_FH = None


if __name__ == "__main__":
    sys.exit(main())
