#!/usr/bin/env python3
"""Copy every NVE rate plan from NVE UAT (upper environment) to target pilot the target pilot MDS (lower
environment) through the rates APIs. Creates ONLY the plans that exist on NVE UAT and do not exist on the target pilot yet.

SOURCE  NVE UAT = deployment <source deployment>-uat (https://<source API host>.bidgely.com), pilot <source pilot> (NVE RESI).
        GET /v3.0/rates/configuration/utilityId/<source pilot>            plan list (number, name, description, residential)
        GET /v3.0/rates/utilities/<source pilot>/plans/<n>/structure       the raw eRates rows of each plan. This is the
            faithful read: the configuration GET nulls per-rate groupName and truncates validity to dates.
        The snapshot is saved to fixtures/nve_uat_<source pilot>_all_plans.json. Without an NVE token the script reuses it.

TARGET  NVE plan N -> target plan N + --plan-offset (required; the historical port used 10000), name --name-prefix +
        <NVE name> (required; the historical port used NVE_). Rows are copied verbatim
        (rates, TOU windows, tiers, demand, holiday rows, groupName, chargeSubType, validity), except out-of-range
        months/hours, which both rates APIs reject and are clamped (NVE 104's fixed row month 0-12 -> 1-12, same
        coverage; listed in the report). The only MDS addition is
        attributes.data_stream=DELIVERED on rows without it: MDS pricing skips rows without a data_stream
        (AbstractRateProvider.filterRatesByAttributes). Options: --holiday-twins, --feed-in-rate.

SKIP    A plan is NOT created when the target pilot already has it:
          * copied   the target pilot has the target plan number with the target name (an earlier run of this script)
          * ported   an target plan description names it ("NVE UAT <source pilot> plan <N> ...", e.g. 907 = NVE 198)
          * conflict the target plan number exists with another name (never overwritten; pick --plan-offset)
          * empty    no rate rows on NVE UAT
          * not-supported  the plan prices a data stream the pilot's supported_streams does not list; never copied.
          * not-mds  the plan would not price BOTH MDS streams (DELIVERED + RECEIVED). Every the target pilot MDS user is D&R
                     solar: the RC API skips such a candidate (AbstractBillingCycleCandidateRateValuesComputer:275)
                     and a user enrolled on it gets no export pricing. NVE plans carry DELIVERED rows only, so pass
                     --feed-in-rate <$/kWh> to add a flat RECEIVED band (as 907 has) to create MDS-proper copies.
          * mds-incompatible  the plan breaks the MDS band rule (one rateBandId per chargeType+data_stream and one
                     chargeType per rateBandId+data_stream, UtilityRatesManager); both rates APIs reject it. Checked
                     BEFORE any write, so no half-created plan is left behind.
        Identical rates under another name do NOT count (METERENROLL resolves plans by name; NVE itself has
        distinct plans with identical rates, e.g. 205/208/209/212); the report notes them.
        A copied plan whose row count differs from the source (an interrupted run) is reported as PARTIAL. --repair
        re-creates those.

HOW     Per plan, all API (the deployed <deployment> rates DTOs predate PE-14194, so one call cannot carry
        attributes):
        1) POST /v3.0/rates/configuration/utilityId/the target pilot     the eUtilityPlans row (name METERENROLL resolves).
           Its validator rejects some NVE plans ("Overlapping windows": NVE holiday rows overlap and the engine
           resolves them by lower bound); then the plan row is sent with a single rate.
        2) DELETE /v3.0/rates/utilities/the target pilot/plans/<plan>     then POST /v3.0/rates with every row (attributes,
           groupName). addRates is a plain INSERT: the DELETE keeps a rerun from duplicating rows.
        3) POST /v3.0/rate-info/utilities/the target pilot/<plan>/rate-resources   planName (TOU emails NPE without it)
        rateStructureImageUrl (only needed for TOU Onboarding emails) has no API: use
        create_mds_nve_tou_rate_plan.py for a plan that must send TOU emails.

VERIFY  Re-reads the the target pilot configuration GET (live MySQL) and compares every created plan to its NVE source row by
        row (groupName excluded: that GET drops it), checks rate-resources, and, when the config-fail-testcase-debugger dbq tunnel
        exists, counts rows, groupName and data_stream per plan in eRates. Running JVMs cache rate plans per utility:
        /structure and pricing see new plans only after a restart of data-server/aggregation.

CREDS   Never hard-coded. <deployment>: --token-file / BIDGELY_TOKEN, or --client-file to mint one. Source: the
        --source-profile's api.token_file, --nve-token-file, or --nve-client-file (minted at the source profile's
        api.url). There is no source token variable. Client id/secret pairs come from the Jenkins
        self-service portal (https://jenkins.bidgely.com:4567, VPN, "Get Client"); a client file is mode-600 JSON
        {"client_id": "...", "client_secret": "..."}. Tokens and secrets are redacted from every log line.

RUN     The target comes from --profile; the SOURCE (host, pilot, token) comes only from --source-profile <kit profile>.
        Without it the script stops with "BLOCKED: source target must come from a --source-profile" before loading
        anything or touching the network. Writes pass the kit write policy (read-only preview without a kit authorization).
    python3 mds_user_automation/copy_nve_rates_to_mds.py --profile <target> --source-profile <source> --plan-offset <N>
        --name-prefix <PREFIX> --dry-run
    ... (no --dry-run)                       # create the missing plans + verify
    ... --plans 104,105                      # only these source plans
    python3 mds_user_automation/copy_nve_rates_to_mds.py --phases verify    # re-check earlier copies
    python3 mds_user_automation/copy_nve_rates_to_mds.py --feed-in-rate 0.03  # MDS-proper copies (+ RECEIVED band)

OUTPUT  results/nve_rate_copy/<ts>/: plan_diff.json, payloads/, created.json, verify.json, nve_rate_copy_report.md
Exit code: 0 = every plan to create was created and verified, 1 = a plan failed, 2 = setup error.
"""
from __future__ import annotations

import argparse
import base64
import copy
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mds_common as mc
import mds_target  # noqa: E402
from mds_common import SetupError, api, log, payload, status_of  # noqa: E402
import create_mds_nve_tou_rate_plan as NP  # noqa: E402
from create_mds_nve_tou_rate_plan import CONFIG_COMPONENT_KEYS, CONFIG_RATE_KEYS, dbq, iso  # noqa: E402

HERE = Path(__file__).resolve().parent
# the SOURCE comes only from --source-profile (set by _use_source in main); no built-in host, pilot or token
SOURCE_BASE: str | None = None
SOURCE_PILOT: int | None = None
SNAPSHOT: Path | None = None
# The target plan number offset and name prefix are required arguments (--plan-offset, --name-prefix): no default.
# PLAN_OFFSET stays importable as None so an importer that still expects a default fails closed instead of guessing.
PLAN_OFFSET: int | None = None
PHASES = ("source", "diff", "post", "verify")
MDS_STREAMS = {"DELIVERED", "RECEIVED"}           # an the target pilot MDS user is D&R solar: a plan must price both
# RateInfo fields POST /v3.0/rates accepts (the /structure rows carry these plus erateId/tou/threshold)
RATE_KEYS = ("monthLow", "dayLow", "monthHigh", "dayHigh", "consumptionLow", "consumptionHigh", "timeOfDayLow",
             "timeOfDayHigh", "weekLow", "weekHigh", "validLow", "validHigh", "rate", "chargeType", "chargeSubType",
             "touName", "tierName", "groupName", "isHoliday", "rateBandId", "rateBandName", "description", "attributes")


# ---------------------------------------------------------------------------
# source
# ---------------------------------------------------------------------------
def mint_token(base: str, cred_file: str) -> str:
    """OAuth client_credentials token (the Jenkins self-service client, see Confluence DEVOPS "Generating API
    Access token"): cred_file is a mode-600 JSON {"client_id": ..., "client_secret": ...}."""
    cred = json.loads(Path(cred_file).expanduser().read_text())
    basic = base64.b64encode(f"{cred['client_id']}:{cred['client_secret']}".encode()).decode()
    mc._SECRETS.append(cred["client_secret"])
    req = urllib.request.Request(f"{base}/oauth/token?grant_type=client_credentials&scope=all",
                                 headers={"Authorization": f"Basic {basic}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            tok = json.loads(r.read().decode()).get("access_token")
    except urllib.error.HTTPError as exc:
        raise SetupError(f"token mint at {base} -> HTTP {exc.code}") from exc
    if not tok:
        raise SetupError(f"token mint at {base}: no access_token in the response")
    mc._SECRETS.append(tok)
    return tok


def nve_token(a: argparse.Namespace) -> str:
    if a.nve_client_file:
        return mint_token(SOURCE_BASE, a.nve_client_file)
    return NP.source_token(a.nve_token_file)          # --nve-token-file, else the source profile's api.token_file


def _use_source(src: dict) -> None:
    global SOURCE_BASE, SOURCE_PILOT, SNAPSHOT
    NP.use_source(src)
    SOURCE_BASE, SOURCE_PILOT = src["base"], src["pilot"]
    SNAPSHOT = HERE / "fixtures" / f"nve_uat_{SOURCE_PILOT}_all_plans.json"


def nve_get(path: str, token: str) -> Any:
    """GET on NVE UAT with retries: the <source deployment>-uat LB drops idle/parallel connections now and then."""
    for attempt in range(1, 6):
        req = urllib.request.Request(f"{SOURCE_BASE}/{path.lstrip('/')}",
                                     headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise SetupError(f"NVE UAT GET {path} -> HTTP {exc.code}: the NVE token is expired or has no access "
                                 "(mint one with --nve-client-file)") from exc
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 5:
                raise SetupError(f"NVE UAT GET {path} -> HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            if attempt == 5:
                raise SetupError(f"NVE UAT GET {path} -> {exc}") from exc
        time.sleep(3 * attempt)
    raise SetupError(f"NVE UAT GET {path}: retries exhausted")


def fetch_source(a: argparse.Namespace) -> dict:
    if not SOURCE_BASE or SOURCE_PILOT is None or SNAPSHOT is None:
        raise SetupError(NP.BLOCKED_SOURCE)
    tok = nve_token(a)
    if not tok or a.use_snapshot:
        if not SNAPSHOT.exists():
            raise SetupError(f"no source token and no snapshot {SNAPSHOT.name}: the --source-profile's api.token_file "
                             "or --nve-token-file")
        snap = json.loads(SNAPSHOT.read_text())
        log(f"SOURCE snapshot {SNAPSHOT.name}: {len(snap['plans'])} plans, fetched {snap['fetchedAt']}")
        return snap
    plans = payload(nve_get(f"v3.0/rates/configuration/utilityId/{SOURCE_PILOT}", tok)) or []
    if not plans:
        raise SetupError(f"NVE UAT {SOURCE_PILOT}: the configuration GET returned no plans")

    def rows_of(p: dict) -> tuple[int, list[dict], str]:
        n = p["planNumber"]
        try:
            rows = payload(nve_get(f"v3.0/rates/utilities/{SOURCE_PILOT}/plans/{n}/structure", tok)) or []
            if rows:
                return n, rows, "structure"
        except SetupError as exc:
            log(f"  NVE plan {n}: /structure failed ({exc}); using the configuration rows", "WARN")
        # fallback: the configuration rows (validity as dates, groupName possibly null)
        rows = []
        for c in p.get("components", []):
            for r in c.get("rates", []):
                rows.append({**r, "rateBandId": str(c["rateBandId"]), "rateBandName": c.get("rateBandName"),
                             "validLow": to_epoch(r["validLow"]), "validHigh": to_epoch(r["validHigh"], end=True),
                             "attributes": c.get("attributes") or r.get("attributes")})
        return n, rows, "configuration"

    with ThreadPoolExecutor(a.workers) as ex:
        got = {n: (rows, via) for n, rows, via in ex.map(rows_of, plans)}
    out = []
    for p in sorted(plans, key=lambda x: x["planNumber"]):
        rows, via = got[p["planNumber"]]
        out.append({"planNumber": p["planNumber"], "planName": p.get("planName"),
                    "planDescription": p.get("planDescription"), "residential": p.get("residential", True),
                    "via": via, "rows": rows})
    snap = {"source": f"{SOURCE_BASE} pilot {SOURCE_PILOT}",
            "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "plans": out}
    SNAPSHOT.parent.mkdir(exist_ok=True)
    SNAPSHOT.write_text(json.dumps(snap, indent=1) + "\n")
    log(f"SOURCE NVE UAT {SOURCE_PILOT}: {len(out)} plans, {sum(len(p['rows']) for p in out)} rows "
        f"({Counter(p['via'] for p in out)}) -> {SNAPSHOT.name}")
    return snap


def to_epoch(v: Any, end: bool = False) -> int:
    if isinstance(v, (int, float)):
        return int(v)
    d = datetime.strptime(str(v)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(d.timestamp()) + (86399 if end else 0)


# ---------------------------------------------------------------------------
# diff: what the target pilot already has
# ---------------------------------------------------------------------------
def day(v: Any) -> str:
    return iso(v) if isinstance(v, (int, float)) else str(v)[:10]


def signature(rows: list[dict]) -> Counter:
    """Rate content, comparable between NVE /structure rows and the target pilot configuration rows (groupName excluded:
    the configuration GET drops it; validity compared as UTC dates)."""
    return Counter((int(r["monthLow"]), int(r["dayLow"]), int(r["monthHigh"]), int(r["dayHigh"]),
                    float(r["consumptionLow"]), float(r["consumptionHigh"]), int(r["timeOfDayLow"]),
                    int(r["timeOfDayHigh"]), int(r["weekLow"]), int(r["weekHigh"]), day(r["validLow"]),
                    day(r["validHigh"]), round(float(r["rate"]), 6), r["chargeType"], r.get("touName") or None,
                    r.get("tierName") or None, bool(r.get("isHoliday")), str(r.get("rateBandId")))
                   for r in rows)


def target_rows_of(mp_plan: dict) -> list[dict]:
    return [{**r, "rateBandId": r.get("rateBandId", c.get("rateBandId"))}
            for c in mp_plan.get("components", []) for r in c.get("rates", [])]


def live_plans() -> list[dict]:
    return payload(api("GET", f"v3.0/rates/configuration/utilityId/{mc.PILOT_ID}", allow=(404,))) or []


def pilot_streams() -> set[str]:
    """data_stream types the pilot ingests: launchpad_ingestion_configs.supported_streams, every fuel. A copied rate
    priced on any other stream can never match an interval this pilot stores."""
    lic = mc.read_config("pilot", mc.PILOT_ID, "launchpad_ingestion_configs")
    ss = json.loads(mc.cfg(lic, "supported_streams", "{}") or "{}")
    out = {s_["streamType"] for x in ss.get("supportedStreams", []) for s_ in x.get("streams", [])}
    if not out:
        raise SetupError(f"cannot read supported_streams of pilot {mc.PILOT_ID}: no plan is copied without it")
    return out


def diff(a: argparse.Namespace, snap: dict, live: list[dict]) -> list[dict]:
    supported = pilot_streams()
    log(f"pilot {mc.PILOT_ID} supported streams: {sorted(supported)}: only plans priced on these are copied")
    by_num = {p["planNumber"]: p for p in live}
    by_name = {p.get("planName"): p for p in live}
    sigs = {p["planNumber"]: signature(target_rows_of(p)) for p in live}
    out = []
    for src in snap["plans"]:
        n = src["planNumber"]
        tgt, name = n + a.plan_offset, f"{a.name_prefix}{src['planName']}"
        d = {"nvePlan": n, "nveName": src["planName"], "rows": len(src["rows"]), "plan": tgt, "name": name}
        ported = next((p for p in live if re.search(rf"NVE UAT {SOURCE_PILOT} plan {n}\b", p.get("planDescription") or "")), None)
        want_sig = signature(build_rows(a, src, tgt)) if src["rows"] else None
        same = next((num for num, s in sigs.items() if want_sig and s == want_sig), None)
        cur = by_num.get(tgt)
        if a.plans and n not in a.plans:
            d["status"] = "not-selected"
        elif not src["rows"]:
            d["status"], d["why"] = "empty", "no rate rows on NVE UAT"
        elif (conflicts := mds_band_conflicts(src["rows"])) and not (cur and cur.get("planName") == name
                                                                    and len(target_rows_of(cur)) == len(src["rows"])):
            d["status"], d["why"] = "mds-incompatible", "; ".join(conflicts)
        elif not ported and (extra := plan_streams(build_rows(a, src, tgt)) - supported):
            d["status"] = "not-supported"
            d["why"] = f"prices streams {sorted(extra)} that pilot {mc.PILOT_ID} does not support ({sorted(supported)})"
        elif not ported and not MDS_STREAMS <= plan_streams(build_rows(a, src, tgt)):
            d["status"] = "not-mds"
            d["why"] = (f"prices only {sorted(plan_streams(build_rows(a, src, tgt)))}: an MDS D&R user and the RC API need "
                        "RECEIVED too (pass --feed-in-rate to add a flat export band)")
        elif cur and cur.get("planName") == name:
            have = len(target_rows_of(cur))
            want = len(build_rows(a, src, tgt))
            d["status"] = "copied" if have == want else "partial"
            d["why"] = f"target plan {tgt} {name} has {have} rows (want {want})"
        elif cur:
            d["status"], d["why"] = "conflict", f"target plan {tgt} is {cur.get('planName')!r}"
        elif name in by_name:
            d["status"], d["why"] = "conflict", f"name {name} already belongs to target plan {by_name[name]['planNumber']}"
        elif ported:
            d["status"], d["why"] = "ported", f"target plan {ported['planNumber']} {ported.get('planName')} is its port"
        else:
            d["status"] = "create"
            if same is not None:
                # NVE has distinct plans (names) with identical rates, e.g. 205/208/209/212; METERENROLL resolves by
                # name, so identical rates under another name do not make the plan exist
                d["why"] = f"note: same rates as target plan {same} {by_num[same].get('planName')}"
        defects = source_defects(src["rows"])
        if defects:
            d["sourceDefects"] = defects
        if d["status"] == "partial" and a.repair:
            d["status"] = "create"
        out.append(d)
    return out


def plan_streams(rows: list[dict]) -> set[str]:
    return {(r.get("attributes") or {}).get("data_stream", "DELIVERED") for r in rows}


def db_streams(plans: list[int]) -> dict[int, set[str]]:
    """data_stream per live plan from eRates.attributes. The configuration GET drops per-rate attributes, so its
    rows cannot tell a D&R plan from a DELIVERED-only one; /structure has them but 404s for plans the data-server
    cache has not loaded."""
    if not plans:
        return {}
    rows = dbq(f"SELECT planNumber, attributes FROM eRates WHERE utilityId={mc.PILOT_ID} AND planNumber IN "
               f"({','.join(str(int(n)) for n in plans)}) GROUP BY planNumber, attributes")
    if rows is None:
        raise SetupError("eRates attributes unreadable (config-fail-testcase-debugger dbq tunnel missing): cannot tell MDS streams")
    out: dict[int, set[str]] = {int(n): set() for n in plans}
    for r in rows:
        try:
            out[int(r["planNumber"])].add(json.loads(r.get("attributes") or "{}").get("data_stream", "DELIVERED"))
        except (TypeError, ValueError):
            out[int(r["planNumber"])].add("DELIVERED")
    return out


def mds_band_conflicts(rows: list[dict]) -> list[str]:
    """UtilityRatesManager's MDS rule (both rates APIs reject violations): per data_stream, a chargeType maps to ONE
    rateBandId and a rateBandId to ONE chargeType. NVE's Y-series demand plans split daily demand over bands 4/5/6
    and mix DEMAND_WINDOW + OUT_OF_DEMAND_WINDOW in bands 7/8, so they cannot be written to an MDS pilot as-is."""
    by_ct: dict[tuple, set] = {}
    by_band: dict[tuple, set] = {}
    for r in rows:
        stream = (r.get("attributes") or {}).get("data_stream", "DELIVERED")
        by_ct.setdefault((r["chargeType"], stream), set()).add(str(r["rateBandId"]))
        by_band.setdefault((str(r["rateBandId"]), stream), set()).add(r["chargeType"])
    out = [f"{ct}+{st} in bands {sorted(b)}" for (ct, st), b in sorted(by_ct.items()) if len(b) > 1]
    out += [f"band {b}+{st} holds {sorted(c)}" for (b, st), c in sorted(by_band.items()) if len(c) > 1]
    return out


def source_defects(rows: list[dict]) -> list[str]:
    """NVE rows both rates APIs reject; build_rows clamps them to the same coverage and the report lists them."""
    out = []
    for r in rows:
        if not (1 <= r["monthLow"] <= 12 and 1 <= r["monthHigh"] <= 12):
            out.append(f"{r['chargeType']} holiday={bool(r.get('isHoliday'))} month {r['monthLow']}-{r['monthHigh']}")
        if not (0 <= r["timeOfDayLow"] <= 23 and 0 <= r["timeOfDayHigh"] <= 23):
            out.append(f"{r['chargeType']} hours {r['timeOfDayLow']}-{r['timeOfDayHigh']}")
    return out


# ---------------------------------------------------------------------------
# build + post
# ---------------------------------------------------------------------------
def build_rows(a: argparse.Namespace, src: dict, tgt: int) -> list[dict]:
    """POST /v3.0/rates rows: NVE rows verbatim + data_stream (+ optional holiday twins / RECEIVED feed-in)."""
    rows = []
    for r in src["rows"]:
        x = {k: copy.deepcopy(r.get(k)) for k in RATE_KEYS}
        # the only change to NVE values: out-of-range months/hours (e.g. NVE 104 fixed row month 0-12), which both
        # rates APIs reject, are clamped to the same coverage (month 0 does not exist). source_defects() reports them.
        x.update(monthLow=min(max(int(r["monthLow"]), 1), 12), monthHigh=min(max(int(r["monthHigh"]), 1), 12),
                 timeOfDayLow=min(max(int(r["timeOfDayLow"]), 0), 23), timeOfDayHigh=min(max(int(r["timeOfDayHigh"]), 0), 23))
        x.update(utilityId=mc.PILOT_ID, planNumber=tgt, rateBandId=str(r.get("rateBandId")),
                 validLow=int(r["validLow"]), validHigh=int(r["validHigh"]), isHoliday=bool(r.get("isHoliday")),
                 rateBandName=r.get("rateBandName") or "Electricity Charges",
                 attributes=r.get("attributes") or {"data_stream": "DELIVERED"})
        rows.append(x)
    if a.holiday_twins and any(r["isHoliday"] for r in rows):
        # the engine prices a holiday ONLY from holiday rows once a plan has any: twin bands that have none
        bands_with = {(r["rateBandId"], r["chargeType"]) for r in rows if r["isHoliday"]}
        rows += [{**copy.deepcopy(r), "isHoliday": True} for r in rows
                 if not r["isHoliday"] and (r["rateBandId"], r["chargeType"]) not in bands_with]
    if a.feed_in_rate is not None:
        band = str(max(int(r["rateBandId"]) for r in rows) + 1)
        lo, hi = min(r["validLow"] for r in rows), max(r["validHigh"] for r in rows)
        for hol in sorted({r["isHoliday"] for r in rows}):
            rows.append({"utilityId": mc.PILOT_ID, "planNumber": tgt, "monthLow": 1, "dayLow": 1, "monthHigh": 12,
                         "dayHigh": 31, "consumptionLow": 0, "consumptionHigh": 999999999, "timeOfDayLow": 0,
                         "timeOfDayHigh": 23, "weekLow": 1, "weekHigh": 7, "validLow": lo, "validHigh": hi,
                         "rate": a.feed_in_rate, "chargeType": "CONSUMPTION_BASED", "chargeSubType": None,
                         "touName": None, "tierName": None, "groupName": "Feed-In", "isHoliday": hol,
                         "rateBandId": band, "rateBandName": "Feed-In Credit", "description": None,
                         "attributes": {"data_stream": "RECEIVED"}})
    return rows


def plan_head(src: dict, tgt: int, name: str, rows: list[dict]) -> dict:
    """Rates Configuration body with only the fields the deployed DTOs accept."""
    comps: dict[tuple, dict] = {}
    for r in rows:
        c = comps.setdefault((int(r["rateBandId"]), r["isHoliday"]), {
            "rateBandId": int(r["rateBandId"]), "utilityId": mc.PILOT_ID, "planNumber": tgt,
            "holidaysEnabled": r["isHoliday"], "rateBandName": r["rateBandName"], "rates": []})
        rate = {k: r.get(k) for k in CONFIG_RATE_KEYS}
        rate.update(validLow=iso(r["validLow"]), validHigh=iso(r["validHigh"]), rateBandId=int(r["rateBandId"]))
        c["rates"].append(rate)
    desc = f"NVE UAT {SOURCE_PILOT} plan {src['planNumber']} {src['planName']} (copied {datetime.now():%Y-%m-%d})"
    return {"utilityId": mc.PILOT_ID, "mappedUtility": None, "planNumber": tgt, "planName": name, "sourceFile": None,
            "planDescription": desc, "residential": bool(src.get("residential", True)),
            "components": [{k: v for k, v in c.items() if k in CONFIG_COMPONENT_KEYS | {"rates"}}
                           for _, c in sorted(comps.items())]}


def api_error(r: Any) -> str | None:
    if status_of(r) != 200:
        return f"HTTP {status_of(r)}: {json.dumps(r)[:400]}"
    if isinstance(r, dict) and r.get("error"):
        return f"error {json.dumps(r['error'])[:400]}"
    return None


def post_plan(src: dict, d: dict, rows: list[dict], head: dict) -> None:
    tgt, name = d["plan"], d["name"]
    r = api("POST", f"v3.0/rates/configuration/utilityId/{mc.PILOT_ID}", [head], allow=(400, 404, 409, 500))
    if status_of(r) == 400:
        # the configuration API validates windows (e.g. "Overlapping windows are provided" for NVE's holiday rows,
        # which the engine resolves by lower bound). It only has to create the plan row here -- step 2 replaces its
        # rates -- so retry with one row; every NVE row still goes in through POST /v3.0/rates.
        ok = [(c, x) for c in head["components"] for x in c["rates"]
              if 1 <= x["monthLow"] <= x["monthHigh"] <= 12 and 0 <= x["timeOfDayLow"] <= x["timeOfDayHigh"] <= 23]
        comp, rate = ok[0] if ok else (head["components"][0], head["components"][0]["rates"][0])
        mini = {**head, "components": [{**comp, "rates": [rate]}]}
        log(f"  configuration POST 400 ({json.dumps(r.get('error'))[:160]}); retrying the plan row with 1 rate", "WARN")
        r = api("POST", f"v3.0/rates/configuration/utilityId/{mc.PILOT_ID}", [mini], allow=(400, 404, 409, 500))
    if err := api_error(r):
        raise SetupError(f"configuration POST {err}")
    r = api("DELETE", f"v3.0/rates/utilities/{mc.PILOT_ID}/plans/{tgt}", allow=(207, 400, 404, 500))
    if status_of(r) not in (200, 204, 207, 404):
        raise SetupError(f"DELETE eRates HTTP {status_of(r)}: {json.dumps(r)[:300]}")
    r = api("POST", "v3.0/rates", rows, allow=(400, 404, 409, 500))
    err = api_error(r) or (json.dumps(payload(r))[:400] if isinstance(payload(r), dict) and payload(r) else None)
    if err:
        raise SetupError(f"POST /v3.0/rates {err}")
    r = api("POST", f"v3.0/rate-info/utilities/{mc.PILOT_ID}/{tgt}/rate-resources",
            {"planName": name, "description": head["planDescription"]}, allow=(400, 404, 500))
    if err := api_error(r):
        raise SetupError(f"rate-resources POST {err}")


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------
def verify(a: argparse.Namespace, snap: dict, todo: list[dict]) -> list[dict]:
    live = {p["planNumber"]: p for p in live_plans()}
    src_by = {p["planNumber"]: p for p in snap["plans"]}
    db = None
    if todo:
        lo, hi = min(d["plan"] for d in todo), max(d["plan"] for d in todo)
        db = dbq(f"SELECT planNumber, COUNT(*) n, SUM(groupName IS NULL) no_group, "
                 f"SUM(attributes LIKE '%data_stream%') with_stream, SUM(chargeSubType IS NOT NULL) with_subtype FROM eRates WHERE utilityId={mc.PILOT_ID} "
                 f"AND planNumber BETWEEN {lo} AND {hi} GROUP BY planNumber")
    db_by = {int(x["planNumber"]): x for x in db or []}

    def one(d: dict) -> dict:
        src, cur = src_by[d["nvePlan"]], live.get(d["plan"])
        want_rows = build_rows(a, src, d["plan"])
        v = {"nvePlan": d["nvePlan"], "plan": d["plan"], "name": d["name"], "checks": {}}
        v["checks"]["plan"] = bool(cur and cur.get("planName") == d["name"])
        got = signature(target_rows_of(cur)) if cur else Counter()
        want = signature(want_rows)
        v["checks"]["rates"] = got == want
        if got != want:
            v["missing"] = [list(k) for k in (want - got)][:5]
            v["extra"] = [list(k) for k in (got - want)][:5]
        rr = payload(api("GET", f"v3.0/rate-info/utilities/{mc.PILOT_ID}/{d['plan']}/rate-resources",
                         allow=(404, 500))) or {}
        v["checks"]["rateResources"] = rr.get("planName") == d["name"]
        if db is not None:
            x = db_by.get(d["plan"], {})
            src_nogroup = sum(1 for r in want_rows if not r.get("groupName"))
            src_subtype = sum(1 for r in want_rows if r.get("chargeSubType"))
            v["checks"]["db"] = (int(x.get("n") or 0) == len(want_rows) and int(x.get("with_stream") or 0) == len(want_rows)
                                 and int(x.get("no_group") or 0) == src_nogroup
                                 and int(x.get("with_subtype") or 0) == src_subtype)
            v["db"] = x
        v["status"] = "PASS" if all(v["checks"].values()) else "FAIL"
        return v

    with ThreadPoolExecutor(a.workers) as ex:
        return list(ex.map(one, todo))


# ---------------------------------------------------------------------------
def report(out: Path, snap: dict, plan_diff: list[dict], created: list[dict], checks: list[dict]) -> None:
    ver = {v["nvePlan"]: v for v in checks}
    made = {c["nvePlan"]: c for c in created}
    counts = Counter(d["status"] for d in plan_diff)
    lines = [f"# NVE UAT {SOURCE_PILOT} rates -> target pilot {mc.PILOT_ID}", "",
             f"source: {snap['source']} (fetched {snap['fetchedAt']}), {len(snap['plans'])} plans", "",
             "diff: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())), "",
             "| NVE plan | NVE name | rows | target plan | target name | diff | result | verify |", "|---|---|---|---|---|---|---|---|"]
    for d in plan_diff:
        if d["status"] == "not-selected":
            continue
        res = made.get(d["nvePlan"], {}).get("result", "")
        vs = ver.get(d["nvePlan"], {}).get("status", "")
        lines.append(f"| {d['nvePlan']} | {d['nveName']} | {d['rows']} | {d['plan']} | {d['name']} | {d['status']}"
                     f"{': ' + d['why'] if d.get('why') else ''}{' (NVE defect: ' + '; '.join(d['sourceDefects']) + ')' if d.get('sourceDefects') else ''} | {res} | {vs} |".replace("\n", " "))
    (out / "nve_rate_copy_report.md").write_text("\n".join(lines) + "\n")
    log(f"report {out / 'nve_rate_copy_report.md'}")


def main(argv: list[str] | None = None) -> int:
    argv, source_profile = NP.take_source_profile(argv)
    if not source_profile:                      # before the profile, any token or any network call
        print(NP.BLOCKED_SOURCE, file=sys.stderr)
        return 2
    argv = mds_target.bootstrap(argv)          # PROFILE_REQUIRED before anything touches a target
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--phases", default="all", help=f"comma list of {','.join(PHASES)} or all")
    p.add_argument("--plans", default="", help="comma list of NVE plan numbers (default: every plan)")
    p.add_argument("--plan-offset", type=int, required=True, help="target plan = source plan + offset (no default)")
    p.add_argument("--name-prefix", required=True, help="target plan name = prefix + source plan name (no default)")
    p.add_argument("--repair", action="store_true", help="re-create PARTIAL copies (row count differs)")
    p.add_argument("--holiday-twins", action="store_true",
                   help="add holiday copies to bands without holiday rows in plans that have some (else holidays price $0)")
    p.add_argument("--feed-in-rate", type=float, help="add a flat RECEIVED feed-in band at this $/kWh (MDS D&R solar)")
    p.add_argument("--use-snapshot", action="store_true", help="read the saved NVE snapshot even with a token")
    p.add_argument("--nve-token-file", help="source token file (default: the --source-profile's api.token_file)")
    p.add_argument("--nve-client-file", help='mode-600 JSON {"client_id","client_secret"} to mint a source token')
    p.add_argument("--token-file", help="file holding a target API token (else env BIDGELY_TOKEN)")
    p.add_argument("--client-file", help='mode-600 JSON {"client_id","client_secret"} to mint a target token')
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--dry-run", action="store_true", help="diff + payloads only, no writes")
    p.add_argument("--output-dir", default=str(HERE / "results" / "nve_rate_copy"))
    a = p.parse_args(argv)
    try:
        _use_source(NP.load_source(source_profile))
    except Exception as exc:  # noqa: BLE001 - a bad source profile stops before any network call
        print(f"{NP.BLOCKED_SOURCE}: {getattr(exc, 'message', exc)}", file=sys.stderr)
        return 2
    a.plans = {int(x) for x in a.plans.split(",") if x.strip()}
    phases = list(PHASES) if a.phases == "all" else [x.strip() for x in a.phases.split(",") if x.strip()]
    out = Path(a.output_dir) / f"{datetime.now():%Y%m%d_%H%M%S}"
    (out / "payloads").mkdir(parents=True, exist_ok=True)
    mc._LOG_FH = open(out / "run.log", "a")
    created: list[dict] = []
    checks: list[dict] = []
    try:
        mc._TOKEN = mint_token(mc.BASE_URL, a.client_file) if a.client_file else mc.get_token(a.token_file)
        snap = fetch_source(a)
        plan_diff = diff(a, snap, live_plans())
        (out / "plan_diff.json").write_text(json.dumps(plan_diff, indent=1))
        counts = Counter(d["status"] for d in plan_diff)
        log("DIFF NVE UAT vs target: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        for d in plan_diff:
            if d.get("sourceDefects") and d["status"] != "not-selected":
                log(f"  NVE {d['nvePlan']} {d['nveName']}: source data defect clamped to 1-12 / 0-23: {d['sourceDefects']}", "WARN")
            if d["status"] not in ("create", "not-selected", "copied"):
                log(f"  NVE {d['nvePlan']} {d['nveName']}: {d['status'].upper()} ({d.get('why')})",
                    "WARN" if d["status"] in ("conflict", "partial", "mds-incompatible", "not-mds", "not-supported") else "INFO")
        src_by = {s["planNumber"]: s for s in snap["plans"]}
        todo = [d for d in plan_diff if d["status"] == "create"]
        if "post" in phases:
            for i, d in enumerate(todo, 1):
                src = src_by[d["nvePlan"]]
                rows = build_rows(a, src, d["plan"])
                head = plan_head(src, d["plan"], d["name"], rows)
                (out / "payloads" / f"{d['plan']}.json").write_text(json.dumps({"configuration": [head], "rates": rows}, indent=1))
                if a.dry_run:
                    continue
                try:
                    post_plan(src, d, rows, head)
                    created.append({**d, "result": "created", "rowsPosted": len(rows)})
                    log(f"[{i}/{len(todo)}] CREATED target plan {d['plan']} {d['name']} ({len(rows)} rows) "
                        f"from NVE {d['nvePlan']}")
                except SetupError as exc:
                    created.append({**d, "result": f"FAILED {exc}"[:300]})
                    log(f"[{i}/{len(todo)}] FAILED target plan {d['plan']} {d['name']}: {exc}", "ERROR")
            (out / "created.json").write_text(json.dumps(created, indent=1))
            if a.dry_run:
                log(f"DRY RUN: {len(todo)} plan payloads in {out / 'payloads'}; nothing written")
        if "verify" in phases and not a.dry_run:
            targets = [d for d in plan_diff if d["status"] in ("create", "copied", "partial")]
            if "post" in phases:
                time.sleep(5)
            checks = verify(a, snap, targets)
            (out / "verify.json").write_text(json.dumps(checks, indent=1))
            for v in checks:
                if v["status"] != "PASS":
                    log(f"  VERIFY FAIL target plan {v['plan']} {v['name']}: {v['checks']} "
                        f"missing {v.get('missing')} extra {v.get('extra')}", "ERROR")
            log(f"VERIFY {sum(v['status'] == 'PASS' for v in checks)}/{len(checks)} plans PASS")
        report(out, snap, plan_diff, created, checks)
        failed = any(c["result"] != "created" for c in created) or any(v["status"] != "PASS" for v in checks)
        return 1 if failed else 0
    except SetupError as exc:
        log(str(exc), "ERROR")
        return 2
    finally:
        if mc._LOG_FH:
            mc._LOG_FH.close()


if __name__ == "__main__":
    sys.exit(main())
