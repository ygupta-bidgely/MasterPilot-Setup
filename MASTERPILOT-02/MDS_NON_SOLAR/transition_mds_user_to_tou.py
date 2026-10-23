#!/usr/bin/env python3
"""Controlled TOU Rate Onboarding recovery for an EXISTING MDS NON-SOLAR user on the target pilot (MDS_NON_SOLAR copy:
the preflight requires the ELECTRIC home and its D/R meters to be NOT solar): a fresh
ingestion plan switch, so the system itself publishes TOU_ONBOARDING (no NotificationsProcessorEvent, no replay).

NON-SOLAR (MDS_NON_SOLAR copy of MDS_AUTOMATION): the user is an MDS non-solar premise -- SOLAR_USER FALSE,
DELIVERED = whole consumption, RECEIVED present with all-zero readings, FEED_IN 0 kWh / $0, no solar user-scope
configs, checks expect solarUser / GLOBAL.SOLAR not true and no SOLAR_ENERGY_FLOW. Plans come from the profile.
Preflight requires the ELECTRIC home and its D/R meters to be NOT solar.

    1 reads the user's run manifest (create_mds_*_user.py) and takes the ELECTRIC row of its USERENROLL file;
      GAS rows are never sent, so the gas meter, gas plan and gas schedule stay as they are
    2 writes USERENROLL_..._T<ts>.csv: RATE_PLAN_ID = --tou-rate-plan, RATE_PLAN_EFFECTIVE_DATE = start of the last
      --transition-cycles completed cycles (or --effective-date)
    3 USER-scope onboarding configs (mds_tou.configure, onboarding flow: enable_tou_chart_for_solar=true, the
      TOU_ONBOARDING sublevel itemization config, subscription, sections, subject)
    4 opens the the target pilot handle_meter_enrollment=false window for that one file (backed up, always restored), uploads it,
      waits for the ELECTRIC schedule to switch, and checks the GAS schedule did not change
    5 waits for the system-generated TOU_ONBOARDING email and validates it (O01-O10); a missing email gets the
      evidence chain (publication -> processing -> drop -> storage -> provider acceptance) for this event only

Use only after the eligibility fix (SectionConditionValidator.isEligible) is deployed to the notifications services:
on the current build a solar home is dropped TOU_RATE_STRUCTURE_NOT_AVAILABLE.

RUN
    export BOPS_PW_FILE=$TMPDIR/bops_pw_$USER
    python3 mds_user_automation/transition_mds_user_to_tou.py --manifest mds_user_automation/results/dual_fuel/run_<id>/manifest.json --dry-run
    python3 mds_user_automation/transition_mds_user_to_tou.py --manifest mds_user_automation/results/dual_fuel/run_<id>/manifest.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mds_common as mc
import mds_target  # noqa: E402
import mds_tou  # noqa: E402
from mds_common import Run, SetupError, log  # noqa: E402

RATE_PLAN_ID, RATE_PLAN_EFFECTIVE_DATE, SERVICE_TYPE = 31, 32, 28


def electric_transition_row(userenroll_text: str, plan: str, effective: date) -> str:
    rows = [r for r in userenroll_text.strip().splitlines() if r.strip()]
    elec = [r.split("|") for r in rows if len(r.split("|")) > SERVICE_TYPE and r.split("|")[SERVICE_TYPE] == "ELECTRIC"]
    if len(elec) != 1:
        raise SetupError(f"expected exactly one ELECTRIC USERENROLL row, found {len(elec)}")
    row = elec[0]
    row[RATE_PLAN_ID], row[RATE_PLAN_EFFECTIVE_DATE] = plan, effective.isoformat()
    return "|".join(row)


def preflight_failures(f: dict) -> list[str]:
    """Pure check over the live facts gathered by gather_facts(); every failure blocks the transition."""
    bad = []
    if f.get("manualTrigger"):
        bad.append("--manual-trigger is not allowed: the recovery must be system-generated from ingestion")
    if f.get("pilotId") != mc.PILOT_ID or f.get("manifestPilotId") not in (None, mc.PILOT_ID):
        bad.append(f"pilot {f.get('pilotId')} / manifest {f.get('manifestPilotId')} is not {mc.PILOT_ID}")
    if not f.get("uuidMatches"):
        bad.append(f"externalUserId resolves to {f.get('resolvedUuid')}, not the manifest uuid {f.get('uuid')}")
    if set(f.get("fuels") or []) != {"ELECTRIC", "GAS"}:
        bad.append(f"fuels {f.get('fuels')}: a dual-fuel ELECTRIC + GAS account is required")
    for ref, mt in (f.get("meters") or {}).items():
        if not mt.get("present"):
            bad.append(f"{ref} meter {mt.get('token')} not found on the user")
        elif not mt.get("active"):
            bad.append(f"{ref} meter {mt.get('token')} is not active (contractEnd {mt.get('contractEnd')})")
    for ref in ("ELECTRIC:DELIVERED", "ELECTRIC:RECEIVED", "GAS:DELIVERED"):
        if ref not in (f.get("meters") or {}):
            bad.append(f"no {ref} meter in the manifest identity")
    if str(f.get("homeSolarUser")).lower() == "true":
        bad.append(f"home.solarUser={f.get('homeSolarUser')}: a non-solar ELECTRIC home is required")
    for ref in ("ELECTRIC:DELIVERED", "ELECTRIC:RECEIVED"):
        if str((f.get("meters") or {}).get(ref, {}).get("solar")).lower() == "true":
            bad.append(f"{ref} meter is flagged solar on a non-solar account")
    if str((f.get("meters") or {}).get("GAS:DELIVERED", {}).get("solar")).lower() == "true":
        bad.append("GAS meter is flagged solar")
    if not f.get("electricSchedule"):
        bad.append("no ELECTRIC rate schedule")
    elif f.get("electricCurrentPlanIsTou") is not False:
        bad.append(f"current ELECTRIC plan {f.get('electricCurrentPlan')} is TOU (or unknown): not a non-TOU -> TOU switch")
    if f.get("targetPlanNumber") in [p for _, _, p in f.get("electricSchedule") or []]:
        bad.append(f"target plan {f.get('targetPlanNumber')} is already in the ELECTRIC schedule: transition already done")
    if not f.get("gasSchedule"):
        bad.append("no GAS rate schedule to keep as the unchanged baseline")
    if not f.get("targetPlanNumber"):
        bad.append(f"target plan {f.get('targetPlan')} not found on pilot {mc.PILOT_ID}")
    elif not {"On-Peak", "Off-Peak"} <= set(f.get("targetTouPeriods") or []):
        bad.append(f"target plan {f.get('targetPlan')} has no On-Peak/Off-Peak periods: {f.get('targetTouPeriods')}")
    eff, today = f.get("effectiveTs"), f.get("todayTs")
    if eff is None or today is None or eff > today:
        bad.append(f"effective date {f.get('effectiveDate')} is in the future: the ingester only publishes "
                   "TOU_ONBOARDING for an effective date at or before now")
    if eff is not None and f.get("cycleStarts") and eff not in f["cycleStarts"]:
        bad.append(f"effective date {f.get('effectiveDate')} is not a billing cycle start")
    if eff is not None and f.get("electricLastScheduleStart") is not None and eff <= f["electricLastScheduleStart"]:
        bad.append(f"effective date {f.get('effectiveDate')} is not after the current ELECTRIC schedule start")
    if (f.get("onboardingSentCount") or 0) > 0:
        bad.append(f"TOU_ONBOARDING already sent for ELECTRIC (sentCount {f.get('onboardingSentCount')})")
    if f.get("recoveryAlreadyApplied"):
        bad.append(f"a recovery transition was already applied for this run: {f.get('recoveryAlreadyApplied')}")
    if f.get("pilotFlag") != "true":
        bad.append(f"pilot handle_meter_enrollment reads {f.get('pilotFlag')!r}, expected 'true' (another window open?)")
    if f.get("unresolvedWindow"):
        bad.append("this run has an unresolved pilot-flag window: repair it with --restore-pilot-flag first")
    return bad


def gather_facts(run: Run, plan: str, eff: date, manual_trigger: bool) -> dict:
    """Live, read-only facts for preflight_failures()."""
    m, uuid = run.m, run.m["uuid"]
    tz = mc.ZoneInfo(m["timezone"])
    user = mc.payload(mc.api("GET", f"meta/users/{uuid}", allow=(404,))) or {}
    resolved = mc.find_uuid(m["identity"]["externalUserId"])
    home = mds_tou.home(uuid)
    am = mc.all_meters(uuid)
    now = time.time()
    meters = {}
    for ref, token in (m["identity"].get("tokens") or {}).items():
        v = am.get(token)
        mt = (v or {}).get("meter") or {}
        end = int(mt.get("contractEnd") or 0)
        meters[ref] = {"token": token, "present": v is not None, "active": v is not None and (end == 0 or end > now),
                       "contractEnd": end, "solar": mt.get("solar")}
    sched = schedule_by_fuel(uuid)
    elec = sched.get("ELECTRIC") or []
    cur = elec[-1][2] if elec else None
    num = None
    try:
        num, _ = mc.resolve_plan(plan)
    except Exception:  # noqa: BLE001 -- reported as "target plan not found"
        num = None

    def periods(plan_number):
        if plan_number is None:
            return None
        cfgs = mc.payload(mc.api("GET", f"v3.0/rates/configuration/utilityId/{mc.PILOT_ID}?planNumber={plan_number}",
                                 allow=(404,))) or []
        return sorted({r.get("touName") for p in cfgs if isinstance(p, dict) for c in p.get("components", [])
                       for r in c.get("rates", []) if r.get("touName")})
    cur_periods = periods(cur)
    st = mc.payload(mc.api("GET", mds_tou.state_path(uuid, mds_tou.ONB), allow=(404, 500))) or {}
    w = m.get("pilotFlagWindow") or {}
    return {
        "manualTrigger": manual_trigger, "uuid": uuid, "manifestPilotId": m.get("pilotId"),
        "pilotId": user.get("pilotId") if user.get("pilotId") is not None else (user.get("user") or {}).get("pilotId"),
        "resolvedUuid": resolved, "uuidMatches": str(resolved) == uuid, "fuels": m.get("fuels"), "meters": meters,
        "homeSolarUser": home.get("solarUser"), "electricSchedule": [(a, b, str(c)) for a, b, c in elec],
        "electricCurrentPlan": cur, "electricCurrentPlanIsTou": None if cur_periods is None else bool(cur_periods),
        "electricLastScheduleStart": elec[-1][0] if elec else None, "gasSchedule": sched.get("GAS") or [],
        "targetPlan": plan, "targetPlanNumber": str(num) if num else None, "targetTouPeriods": periods(num),
        "effectiveDate": eff.isoformat(), "effectiveTs": mc.midnight(eff, tz),
        "todayTs": mc.midnight(datetime.now(tz).date(), tz),
        "cycleStarts": [c["startTs"] for c in m.get("cycles") or []],
        "onboardingSentCount": int((st if isinstance(st, dict) else {}).get("sentCount") or 0),
        "recoveryAlreadyApplied": (m.get("transitionApplied") if (m.get("transition") or {}).get("recovery") else None),
        "pilotFlag": mc.cfg(mc.read_config("pilot", mc.PILOT_ID, "launchpad_ingestion_configs"), mc.FLAG_KEY),
        "unresolvedWindow": bool(w) and not w.get("closedAt"),
    }


def schedule_by_fuel(uuid: str) -> dict[str, list[tuple]]:
    h = mds_tou.home(uuid)
    out: dict[str, list[tuple]] = {}
    for e in mc.schedule_entries(h):
        out.setdefault(e.get("measurementType"), []).append(
            (e.get("startTime"), e.get("endTime"), (e.get("metaData") or {}).get("planNumber")))
    return out


def main(argv: list[str] | None = None) -> int:
    argv = mds_target.bootstrap(argv)          # PROFILE_REQUIRED before anything touches a target
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--manifest", required=True)
    p.add_argument("--effective-date", help="YYYY-MM-DD (default: start of the last --transition-cycles cycles)")
    p.add_argument("--dry-run", action="store_true", help="write the transition file and print the plan; no writes")
    p.add_argument("--flag-settle", type=int, default=120)
    p.add_argument("--flag-transition-wait", type=int, default=240)
    p.add_argument("--transition-wait", type=int, default=600)
    p.add_argument("--adopt-unrecorded-files", action="store_true",
                   help="OPERATOR ONLY: the manifest predates enrollment integrity records; record the stored files at their "
                        "CURRENT bytes (after checking them) instead of refusing")
    own, rest = p.parse_known_args(argv)
    if "--manual-trigger" in rest:
        print("--manual-trigger is not allowed: this recovery is ingestion-only (no NotificationsProcessorEvent)",
              file=sys.stderr)
        return 2
    a, _ = mds_tou.parse(__doc__, "onboarding", rest, "onboarding")
    mf = Path(own.manifest)
    run = Run(mf.parent, json.loads(mf.read_text()))
    m, uuid = run.m, run.m["uuid"]
    mc.set_uuid_tag(uuid)
    mc._LOG_FH = open(run.dir("logs") / "run.log", "a")
    cyc = m["cycles"]
    eff = date.fromisoformat(own.effective_date) if own.effective_date else \
        date.fromisoformat(cyc[-max(1, min(a.transition_cycles, len(cyc)))]["start"])
    try:
        # the transition row is rebuilt from the ORIGINAL USERENROLL only: every recorded original must match the
        # manifest sha256 before anything is built, read from the target or written (missing/changed -> refused)
        mc.verify_recorded_files(run, adopt_unrecorded=own.adopt_unrecorded_files)
        original = mc.verified_bytes(run, Path(m["files"]["USERENROLL"]))
        row = electric_transition_row(original.decode(), a.tou_rate_plan, eff)
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        base = Path(m["files"]["USERENROLL"]).name.rsplit(".", 1)[0]
        f = run.dir("input_files") / f"{base}_T{stamp}.csv"
        if f.exists():
            raise SetupError(f"{f.name} already exists: a produced file is never rewritten")
        f.write_text(row + "\n")
        log(f"TRANSITION FILE {f.name}: ELECTRIC row only, plan {a.tou_rate_plan}, effective {eff} (fuels {m.get('fuels')})")
        mc._TOKEN = mc.get_token(a.token_file)
        facts = gather_facts(run, a.tou_rate_plan, eff, False)
        (run.dir("reports") / f"recovery_preflight_{stamp}.json").write_text(json.dumps(facts, indent=2, default=str))
        bad = preflight_failures(facts)
        for x in bad:
            log(f"  PREFLIGHT FAIL {x}", "ERROR")
        if bad:
            raise SetupError(f"preflight: {len(bad)} check(s) failed -- nothing was written")
        log(f"PREFLIGHT PASS: {uuid} pilot {facts['pilotId']}, fuels {facts['fuels']}, non-solar ELECTRIC D+R, ELECTRIC "
            f"{facts['electricCurrentPlan']} (non-TOU) -> {facts['targetPlanNumber']} {facts['targetTouPeriods']}, "
            f"GAS baseline {facts['gasSchedule']}")
        if own.dry_run:
            log("dry run: preflight only, nothing uploaded, no config written")
            return 0
        num = int(facts["targetPlanNumber"])
        tz = mc.ZoneInfo(m["timezone"])
        before = schedule_by_fuel(uuid)
        from_num = (before.get("ELECTRIC") or [(None, None, None)])[-1][2]
        m["transition"] = {"plan": a.tou_rate_plan, "planNumber": num, "fromPlan": m.get("ratePlan"),
                           "fromPlanNumber": int(from_num) if from_num else None, "from": eff.isoformat(),
                           "fromTs": mc.midnight(eff, tz), "cycles": sum(1 for c in cyc if c["start"] >= eff.isoformat()),
                           "recovery": True}
        m["files"]["USERENROLL_T"] = str(f)
        mc.record_file(run, f, "USERENROLL_T")       # recorded once; the upload re-verifies these exact bytes
        m.pop("transitionApplied", None)
        m["systemSince"] = int(time.time()) - 60
        run.save()
        mds_tou.configure(run, a)
        mds_tou.settle(run, a)
        ns = argparse.Namespace(transition_via="userenroll-flag", transition_wait=own.transition_wait, poll=a.poll,
                                flag_settle=own.flag_settle, flag_transition_wait=own.flag_transition_wait,
                                aws_profile=a.aws_profile)
        s3 = mc.aws(a.aws_profile).client("s3")
        # ingestion-only: an ingestion timeout fails with NO home API schedule write (GAS cannot be touched)
        bucket = (m.get("preflight") or {}).get("bucket") or mc.DEFAULT_BUCKET     # the profile's launchpad bucket, never a literal
        if not bucket:
            raise SystemExit("NEEDS_PROFILE: no launchpad bucket (manifest preflight or the profile's email.s3.launchpad_bucket)")
        mc.apply_transition(run, ns, s3, bucket, home_api_fallback=False)
        after = schedule_by_fuel(uuid)
        gas_same = before.get("GAS") == after.get("GAS")
        log(f"schedules before {before} after {after}: GAS {'unchanged' if gas_same else 'CHANGED'}",
            "INFO" if gas_same else "ERROR")
        m["recoveryGasScheduleUnchanged"] = gas_same
        run.save()
        checks = mds_tou.onboarding(run, a)
        return 1 if (not gas_same or any(c.status == "FAIL" for c in checks)) else 0
    except SetupError as exc:
        log(str(exc), "ERROR")
        return 2
    finally:
        try:
            mc.restore_pilot_flag(run)
        except SetupError as exc:            # unresolved restore: the run fails whatever happened before
            log(f"RESTORE FAILED: {exc}", "ERROR")
            return 2                         # noqa: B012 -- deliberate: an open pilot window overrides any result
        finally:
            if mc._LOG_FH:
                mc._LOG_FH.close()


if __name__ == "__main__":
    sys.exit(main())
