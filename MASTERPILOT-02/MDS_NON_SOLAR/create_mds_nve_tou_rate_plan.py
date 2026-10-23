#!/usr/bin/env python3
"""Create the NVE TOU rate plan on target pilot the target pilot (MDS) through the Rates Configuration API.

SOURCE  NVE UAT = deployment <source deployment>-uat (https://<source API host>.bidgely.com), pilot <source pilot> (NVE RESI).
        Default plan 198 OD-1-TOU_EVRR: NVE's 3-period seasonal TOU (EVRR family, as 104/105/123/.../196):
            summer Jun-Sep  Off-Peak 00-08 $0.07021 | Mid-Peak 08-18 $0.07737 | On-Peak 18-21 $0.47587 | Mid-Peak 21-24
            winter Oct-May  Off-Peak 00-08 $0.08239 | Mid-Peak 08-24 $0.0909
            same windows all 7 days, MONTHLY_FIXED $18.90, valid through 2027-12-31
        Rows are read from NVE's raw /structure endpoint (faithful: per-rate groupName Summer/Winter, one consumption
        rate band); the NVE config GET is groupName-lossy on that build. A snapshot is kept in
        fixtures/nve_uat_<source pilot>_plan<N>.json so a rerun does not need the NVE token.

TARGET  --plan-number/--plan-name (required; historical example: the target pilot plan 907 MDS_TOU_EVRR_D_R). Changes vs the
        NVE source, each needed on an MDS D&R solar pilot:
        * DELIVERED consumption + fixed rows are NVE's, verbatim (rates, touName, groupName, windows, validity).
        * holiday twins: NVE 198 has no holiday consumption rows. The pricing engine prices a holiday ONLY from holiday
          rows when any exist (RateProvider.findRateNodes: the fixed band has them), so holiday usage would cost $0.
          Every non-holiday consumption row gets an identical isHoliday=true copy.
        * RECEIVED feed-in: MDS stores export on its own RECEIVED stream; a flat CONSUMPTION_BASED feed-in band
          (--feed-in-rate, required; the historical port used $0.03/kWh) with attributes.data_stream=RECEIVED.
        * rateStructureImageUrl on every TOU row: TouRateStructureDataProvider returns null without it and TOU
          Onboarding drops (TOU_RATE_STRUCTURE_NOT_AVAILABLE) / TOU_BILL_SAVING_RECO NPEs (PE-14194). One generic image
          per season is rendered from the plan rows (headless Chrome, --chrome <binary>) and uploaded to the target's
          email-images bucket (profile email.provisioner.target.image_bucket / image_host, or --image-bucket /
          --image-host) under rate/mds<pilot>/. Without bucket+host (and without --image-url for every season) the
          images/post phases stop with BLOCKED before any token, source read or network call.

HOW     The deployed <deployment> rates DTOs predate PE-14194: the Rates Configuration API rejects component
        `attributes`, `chargeSubType` and `rateStructureImageUrl`, and MDS pricing skips rows without
        attributes.data_stream. So the plan is written in three steps (post_plan):
        1) POST /v3.0/rates/configuration/utilityId/the target pilot with whitelisted fields only: upserts the eUtilityPlans
           row (the plan name METERENROLL uses).
        2) DELETE /v3.0/rates/utilities/the target pilot/plans/<plan>, then legacy POST /v3.0/rates with every row (groupName,
           attributes.data_stream, holiday twins). addRates is a plain INSERT, so the DELETE keeps a rerun from
           duplicating rows.
        3) rateStructureImageUrl via one scoped SQL UPDATE (host/erates_image_update.py; --no-image-sql skips it).
        Then POST /v3.0/rate-info/utilities/the target pilot/<plan>/rate-resources (plan_name; an absent row NPEs TOU emails that
        read planName, see pilot-<pilot A>-rate-resources-missing).

VERIFY  config GET (reads live MySQL), the DB rows (read-only dbq tunnel, when the config-fail-testcase-debugger skill is installed),
        the image URLs (HTTP 200), rate-resources, and the cached /structure endpoint. /structure and the pricing path
        (SQLRatesDao) cache every the target pilot plan per JVM with no invalidation: a stale 404 there is reported, not retried
        (a re-POST is safe here -- the config API replaces -- but it cannot refresh a JVM cache).

RUN     The target comes from --profile (or BIDGELY_PROFILE); the SOURCE comes only from --source-profile <kit profile>
        (its api.url, pilot and api.token_file) plus --source-plan. Without --source-profile the script stops with
        "BLOCKED: source target must come from a --source-profile" before loading anything or touching the network.
    python3 mds_user_automation/create_mds_nve_tou_rate_plan.py --profile <target> --source-profile <source> --source-plan 198
        --plan-number <N> --plan-name <NAME> --feed-in-rate <$/kWh> --chrome <headless chrome binary>
    ... --refresh-source                     # re-read the source with the source profile's token, rewrite the fixture
    ... --phases verify                      # re-check only
    ... --dry-run                            # payload + images, no writes
        Writes (API, S3, the eRates SQL UPDATE) pass the kit write policy: without a kit authorization the run is a
        read-only preview, and the SQL UPDATE is refused under every kit authorization (it is printed for a human).

OUTPUT  results/rate_plans/<planName>_<ts>/: payload.json, images/, verify.json, rate_plan_report.md
Exit code: 0 = created and verified (WARN allowed), 1 = a check FAILed, 2 = setup error.
"""
from __future__ import annotations

import argparse
import copy
import json
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mds_common as mc
import mds_target  # noqa: E402
from mds_common import SetupError, api, log, payload, status_of  # noqa: E402

HERE = Path(__file__).resolve().parent
# the SOURCE (upper environment) of the port comes ONLY from --source-profile (a debug-kit profile: api.url, pilot,
# api.token_file); there is no built-in source host, pilot, plan or token variable. Set by use_source().
SOURCE_BASE: str | None = None
SOURCE_PILOT: int | None = None
SOURCE_TOKEN_FILE: str | None = None
BLOCKED_SOURCE = ("BLOCKED: source target must come from a --source-profile <kit profile> (no built-in source "
                  "host/pilot/plan/token)")
# The target plan number/name, the feed-in rate and the Chrome binary are explicit arguments (no defaults). The image
# bucket/host come from the target profile (email.provisioner.target.image_bucket / image_host) or --image-bucket /
# --image-host; the key prefix is computed from the APPLIED target's pilot (image_settings), never at import time.
BLOCKED_IMAGES = ("BLOCKED: the images phase needs the target's image bucket and host: set "
                  "email.provisioner.target.image_bucket and image_host in the profile, pass --image-bucket/--image-host, "
                  "or pass --image-url SEASON=URL for every season")
DBQ = mc.SKILL_DIR
# the debug-kit installer keeps the venv outside the skill dir; older installs kept it inside
DBQ_PY = next((c for c in (Path.home() / ".local/share/bidgely-debug/venv/bin/python", DBQ / ".venv/bin/python")
               if c.exists()), DBQ / ".venv/bin/python")
PHASES = ("source", "build", "images", "post", "verify")
TOU_ORDER = {"Off-Peak": 0, "Mid-Peak": 1, "On-Peak": 2}
TOU_COLOR = {"Off-Peak": "#0079c1", "Mid-Peak": "#f2a900", "On-Peak": "#f15a2b"}
# the fields the DEPLOYED RatePlanConfiguration DTOs accept (= what its GET returns); anything else is a 400
CONFIG_COMPONENT_KEYS = {"rateBandId", "utilityId", "planNumber", "holidaysEnabled", "rateBandName"}
CONFIG_RATE_KEYS = {"monthLow", "dayLow", "monthHigh", "dayHigh", "consumptionLow", "consumptionHigh", "timeOfDayLow",
                    "timeOfDayHigh", "weekLow", "weekHigh", "validLow", "validHigh", "rate", "touName", "tierName",
                    "isHoliday", "groupName", "chargeType", "rateBandId", "utilityId", "planNumber"}
MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---------------------------------------------------------------------------
# source
# ---------------------------------------------------------------------------
def _profile_value(dotted: str):
    """A value of the loaded target profile (the debug kit's Target that mds_target.bootstrap validated), else None."""
    t = getattr(mds_target, "_TARGET", None)
    return t.section(dotted) if t is not None else None


def image_settings(a: argparse.Namespace) -> tuple[str | None, str | None, str]:
    """-> (bucket, host, key prefix): explicit args first, then the target profile; the prefix uses the applied pilot."""
    bucket = getattr(a, "image_bucket", None) or _profile_value("email.provisioner.target.image_bucket")
    host = getattr(a, "image_host", None) or _profile_value("email.provisioner.target.image_host")
    return (bucket or None, str(host).rstrip("/") if host else None, f"rate/mds{mc.need('PILOT_ID')}")


def take_source_profile(argv: list[str] | None) -> tuple[list[str], str | None]:
    """-> (argv without --source-profile <p>, p or None). Called BEFORE anything else: no profile, no network."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--source-profile" not in argv:
        return argv, None
    i = argv.index("--source-profile")
    if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
        return argv, None
    src = argv[i + 1]
    del argv[i:i + 2]
    return argv, src


def load_source(profile: str) -> dict:
    """The source environment of a port from a debug-kit profile (read-only use: GETs only)."""
    _, target = mds_target._kit()
    t = target.load_target(profile, {}, environ={})
    base, pilot = t.get("api.url"), t.pilot
    if not base or not str(pilot or "").isdigit():
        raise SetupError(f"source profile {profile}: api.url and a numeric pilot are required")
    return {"base": str(base).rstrip("/"), "pilot": int(pilot), "token_file": t.get("api.token_file"),
            "profile": t.settings.profile_path}


def use_source(src: dict) -> None:
    global SOURCE_BASE, SOURCE_PILOT, SOURCE_TOKEN_FILE
    SOURCE_BASE, SOURCE_PILOT, SOURCE_TOKEN_FILE = src["base"], src["pilot"], src.get("token_file")


def source_token(token_file: str | None) -> str:
    """The source token: --nve-token-file, else the source profile's api.token_file (mode 0600). Never an env var."""
    f = token_file or SOURCE_TOKEN_FILE
    if not f:
        return ""
    p = Path(f).expanduser()
    if not p.exists():
        return ""
    if p.stat().st_mode & 0o077:
        raise SetupError(f"{p} must be mode 0600 (chmod 600 {p})")
    tok = p.read_text().strip()
    if tok:
        mc._SECRETS.append(tok)
    return tok


def fixture_path(plan: int) -> Path:
    return HERE / "fixtures" / f"nve_uat_{SOURCE_PILOT}_plan{plan}.json"


def nve_get(path: str, token: str) -> Any:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(f"{SOURCE_BASE}/{path.lstrip('/')}",
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90, context=ctx) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        raise SetupError(f"NVE UAT GET {path} -> HTTP {exc.code}: {exc.read().decode(errors='replace')[:200]}") from exc


def fetch_source(a: argparse.Namespace) -> dict:
    if not SOURCE_BASE or SOURCE_PILOT is None:
        raise SetupError(BLOCKED_SOURCE)
    fx = fixture_path(a.source_plan)
    if not a.refresh_source and fx.exists():
        snap = json.loads(fx.read_text())
        log(f"SOURCE fixture {fx.name}: NVE UAT {SOURCE_PILOT} plan {snap['planNumber']} {snap['planName']} "
            f"({len(snap['rows'])} rows, fetched {snap['fetchedAt']})")
        return snap
    token = source_token(a.nve_token_file)
    if not token:
        raise SetupError(f"no fixture {fx.name} and no source token: the --source-profile's api.token_file or --nve-token-file")
    rows = payload(nve_get(f"v3.0/rates/utilities/{SOURCE_PILOT}/plans/{a.source_plan}/structure", token)) or []
    plans = payload(nve_get(f"v3.0/rates/configuration/utilityId/{SOURCE_PILOT}?planNumber={a.source_plan}", token)) or []
    name = next((p.get("planName") for p in plans if p.get("planNumber") == a.source_plan), None)
    if not rows or not name:
        raise SetupError(f"NVE UAT plan {a.source_plan} not found ({len(rows)} rows, name {name})")
    snap = {"source": f"{SOURCE_BASE} pilot {SOURCE_PILOT}", "planNumber": a.source_plan, "planName": name,
            "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "endpoint": f"GET /v3.0/rates/utilities/{SOURCE_PILOT}/plans/{a.source_plan}/structure", "rows": rows}
    fx.parent.mkdir(exist_ok=True)
    fx.write_text(json.dumps(snap, indent=1) + "\n")
    log(f"SOURCE NVE UAT {SOURCE_PILOT} plan {a.source_plan} {name}: {len(rows)} rows -> {fx}")
    return snap


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------
def season_of(r: dict) -> str:
    return r.get("groupName") or f"M{r['monthLow']:02d}-{r['monthHigh']:02d}"


def iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).date().isoformat()


def target_rows(src: list[dict], feed_in: float) -> list[dict]:
    """The the target pilot rows (engine field names). DELIVERED rows verbatim, holiday twins, flat RECEIVED feed-in."""
    keep = [r for r in src if (r.get("attributes") or {}).get("data_stream", "DELIVERED") == "DELIVERED"]
    base = [r for r in keep if not r.get("isHoliday")]
    out = []
    for r in base:
        for hol in (False, True):
            if r["chargeType"] != "CONSUMPTION_BASED" and hol and any(
                    x.get("isHoliday") and x["chargeType"] == r["chargeType"] for x in keep):
                src_h = next(x for x in keep if x.get("isHoliday") and x["chargeType"] == r["chargeType"])
                out.append({**copy.deepcopy(src_h), "isHoliday": True})
                continue
            out.append({**copy.deepcopy(r), "isHoliday": hol})
    lo = min(int(r["validLow"]) for r in base)
    hi = max(int(r["validHigh"]) for r in base)
    cons = [r for r in base if r["chargeType"] == "CONSUMPTION_BASED"]
    band = str(max(int(r["rateBandId"]) for r in keep) + 1)
    for hol in (False, True):
        out.append({"rateBandId": band, "rateBandName": "Feed-In Credit", "groupName": "Feed-In",
                    "chargeType": "CONSUMPTION_BASED", "touName": None, "tierName": None,
                    "monthLow": 1, "dayLow": 1, "monthHigh": 12, "dayHigh": 31, "timeOfDayLow": 0, "timeOfDayHigh": 23,
                    "weekLow": 1, "weekHigh": 7, "consumptionLow": 0, "consumptionHigh": cons[0]["consumptionHigh"],
                    "validLow": lo, "validHigh": hi, "rate": feed_in, "isHoliday": hol,
                    "attributes": {"data_stream": "RECEIVED"}})
    return out


def hour_labels(rows: list[dict], month: int, week: int) -> list[str | None]:
    """touName per hour 0..23 as the pricing engine resolves it: the row with the LARGEST timeOfDayLow <= hour
    (calc.Node.findChildNode lower-bound lookup), among rows valid for the month and weekday."""
    cand = [r for r in rows if r.get("touName") and not r.get("isHoliday") and r["chargeType"] == "CONSUMPTION_BASED"
            and (r.get("attributes") or {}).get("data_stream", "DELIVERED") == "DELIVERED"
            and r["monthLow"] <= month <= r["monthHigh"] and r["weekLow"] <= week <= r["weekHigh"]]
    out = []
    for h in range(24):
        below = [r for r in cand if r["timeOfDayLow"] <= h]
        out.append(max(below, key=lambda r: r["timeOfDayLow"])["touName"] if below else None)
    return out


def runs(labels: list[str | None]) -> list[tuple[int, int, str | None]]:
    out: list[tuple[int, int, str | None]] = []
    for h, lab in enumerate(labels):
        if out and out[-1][2] == lab:
            out[-1] = (out[-1][0], h + 1, lab)
        else:
            out.append((h, h + 1, lab))
    return out


def seasons(rows: list[dict]) -> dict[str, dict]:
    """season -> {months, weekday runs, weekend runs, rates by touName}."""
    out: dict[str, dict] = {}
    for r in rows:
        if not r.get("touName") or r.get("isHoliday") or r["chargeType"] != "CONSUMPTION_BASED":
            continue
        s = out.setdefault(season_of(r), {"months": set(), "rates": {}})
        s["months"].update(range(r["monthLow"], r["monthHigh"] + 1))
        s["rates"][r["touName"]] = float(r["rate"])
    for s in out.values():
        m = min(s["months"])
        s["weekday"], s["weekend"] = runs(hour_labels(rows, m, 1)), runs(hour_labels(rows, m, 6))
        s["months"] = sorted(s["months"])
    return out


def month_span(months: list[int]) -> str:
    ms = sorted(months)
    # wrap-around seasons (Oct..May) read better starting after the gap
    for i in range(1, len(ms)):
        if ms[i] != ms[i - 1] + 1:
            ms = ms[i:] + ms[:i]
            break
    return f"{MONTHS[ms[0]]}-{MONTHS[ms[-1]]}"


def build_payload(a: argparse.Namespace, snap: dict, image_urls: dict[str, str]) -> dict:
    rows = target_rows(snap["rows"], a.feed_in_rate)
    comps: dict[tuple, dict] = {}
    for r in rows:
        stream = (r.get("attributes") or {}).get("data_stream", "DELIVERED")
        key = (str(r["rateBandId"]), bool(r["isHoliday"]))
        c = comps.setdefault(key, {"rateBandId": int(r["rateBandId"]), "utilityId": mc.PILOT_ID,
                                   "planNumber": a.plan_number, "holidaysEnabled": bool(r["isHoliday"]),
                                   "rateBandName": r.get("rateBandName") or "Electricity Charges",
                                   "attributes": {"data_stream": stream}, "rates": []})
        rate = {"monthLow": r["monthLow"], "dayLow": r["dayLow"], "monthHigh": r["monthHigh"], "dayHigh": r["dayHigh"],
                "consumptionLow": r["consumptionLow"], "consumptionHigh": r["consumptionHigh"],
                "timeOfDayLow": r["timeOfDayLow"], "timeOfDayHigh": r["timeOfDayHigh"],
                "weekLow": r["weekLow"], "weekHigh": r["weekHigh"],
                "validLow": iso(r["validLow"]), "validHigh": iso(r["validHigh"]),
                "rate": r["rate"], "touName": r.get("touName"), "tierName": r.get("tierName"),
                "isHoliday": bool(r["isHoliday"]), "groupName": r.get("groupName"), "chargeType": r["chargeType"],
                "chargeSubType": r.get("chargeSubType"), "rateBandId": int(r["rateBandId"]),
                "utilityId": mc.PILOT_ID, "planNumber": a.plan_number,
                "_validLowTs": int(r["validLow"]), "_validHighTs": int(r["validHigh"])}   # for POST /v3.0/rates
        if r.get("touName") and season_of(r) in image_urls:
            rate["rateStructureImageUrl"] = image_urls[season_of(r)]
        c["rates"].append(rate)
    desc = (f"TOU EVRR (3-period, seasonal) from NVE UAT {SOURCE_PILOT} plan {snap['planNumber']} {snap['planName']}; "
            f"holiday twins + flat RECEIVED feed-in ${a.feed_in_rate}/kWh for MDS D&R")
    return {"utilityId": mc.PILOT_ID, "mappedUtility": None, "planNumber": a.plan_number, "planName": a.plan_name,
            "sourceFile": None, "planDescription": desc, "residential": True,
            "components": sorted(comps.values(), key=lambda c: (c["rateBandId"], c["holidaysEnabled"]))}


# ---------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------
def hour_txt(h: int) -> str:
    h %= 24
    return "12 AM" if h == 0 else ("12 PM" if h == 12 else (f"{h} AM" if h < 12 else f"{h - 12} PM"))


def panel_html(title: str, rn: list[tuple[int, int, str | None]], rates: dict[str, float]) -> str:
    height = {"Off-Peak": 60, "Mid-Peak": 92, "On-Peak": 124}
    bars, ticks = [], {0, 24}
    for s, e, lab in rn:
        ticks.update((s, e))
        left, width = s / 24 * 100, (e - s) / 24 * 100
        h = height.get(lab or "", 40)
        price = f"{rates[lab] * 100:.1f}&cent;" if lab in rates else ""
        bars.append(f'<div class="bar" style="left:calc({left}% + 2px);width:calc({width}% - 4px);height:{h}px;'
                    f'background:{TOU_COLOR.get(lab or "", "#999")}"><span class="lab" style="color:'
                    f'{TOU_COLOR.get(lab or "", "#999")}">{lab or ""}</span><span class="pr">{price}</span></div>')
    tick = "".join(f'<span class="tk" style="left:{t / 24 * 100}%">{hour_txt(t)}</span>' for t in sorted(ticks))
    return f'<div class="panel"><div class="ttl">{title}</div><div class="plot">{"".join(bars)}</div>' \
           f'<div class="ticks">{tick}</div></div>'


def season_html(name: str, s: dict) -> str:
    same = s["weekday"] == s["weekend"]
    panels = [panel_html("Every Day (including weekends and holidays)" if same else "Weekdays", s["weekday"], s["rates"])]
    if not same:
        panels.append(panel_html("Weekends and Holidays", s["weekend"], s["rates"]))
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
body{{margin:0;background:#fff;font-family:Arial,Helvetica,sans-serif;width:536px}}
.wrap{{padding:14px 26px 10px}} .panel{{margin-bottom:18px}} .ttl{{font-weight:bold;font-size:14px;margin-bottom:26px}}
.plot{{position:relative;height:130px;border-bottom:1px solid #ddd}}
.bar{{position:absolute;bottom:0;border-radius:6px 6px 0 0}}
.lab{{position:absolute;top:-19px;left:0;right:0;text-align:center;font-weight:bold;font-size:12px;white-space:nowrap}}
.pr{{position:absolute;bottom:6px;left:0;right:0;text-align:center;color:#fff;font-size:12px;font-weight:bold}}
.ticks{{position:relative;height:18px;font-size:11px;color:#333}}
.tk{{position:absolute;top:4px;transform:translateX(-50%);white-space:nowrap}}
.tk:first-child{{transform:none}} .tk:last-child{{transform:translateX(-100%)}}
.foot{{text-align:center;font-size:13px;color:#333;padding-top:4px}}
</style></head><body><div class="wrap">{"".join(panels)}
<div class="foot">Your Time-Of-Use Rate &middot; {name} ({month_span(s["months"])})</div></div></body></html>"""


def render_png(htmlt: str, out: Path, height: int, chrome: str | None) -> None:
    if not chrome:
        raise SetupError("no headless Chrome binary: pass --chrome <path> or --image-url SEASON=URL for every season")
    if not Path(chrome).exists():
        raise SetupError(f"headless Chrome not found at {chrome}: pass --chrome <path> or --image-url SEASON=URL for every season")
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "img.html"
        src.write_text(htmlt)
        cmd = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
               f"--window-size=536,{height}", f"--screenshot={out}", f"--user-data-dir={td}/prof", src.as_uri()]
        if out.exists():
            out.unlink()
        # headless Chrome writes the screenshot within seconds but can keep running afterwards: wait for the file
        # to appear and settle, then stop the process
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        size, deadline = -1, time.time() + 90
        while time.time() < deadline:
            time.sleep(1)
            if out.exists() and out.stat().st_size > 1000 and out.stat().st_size == size:
                break
            size = out.stat().st_size if out.exists() else -1
            if p.poll() is not None and not out.exists():
                break
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        if not out.exists() or out.stat().st_size < 1000:
            raise SetupError(f"Chrome screenshot failed ({p.returncode}): {(p.stderr.read() if p.stderr else '')[-300:]}")


def http_status(url: str) -> int:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, TimeoutError):
        return 0


def images(a: argparse.Namespace, snap: dict, out: Path) -> dict[str, str]:
    given = dict(x.split("=", 1) for x in a.image_url)
    ss = seasons(target_rows(snap["rows"], a.feed_in_rate))
    bucket, host, prefix = image_settings(a)
    if any(n not in given for n in ss) and not (bucket and host):
        raise SetupError(BLOCKED_IMAGES)
    urls: dict[str, str] = {}
    idir = out / "images"
    idir.mkdir(exist_ok=True)
    s3 = None
    for name, s in ss.items():
        if name in given:
            urls[name] = given[name]
            log(f"IMAGE {name}: given {given[name]}")
            continue
        key = f"{prefix}/{a.plan_name}_{a.plan_number}_{name.lower()}.png"
        png = idir / Path(key).name
        same = s["weekday"] == s["weekend"]
        render_png(season_html(name, s), png, 250 if same else 450, a.chrome)
        (idir / (png.stem + ".html")).write_text(season_html(name, s))
        urls[name] = f"{host}/{key}"
        desc = " | ".join(f"{hour_txt(b)}-{hour_txt(e)} {lab}" for b, e, lab in s["weekday"])
        log(f"IMAGE {name} ({month_span(s['months'])}): {desc} -> {png.name} ({png.stat().st_size} B)")
        if a.dry_run:
            continue
        if http_status(urls[name]) == 200 and not a.reupload_images:
            log(f"  already served: {urls[name]}")
            continue
        s3 = s3 or mc.aws(a.aws_profile).client("s3")
        try:
            s3.upload_file(str(png), bucket, key, ExtraArgs={"ContentType": "image/png"})
        except Exception as exc:  # noqa: BLE001
            raise SetupError(f"upload s3://{bucket}/{key} failed ({exc}); host the PNGs in {idir} yourself and "
                             f"pass --image-url {name}=<url>") from exc
        log(f"  uploaded s3://{bucket}/{key}")
    return urls


# ---------------------------------------------------------------------------
# post + verify
# ---------------------------------------------------------------------------
def live_plans() -> list[dict]:
    return payload(api("GET", f"v3.0/rates/configuration/utilityId/{mc.PILOT_ID}", allow=(404,))) or []


def post_plan(a: argparse.Namespace, body: dict, out: Path) -> None:
    plans = live_plans()
    plans = plans if isinstance(plans, list) else []
    same_num = next((p for p in plans if p.get("planNumber") == a.plan_number), None)
    same_name = next((p for p in plans if p.get("planName") == a.plan_name), None)
    if same_name and same_name.get("planNumber") != a.plan_number:
        raise SetupError(f"plan name {a.plan_name} already belongs to plan {same_name['planNumber']}; "
                         f"pass --plan-number {same_name['planNumber']} or another --plan-name")
    if same_num:
        if same_num.get("planName") != a.plan_name:
            raise SetupError(f"plan {a.plan_number} exists as {same_num.get('planName')!r}: refusing to overwrite a "
                             "different plan (pick a free --plan-number)")
        bk = HERE.parent / "backups" / f"rate_plan_{mc.PILOT_ID}_{a.plan_number}_before_{datetime.now():%Y%m%d_%H%M%S}.json"
        bk.parent.mkdir(exist_ok=True)
        bk.write_text(json.dumps(same_num, indent=1))
        log(f"plan {a.plan_number} {a.plan_name} exists: replacing its rates (backup {bk.name})")
    # 1) Rates Configuration API: the plan row (eUtilityPlans: name + description -- what METERENROLL resolves).
    #    The deployed <deployment> build rejects component `attributes` (PE-14194 DTO change not deployed), and MDS
    #    pricing skips rates without attributes.data_stream (AbstractRateProvider.filterRatesByAttributes), so the
    #    rows it writes are replaced in step 2.
    head = {**body, "components": [{**{k: v for k, v in c.items() if k in CONFIG_COMPONENT_KEYS},
                                    "rates": [{k: v for k, v in r.items() if k in CONFIG_RATE_KEYS} for r in c["rates"]]}
                                   for c in body["components"]]}
    r = api("POST", f"v3.0/rates/configuration/utilityId/{mc.PILOT_ID}", [head], allow=(400, 404, 409, 500))
    (out / "post_config_response.json").write_text(json.dumps(r, indent=1))
    if status_of(r) != 200 or (isinstance(r, dict) and r.get("error")):
        raise SetupError(f"Rates Configuration POST -> HTTP {status_of(r)}: {json.dumps(r)[:500]}")
    log(f"POSTED plan {a.plan_number} {a.plan_name} via Rates Configuration API (eUtilityPlans row)")
    # 2) Rates API: DELETE the plan's eRates (eUtilityPlans survives) and POST the full rows WITH attributes and
    #    groupName. addRates is a plain INSERT, so the DELETE first is what keeps a rerun from duplicating rows.
    d = api("DELETE", f"v3.0/rates/utilities/{mc.PILOT_ID}/plans/{a.plan_number}", allow=(207, 400, 404, 500))
    log(f"DELETE eRates plan {a.plan_number} -> HTTP {status_of(d)}")
    rows = legacy_rows(a, body)
    (out / "post_rates_body.json").write_text(json.dumps(rows, indent=1))
    r = api("POST", "v3.0/rates", rows, allow=(400, 404, 409, 500))
    (out / "post_rates_response.json").write_text(json.dumps(r, indent=1))
    err = (payload(r) if isinstance(r, dict) else None) or (r.get("error") if isinstance(r, dict) else None)
    if status_of(r) != 200 or (isinstance(r, dict) and r.get("error")) or (isinstance(err, dict) and err):
        raise SetupError(f"POST /v3.0/rates -> HTTP {status_of(r)}: {json.dumps(r)[:600]}")
    log(f"POSTED {len(rows)} rates via /v3.0/rates (attributes.data_stream, groupName, holiday twins)")
    # 3) rateStructureImageUrl: no deployed API writes it (RateInfo has no field; the config DTO path replaces the rows)
    set_images_sql(a, body)
    rr = {"planName": a.plan_name, "description": body["planDescription"]}
    r2 = api("POST", f"v3.0/rate-info/utilities/{mc.PILOT_ID}/{a.plan_number}/rate-resources", rr, allow=(400, 404, 500))
    log(f"rate-resources upsert -> HTTP {status_of(r2)} {json.dumps(payload(r2))[:80]}")


def legacy_rows(a: argparse.Namespace, body: dict) -> list[dict]:
    """RateInfo rows for POST /v3.0/rates (epoch validity, per-rate attributes/rateBandName)."""
    out = []
    for c in body["components"]:
        for r in c["rates"]:
            src_lo, src_hi = r["_validLowTs"], r["_validHighTs"]
            out.append({"utilityId": mc.PILOT_ID, "planNumber": a.plan_number,
                        "monthLow": r["monthLow"], "dayLow": r["dayLow"], "monthHigh": r["monthHigh"], "dayHigh": r["dayHigh"],
                        "consumptionLow": r["consumptionLow"], "consumptionHigh": r["consumptionHigh"],
                        "timeOfDayLow": r["timeOfDayLow"], "timeOfDayHigh": r["timeOfDayHigh"],
                        "weekLow": r["weekLow"], "weekHigh": r["weekHigh"], "validLow": src_lo, "validHigh": src_hi,
                        "rate": r["rate"], "chargeType": r["chargeType"], "touName": r.get("touName"),
                        "tierName": r.get("tierName"), "groupName": r.get("groupName"), "isHoliday": r["isHoliday"],
                        "rateBandId": str(c["rateBandId"]), "rateBandName": c["rateBandName"],
                        "description": f"{c['rateBandName']} {r.get('groupName') or ''} {r.get('touName') or ''}".strip(),
                        "attributes": c["attributes"]})
    return out


def set_images_sql(a: argparse.Namespace, body: dict) -> None:
    images = {}
    for c in body["components"]:
        for r in c["rates"]:
            if r.get("rateStructureImageUrl"):
                images[r["groupName"]] = r["rateStructureImageUrl"]
    py, helper = DBQ_PY, HERE / "host" / "erates_image_update.py"
    if not images:
        log("no image URLs to set", "WARN")
        return
    stmt = (f"UPDATE eRates SET rateStructureImageUrl=<url> WHERE utilityId={mc.PILOT_ID} AND planNumber={a.plan_number} "
            f"AND groupName=<season> AND touName IS NOT NULL -- {images}")
    if a.no_image_sql:
        log(f"rateStructureImageUrl NOT set (--no-image-sql): TOU emails drop until it is. SQL: {stmt}", "WARN")
        return
    # a direct DB write: it passes the run's write policy like every other write (no kit authorization grants SQL, so a
    # kit-launched or standalone run is refused here and journalled; a human runs the statement)
    try:
        mds_target.check_write("sql", op="erates_image_update", schema=mc.DB_SCHEMA, utility=mc.PILOT_ID,
                               plan=a.plan_number, table="eRates")
    except mds_target.WriteRefused as exc:
        raise SetupError(f"{exc}. rateStructureImageUrl NOT set; SQL to run by hand: {stmt}") from exc
    if not py.exists():
        log(f"rateStructureImageUrl NOT set (no pymysql venv): TOU emails drop until it is. SQL: {stmt}", "WARN")
        return
    job = {"schema": mc.DB_SCHEMA, "utilityId": mc.PILOT_ID, "planNumber": a.plan_number, "images": images}
    p = subprocess.run([str(py), str(helper)], input=json.dumps(job), capture_output=True, text=True, timeout=240)
    if p.returncode:
        raise SetupError(f"image UPDATE failed: {(p.stderr or p.stdout)[-300:]}")
    log(f"SQL rateStructureImageUrl set (scoped to utility {mc.PILOT_ID} plan {a.plan_number} TOU rows): {p.stdout.strip()}")


def dbq(sql: str) -> list[dict] | None:
    py, script = DBQ_PY, DBQ / "scripts/dbq.py"
    if not (py.exists() and script.exists()):
        return None
    p = subprocess.run([str(py), str(script), "--db", str(mc.DB_SCHEMA), "--max-rows", "500", "--max-chars", "400", sql],
                       capture_output=True, text=True, timeout=240, env=mc.kit_env())
    # the last line is "(N row(s))"; a single-column result has no tab, so do not use tabs to detect success
    lines = [x for x in p.stdout.splitlines() if x.strip() and not x.startswith("(")]
    if p.returncode or not lines or "Traceback" in p.stdout + p.stderr:
        log(f"dbq unavailable ({(p.stderr or p.stdout)[-200:].strip()})", "WARN")
        return None
    head = lines[0].split("\t")
    return [dict(zip(head, ln.split("\t"))) for ln in lines[1:]]


def verify(a: argparse.Namespace, body: dict | None, out: Path) -> list[tuple[str, str, str, str]]:
    checks: list[tuple[str, str, str, str]] = []

    def add(cid: str, name: str, ok: bool | None, detail: str) -> None:
        st = "PASS" if ok else ("WARN" if ok is None else "FAIL")
        checks.append((cid, name, st, detail))
        log(f"  {cid} {st:4} {name}: {detail}", "ERROR" if st == "FAIL" else "INFO")

    want = sum(len(c["rates"]) for c in body["components"]) if body else None
    plan = next((p for p in live_plans() if p.get("planNumber") == a.plan_number), None)
    add("R01", "plan exists (config API = live MySQL)", bool(plan and plan.get("planName") == a.plan_name),
        f"plan {a.plan_number} name {plan.get('planName') if plan else None!r}")
    live = [r for c in (plan or {}).get("components", []) for r in c.get("rates", [])]
    add("R02", "row count", (len(live) == want) if want else bool(live), f"{len(live)} rows (payload {want})")
    tou = sorted({r.get("touName") for r in live if r.get("touName")})
    add("R03", "TOU periods", set(tou) >= {"On-Peak", "Off-Peak"}, f"{tou}")
    today = date.today().isoformat()
    cur = [r for r in live if r.get("touName") and str(r.get("validLow")) <= today < str(r.get("validHigh"))]
    add("R04", "TOU rows valid today (checkForTOURatePlan)", bool(cur), f"{len(cur)} rows valid on {today}")
    rows = dbq(f"SELECT erateId, groupName, touName, isHoliday, chargeType, rateBandId, rate, attributes, "
               f"rateStructureImageUrl FROM eRates WHERE utilityId={mc.PILOT_ID} AND planNumber={a.plan_number}")
    if rows is None:
        add("R05", "DB rows (groupName, attributes, image)", None, "dbq not available: skipped")
    else:
        (out / "db_rows.json").write_text(json.dumps(rows, indent=1))
        no_grp = [x["erateId"] for x in rows if x.get("chargeType") == "CONSUMPTION_BASED" and x.get("groupName") in ("NULL", "", None)]
        no_img = [x["erateId"] for x in rows if x.get("touName") not in ("NULL", "", None)
                  and x.get("rateStructureImageUrl") in ("NULL", "", None)]
        no_attr = [x["erateId"] for x in rows if "data_stream" not in (x.get("attributes") or "")]
        hol = sum(1 for x in rows if x.get("isHoliday") == "1")
        add("R05", "DB rows (groupName, attributes, image)", bool(rows) and not (no_grp or no_img or no_attr),
            f"{len(rows)} rows, holiday {hol}, missing groupName {len(no_grp)}, data_stream {len(no_attr)}, "
            f"image {len(no_img)} (image blank = TOU emails drop: PE-14194 not deployed)")
    imgs = sorted({r.get("rateStructureImageUrl") for c in (body or {}).get("components", []) for r in c["rates"]
                   if r.get("rateStructureImageUrl")})
    bad = [u for u in imgs if http_status(u) != 200]
    add("R06", "rate structure images served", bool(imgs) and not bad, f"{len(imgs)} url(s), not 200: {bad}")
    rr = payload(api("GET", f"v3.0/rate-info/utilities/{mc.PILOT_ID}/{a.plan_number}/rate-resources", allow=(404, 500))) or {}
    add("R07", "rate-resources planName", rr.get("planName") == a.plan_name,
        f"planNumber {rr.get('planNumber')} planName {rr.get('planName')!r}")
    st = api("GET", f"v3.0/rates/utilities/{mc.PILOT_ID}/plans/{a.plan_number}/structure", allow=(404, 500))
    n = len(payload(st) or []) if status_of(st) == 200 else 0
    add("R08", "/structure (JVM-cached pricing view)", True if n == (want or n) and n else None,
        f"HTTP {status_of(st)} {n} rows" + ("" if n else " -- SQLRatesDao cache on this data-server pod predates the "
                                            "plan; pricing JVMs refresh on restart/host churn (E11 in the user run proves it)"))
    (out / "verify.json").write_text(json.dumps([dict(zip(("id", "name", "status", "detail"), c)) for c in checks], indent=1))
    return checks


def report(out: Path, a: argparse.Namespace, snap: dict | None, body: dict | None, urls: dict[str, str],
           checks: list[tuple[str, str, str, str]]) -> None:
    lines = [f"# Rate plan {a.plan_number} {a.plan_name} on pilot {mc.PILOT_ID}", ""]
    if snap:
        lines += [f"source: NVE UAT {snap['source']} plan {snap['planNumber']} {snap['planName']} "
                  f"(fetched {snap['fetchedAt']})", ""]
        for name, s in seasons(target_rows(snap["rows"], a.feed_in_rate)).items():
            lines.append(f"- **{name}** {month_span(s['months'])}: " + ", ".join(
                f"{hour_txt(b)}-{hour_txt(e)} {lab} ${s['rates'].get(lab, 0):.5f}" for b, e, lab in s["weekday"])
                + ("" if s["weekday"] == s["weekend"] else " (weekends differ)") + (f" -- image {urls[name]}" if name in urls else ""))
        lines.append("")
    if body:
        lines.append(f"payload: {sum(len(c['rates']) for c in body['components'])} rates, components " + ", ".join(
            f"band {c['rateBandId']} {c['attributes']['data_stream']}{' holiday' if c['holidaysEnabled'] else ''} "
            f"({len(c['rates'])})" for c in body["components"]))
        lines.append("")
    lines += ["| id | check | status | detail |", "|---|---|---|---|"]
    lines += [f"| {c[0]} | {c[1]} | {c[2]} | {c[3].replace('|', '/')} |" for c in checks]
    (out / "rate_plan_report.md").write_text("\n".join(lines) + "\n")
    log(f"report {out / 'rate_plan_report.md'}")


def main(argv: list[str] | None = None) -> int:
    argv, source_profile = take_source_profile(argv)
    if not source_profile:                      # before the profile, any token or any network call
        print(BLOCKED_SOURCE, file=sys.stderr)
        return 2
    argv = mds_target.bootstrap(argv)          # PROFILE_REQUIRED before anything touches a target
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--phases", default="all", help=f"comma list of {','.join(PHASES)} or all")
    p.add_argument("--source-plan", type=int, required=True, help="plan number on the --source-profile pilot")
    p.add_argument("--refresh-source", action="store_true",
                   help="re-read the source (the --source-profile's api.token_file) and rewrite the fixture")
    p.add_argument("--nve-token-file", help="source token file (default: the --source-profile's api.token_file)")
    p.add_argument("--plan-number", type=int, required=True, help="target plan number (no default)")
    p.add_argument("--plan-name", required=True, help="target plan name (no default)")
    p.add_argument("--feed-in-rate", type=float, required=True,
                   help="flat RECEIVED feed-in band, $/kWh (no default; 0 is a valid explicit choice)")
    p.add_argument("--chrome", help="headless Chrome/Chromium binary that renders the season images (no default)")
    p.add_argument("--image-bucket", help="S3 bucket for the images (default: the profile's email.provisioner.target.image_bucket)")
    p.add_argument("--image-host", help="public host serving that bucket (default: email.provisioner.target.image_host)")
    p.add_argument("--image-url", action="append", default=[], metavar="SEASON=URL", help="use a hosted image instead")
    p.add_argument("--reupload-images", action="store_true")
    p.add_argument("--no-image-sql", action="store_true",
                   help="do not set eRates.rateStructureImageUrl (the one SQL UPDATE; prints the statement instead)")
    p.add_argument("--dry-run", action="store_true", help="build the payload and images; no S3/API writes")
    p.add_argument("--token-file")
    p.add_argument("--aws-profile")
    p.add_argument("--output-dir", default=str(HERE / "results" / "rate_plans"))
    a = p.parse_args(argv)
    try:
        use_source(load_source(source_profile))
    except Exception as exc:  # noqa: BLE001 - a bad source profile stops before any network call
        print(f"{BLOCKED_SOURCE}: {getattr(exc, 'message', exc)}", file=sys.stderr)
        return 2
    phases = list(PHASES) if a.phases == "all" else [x.strip() for x in a.phases.split(",") if x.strip()]
    if ("images" in phases or "post" in phases) and not a.image_url and not all(image_settings(a)[:2]):
        print(BLOCKED_IMAGES, file=sys.stderr)  # before any token, source read, render or network call
        return 2
    out = Path(a.output_dir) / f"{a.plan_name}_{datetime.now():%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    mc._LOG_FH = open(out / "run.log", "a")
    snap, body, urls, checks = None, None, {}, []
    try:
        mc._TOKEN = mc.get_token(a.token_file)
        if {"source", "build", "images", "post"} & set(phases) or "verify" in phases:
            snap = fetch_source(a)
        if "images" in phases or "post" in phases:
            urls = images(a, snap, out)
        if {"build", "post"} & set(phases) or "verify" in phases:
            body = build_payload(a, snap, urls if urls else {})
            (out / "payload.json").write_text(json.dumps([body], indent=1))
            log(f"BUILD payload {out / 'payload.json'}")
        if "post" in phases and not a.dry_run:
            post_plan(a, body, out)
        if "verify" in phases and not a.dry_run:
            if "post" in phases:
                time.sleep(5)
            checks = verify(a, body if urls else None, out)
        report(out, a, snap, body, urls, checks)
        worst = "FAIL" if any(c[2] == "FAIL" for c in checks) else "PASS"
        log(f"RATE PLAN {a.plan_number} {a.plan_name}: {worst} " + " ".join(f"{c[0]}={c[2]}" for c in checks))
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
