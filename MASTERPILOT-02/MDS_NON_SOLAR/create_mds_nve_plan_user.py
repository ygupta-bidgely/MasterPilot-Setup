#!/usr/bin/env python3
"""Create a new MDS user on target pilot the target pilot on a copied NVE rate plan.

NON-SOLAR (MDS_NON_SOLAR copy of MDS_AUTOMATION): the user is an MDS non-solar premise -- SOLAR_USER FALSE,
DELIVERED = whole consumption, RECEIVED present with all-zero readings, FEED_IN 0 kWh / $0, no solar user-scope
configs, checks expect solarUser / GLOBAL.SOLAR not true and no SOLAR_ENERGY_FLOW. Plans come from the profile.
Launches this folder's (non-solar) single fuel / TOU scripts.

The plans come from copy_nve_rates_to_mds.py (NVE UAT <source pilot> plan N -> target plan N + 10000, name NVE_<name>).
This wrapper resolves the plan, checks it is usable, then runs the existing end-to-end flow:
    flat / tiered / demand plan  -> create_mds_single_fuel_user.py --rate-plan NVE_<name>
    TOU plan with --tou          -> create_mds_tou_user.py --tou-rate-plan NVE_<name>  (flat base plan, METERENROLL
                                    rate transition onto the NVE plan, TOU Onboarding + TOU Coaching emails)
Any other option is passed through unchanged (--phases, --peak-kw, --transition-cycles, --dry-run, ...).

PREFLIGHT (stops before any file is uploaded)
    P1 plan exists on the target pilot (configuration GET = live MySQL) with rows and data_stream
    P2 /v3.0/rates/.../structure answers 200. Running JVMs cache every target plan per utility and are never
       invalidated: a 404 here means data-server has not loaded the plan, and ingestion (METERENROLL plan-name
       lookup) and aggregation (pricing) are in the same state. Restart data-server, ingestion,
       aggregation and aggregation-priority on <deployment>, or pass --ignore-cache-check.
    P3 (--tou) the plan has TOU rows; rateStructureImageUrl set on them (TOU Onboarding drops without it --
       no API writes it: create_mds_nve_tou_rate_plan.py renders + sets the images for a TOU plan).

RUN
    export BIDGELY_TOKEN=<target token>          # or --token-file
    python3 mds_user_automation/create_mds_nve_plan_user.py --nve-plan 205              # demand plan NVE_Y01
    python3 mds_user_automation/create_mds_nve_plan_user.py --nve-plan 104 --tou        # TOU user + TOU emails
    python3 mds_user_automation/create_mds_nve_plan_user.py --plan-name NVE_E6LS --phases preflight
    python3 mds_user_automation/create_mds_nve_plan_user.py --nve-plan 104 --preflight-only
Exit code: the wrapped script's (0 PASS, 1 a check FAILed, 2 setup error); 2 when preflight fails.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mds_common as mc
import mds_target  # noqa: E402
from mds_common import SetupError, api, log, payload, status_of  # noqa: E402
from create_mds_nve_tou_rate_plan import dbq  # noqa: E402

HERE = Path(__file__).resolve().parent


def resolve(a: argparse.Namespace) -> dict:
    plans = payload(api("GET", f"v3.0/rates/configuration/utilityId/{mc.PILOT_ID}", allow=(404,))) or []
    if a.plan_name:
        plan = next((p for p in plans if p.get("planName") == a.plan_name), None)
    else:
        plan = next((p for p in plans if p.get("planNumber") == a.nve_plan + a.plan_offset), None)
    if not plan:
        want = a.plan_name or f"plan {a.nve_plan + a.plan_offset}"
        raise SetupError(f"P1 target has no {want}: copy it first -- python3 mds_user_automation/copy_nve_rates_to_mds.py "
                         f"--plans {a.nve_plan or '<nve plan>'}")
    return plan


def preflight(a: argparse.Namespace, plan: dict) -> None:
    num, name = plan["planNumber"], plan["planName"]
    rows = [r for c in plan.get("components", []) for r in c.get("rates", [])]
    if not rows:
        raise SetupError(f"P1 target plan {num} {name} has no rate rows (interrupted copy: re-run the copy with --repair)")
    log(f"P1 PASS target plan {num} {name}: {len(rows)} rows, charge types "
        f"{sorted({r['chargeType'] for r in rows})}")
    st = api("GET", f"v3.0/rates/utilities/{mc.PILOT_ID}/plans/{num}/structure", allow=(404, 500))
    if status_of(st) == 200 and payload(st):
        log(f"P2 PASS /structure serves plan {num} ({len(payload(st))} rows): the rate cache has the plan")
    elif a.ignore_cache_check:
        log(f"P2 WARN /structure HTTP {status_of(st)} for plan {num}: JVM rate caches predate the plan "
            "(--ignore-cache-check: continuing; ingestion/aggregation may not resolve or price it)", "WARN")
    else:
        raise SetupError(f"P2 /structure HTTP {status_of(st)} for plan {num} {name}: the running JVMs cache the target's rate "
                         "plans and have not loaded this one, so METERENROLL cannot resolve it and aggregation prices at "
                         "the default rate. Restart data-server, ingestion, aggregation and aggregation-priority on "
                         "the target deployment, then re-run (or --ignore-cache-check)")
    tou = sorted({r["touName"] for r in rows if r.get("touName")})
    if a.tou:
        if not tou:
            raise SetupError(f"P3 --tou but target plan {num} {name} has no TOU rows (charge types "
                             f"{sorted({r['chargeType'] for r in rows})})")
        db = dbq(f"SELECT erateId, touName, rateStructureImageUrl FROM eRates WHERE utilityId={mc.PILOT_ID} "
                 f"AND planNumber={num} AND touName IS NOT NULL") or []
        no_img = [r for r in db if r.get("rateStructureImageUrl") in (None, "", "NULL")]
        if db and no_img:
            log(f"P3 WARN plan {num} {name}: {len(no_img)} TOU rows without rateStructureImageUrl -- TOU Onboarding "
                "drops (TOU_RATE_STRUCTURE_NOT_AVAILABLE); TOU Coaching is unaffected", "WARN")
        else:
            log(f"P3 {'PASS' if db else 'WARN (DB not reachable, image not checked)'} TOU periods {tou}")
    elif tou:
        log(f"   plan {num} {name} is TOU {tou}: add --tou for the TOU transition + TOU emails")


def main(argv: list[str] | None = None) -> int:
    argv = mds_target.bootstrap(argv)          # PROFILE_REQUIRED before anything touches a target
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--nve-plan", type=int, help="source plan number (target plan = this + --plan-offset)")
    g.add_argument("--plan-name", help="target plan name, e.g. NVE_E6LS")
    p.add_argument("--plan-offset", type=int, help="target plan = source plan + offset (required with --nve-plan; no default)")
    p.add_argument("--tou", action="store_true", help="TOU user: rate transition onto the plan + TOU Onboarding/Coaching")
    p.add_argument("--ignore-cache-check", action="store_true")
    p.add_argument("--preflight-only", action="store_true")
    p.add_argument("--token-file")
    a, rest = p.parse_known_args(argv)
    if a.nve_plan is not None and a.plan_offset is None:
        p.error("--plan-offset is required with --nve-plan (target plan = source plan + offset; no default)")
    try:
        mc._TOKEN = mc.get_token(a.token_file)
        plan = resolve(a)
        preflight(a, plan)
    except SetupError as exc:
        log(str(exc), "ERROR")
        return 2
    if a.preflight_only:
        return 0
    tok = ["--token-file", a.token_file] if a.token_file else []
    if a.tou:
        cmd = [sys.executable, str(HERE / "create_mds_tou_user.py"), "--tou-rate-plan", plan["planName"], *tok, *rest]
    else:
        cmd = [sys.executable, str(HERE / "create_mds_single_fuel_user.py"), "--rate-plan", plan["planName"], *tok, *rest]
    log(f"RUN {' '.join(Path(c).name if c.endswith('.py') else c for c in cmd[1:])}")
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
