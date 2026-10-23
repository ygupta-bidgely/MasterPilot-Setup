#!/usr/bin/env python3
"""Set the rate_comparison config at PILOT level on the target pilot (--profile). Nothing else: no user config, no RC
computation, no other pilot. The token is --token-file, else BIDGELY_TOKEN, else the profile's api.token_file.

    python3 mds_user_automation/set_mds_pilot_rate_comparison.py --profile <p> --map CUR:CAND[,CAND] [--map ...]
    python3 mds_user_automation/set_mds_pilot_rate_comparison.py --profile <p> --map-from-profile   # flat -> tou plan
    ... --dry-run                                                                                    # show what would change
    options: --max-saving-pct 100  --computation-method BILLING_CYCLE_ACTUAL
The comparison map is never assumed: either --map (target plan numbers) or --map-from-profile (the profile's
email.rate_plans flat -> tou, plan names resolved to numbers on the target) is required.

The previous pilot values are saved to backups/pilot<pilot>_rate_comparison_before_<time>.json first.
Exit code: 0 = written and read back, 1 = readback mismatch, 2 = setup error.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mds_common as mc
import mds_target  # noqa: E402
from mds_common import SetupError, api, log  # noqa: E402

CT = "rate_comparison"


def profile_map() -> list[str]:
    """--map-from-profile: the profile's email.rate_plans flat -> tou, plan NAMES resolved to numbers on the target."""
    flat, tou = mc.DEFAULT_RATE_PLAN, mc.TOU_RATE_PLAN
    if not (flat and tou):
        raise SetupError("--map-from-profile needs email.rate_plans.flat and .tou in the profile: pass --map CUR:CAND instead")
    plans = mc.payload(api("GET", f"v3.0/rates/configuration/utilityId/{mc.PILOT_ID}", allow=(404,))) or []
    num = {str(p.get("planName")): p.get("planNumber") for p in plans}
    miss = [n for n in (flat, tou) if num.get(n) is None]
    if miss:
        raise SetupError(f"plans {miss} (profile email.rate_plans) do not exist on pilot {mc.PILOT_ID}: pass --map CUR:CAND")
    return [f"{num[flat]}:{num[tou]}"]


def build_values(a: argparse.Namespace) -> dict[str, str]:
    cmap: dict[str, list[int]] = {}
    for item in a.map or []:
        cur, cands = item.split(":", 1)
        cmap[str(int(cur))] = sorted({int(x) for x in cands.split(",") if x.strip()})
    if not cmap:
        raise SetupError("no rate_plans_comparison_map entry: pass --map CUR:CAND[,CAND] or --map-from-profile")
    return {
        "rate_comparison_enabled": "true",
        "computation_method": a.computation_method,
        "rate_comparison_computation_type_duration_map": json.dumps({
            "BILLING_CYCLE_ACTUAL": {"min_data_days_threshold": "345", "max_data_days_threshold": "350"},
            "BILLING_CYCLE_PROJECTED": {"min_data_days_threshold": "350", "max_data_days_threshold": "2147483647"}},
            separators=(",", ":")),
        "duration_in_months_for_rate_comparison": "12",
        "rate_plans_comparison_map": json.dumps(cmap, separators=(",", ":")),
        # ACTUAL prices both the DELIVERED and RECEIVED streams; do not let the data-duration rule switch to PROJECTED
        "check_computation_type_based_on_user_data": "false",
        # the savings-% cap drops the whole result when exceeded (code default 20)
        "max_saving_threshold_in_percentage": str(a.max_saving_pct),
        "target_duration_in_days_for_rate_comparison": "350",
        "raise_event_on_invoice_update": "true",
        "number_of_candidates_for_rate_comparison": "12",
        "use_minimum_365_days_for_rate_comparison": "true",
        "decimal_rounding_place": "0",
    }


def main(argv: list[str] | None = None) -> int:
    argv = mds_target.bootstrap(argv)          # PROFILE_REQUIRED before anything touches a target
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--token-file", help="default: BIDGELY_TOKEN, else the profile's api.token_file")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--map", action="append", metavar="CUR:CAND[,CAND]",
                   help="rate_plans_comparison_map entry in target plan numbers, repeatable (no default)")
    g.add_argument("--map-from-profile", action="store_true",
                   help="map the profile's email.rate_plans.flat plan to its .tou plan (names resolved on the target)")
    p.add_argument("--max-saving-pct", type=int, default=100)
    p.add_argument("--computation-method", default="BILLING_CYCLE_ACTUAL",
                   choices=["BILLING_CYCLE_ACTUAL", "BILLING_CYCLE_PROJECTED"])
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    try:
        mc._TOKEN = mc.get_token(a.token_file)     # None -> BIDGELY_TOKEN, else the profile's api.token_file
        if a.map_from_profile:
            a.map = profile_map()
        vals = build_values(a)
        cur = mc.read_config("pilot", mc.PILOT_ID, CT)
        changes = {k: (mc.cfg(cur, k) or None, v) for k, v in vals.items() if mc.cfg(cur, k) != v}
        log(f"pilot {mc.PILOT_ID} {CT}: {len(changes)} of {len(vals)} keys differ")
        for k, (old, new) in changes.items():
            log(f"  {k}: {old!r} -> {new!r}")
        if a.dry_run:
            return 0
        bk = Path(__file__).resolve().parent.parent / "backups" / \
            f"pilot{mc.PILOT_ID}_{CT}_before_{datetime.now():%Y%m%d_%H%M%S}.json"
        bk.parent.mkdir(exist_ok=True)
        bk.write_text(json.dumps(cur, indent=1))
        log(f"backup -> {bk}")
        kvs = [{"configKey": k, "configVal": v, "configDataType": "TEXT", "configRegex": "(?s).*",
                "configDocumentation": f"set by set_mds_pilot_rate_comparison.py on {date.today()}"}
               for k, v in vals.items()]
        api("POST", f"entities/{mc.PILOT_ID}/configs", {"configType": CT, "configKVs": kvs})
        got = mc.read_config("pilot", mc.PILOT_ID, CT)
        bad = [k for k, v in vals.items() if mc.cfg(got, k) != v]
        for k in vals:
            log(f"  readback {CT}.{k} ({got.get(k, {}).get('configSource')}) "
                f"{'ok' if k not in bad else 'MISMATCH ' + mc.cfg(got, k)[:80]}", "WARN" if k in bad else "INFO")
        if bad:
            log(f"readback mismatch for {bad}", "ERROR")
            return 1
        log("done: services pick pilot config up after their config cache refreshes (about a minute or two)")
        return 0
    except SetupError as exc:
        log(str(exc), "ERROR")
        return 2


if __name__ == "__main__":
    sys.exit(main())
