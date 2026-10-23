#!/usr/bin/env python3
"""Shared engine for the two MDS NON-SOLAR user scripts on the target pilot:

    MDS_NON_SOLAR/create_mds_single_fuel_user.py   ELECTRIC MDS non-solar (DELIVERED + all-zero RECEIVED)
    MDS_NON_SOLAR/create_mds_dual_fuel_user.py     ELECTRIC MDS non-solar + GAS (AMI)

NON-SOLAR (copy of MDS_AUTOMATION/mds_common.py; the shape the Hawk masterpilot-02 MDSNonSolar suite proved on the
target pilot): SOLAR_USER FALSE on USERENROLL (35), METERENROLL (11) and the transition rows (the value Hawk's
MasterPilot02UserCreator writes); no rooftop solar (--solar-kw 0), so RAW_R is all 0.000 and the invoice FEED_IN line is
0 kWh / $0 while the RECEIVED stream still exists; no solar user-scope configs; checks expect home.solarUser and
GLOBAL.SOLAR NOT true, no solar itemization row (id 16) and no SOLAR_ENERGY_FLOW block; the Monthly Summary needs
MONTHLY_SUMMARY in meta_data.supported_communication_events. Notes below that describe solar homes are the solar
engine's history.

Each run builds brand-new launchpad files at runtime, with unique ids and file names
and data anchored to today. It uploads them in dependency order and waits on API
state after each file. It then drives disagg (hybrid V2 for electric, V1 for gas),
aggregation and the Monthly Summary email, and validates every stage through the APIs.
Nothing is read from MDS<deployment>Files/.

Everything below was verified live on the target pilot on 2026-09-28 (user 431c23bf-...):

FILES
  * RAW is written in the pilot's raw_data_consumption_unit_map unit (ELECTRIC=Wh,
    GAS=ccf). A kWh value in a Wh pilot is priced 1000x too low.
  * A RAW timestamp marks the interval END: the ingester stores label - 900 s.
  * Labels are local yyyyMMddHHmmss (raw_data_date_type=DATE, no offset). Java resolves the
    repeated fall-back hour to standard time, so the FIRST fall-back hour (4 intervals a year)
    cannot be written; it is recorded in the manifest and E06 reports coverage as incomplete.
  * Invoice and BC_SCHEDULE end dates are EXCLUSIVE (isEndDateInclusive=false,
    start = end - BC_DURATION). Send 2026-08-01|2026-09-01|31 for August.
  * ONE base METERENROLL row per fuel. MDS creates the DELIVERED and -R meters itself.
  * Dual fuel: GAS rows go first so the ELECTRIC row sets home.planNumber last. Both
    fuels share identical invoice periods (ERROR_CONFLICTING_INVOICE_DATA otherwise).
    Gas meters exist only if supported_streams has a GAS entry (silent otherwise).
ITEMIZATION
  * Electric uses hybrid V2 (disagg_preference.enable_hybrid_v2 plus the
    v2/<deployment>/US_the target pilot_AMI_electric model). Gas uses V1 (gas_hybrid_disagg_enabled
    plus the <deployment>/US_the target pilot_AMI_gas model).
  * Hybrid runs only on an aggregation with sendNotifications=true
    (ApplianceAggregateHandler:271 runHybridInsightReco = sendNotifierEmails).
  * Every DISAGG/AGG wait needs a hybrid completion (hybridSucessRunTimeStamp) NEWER than the
    trigger; pre-existing itemization cannot satisfy it. No fresh completion -> one resend.
    Readable-or-not, the outcome is stored as manifest.triggerEvidence and reported by E13.
  * The electric array includes id 17 "total". Presentation rule (totalFromInvoice=true, so
    this is NOT disagg accuracy): rows 16 (solar) and 17 exist, the other appliances sum to the
    total, the total equals the invoice energy (CONSUMPTION_BASED + FEED_IN) to the cent, and
    total usage = net kWh + solar estimate. The widget and the email are ID/label-matched to it.
MONTHLY SUMMARY
  * Needs meta_data.supported_solar_communication_events at PILOT scope; it is read
    at GenericEventDataEmailContentBuilder:194 via getMetaDataConfig(pilotId).
    Without it the emailer NPEs and nothing is recorded.
  * Triggered with the JSON body {"userId","homeOrdinal","metaData":{"EventName"}}
    on NotificationsProcessorEvent. It is verified through
    /2.1/utility_notifications, which holds the exact HTML that was sent.
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import math
import os
import random
import re
import secrets
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid as uuid_lib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# The TARGET (environment, deployment, pilot, endpoints, AWS identity, buckets, queues, schema, rate plans) is NOT set
# here: mds_target.bootstrap() loads ONE validated debug-kit profile (--profile or BIDGELY_PROFILE) and calls
# _retarget() below. Until then every target value is None and any use fails (no silent environment).
import mds_target  # noqa: E402

TARGET_ENVIRONMENT = TARGET_DEPLOYMENT = None
BASE_URL = DASHBOARD_URL = None
PILOT_ID = None
HID = 1
# the debug skill (envlogs, dbq, profile loading)
SKILL_DIR = mds_target.SKILL_DIR
AWS_PROFILE = AWS_REGION = AWS_ACCOUNT = None
DEFAULT_BUCKET = MARKER_BUCKET = MARKER_PREFIX = MODEL_BUCKET = None
MODEL_KEYS: dict = {}
AGG_QUEUE = DISAGG_QUEUE = NOTIF_QUEUE = ATTR_QUEUE = None        # ATTR = user-attributes-handler (DCX GLOBAL.* attributes)
TOKEN_FILE = DB_SCHEMA = None
DEFAULT_RATE_PLAN = DEFAULT_GAS_RATE_PLAN = TOU_RATE_PLAN = None
DEFAULT_BILL_CYCLE_CODE = DEFAULT_TZ = None
UTILITY_NAME = KEYSPACE = DW_BUCKET = SHC_TEMPLATE_PILOT = PROFILE_BUCKET = PDF_BUCKET = None
ETL_JAVA_ENV = ETL_QUEUE_SUFFIX = DEFAULT_RECIPIENT = None
LINK_HOSTS: tuple = ()
_KIT_ENV: dict[str, str] | None = None


def _retarget(v: dict) -> None:
    """Set every target value from the validated view (emailkit.contract.provisioner_view)."""
    g = globals()
    g.update(TARGET_ENVIRONMENT=v["environment"], TARGET_DEPLOYMENT=v["deployment"], BASE_URL=v["api_url"].rstrip("/"),
             DASHBOARD_URL=(v.get("dashboard_url") or "").rstrip("/") or None, PILOT_ID=int(v["pilot"]),
             AWS_PROFILE=v["aws_profile"], AWS_REGION=v["aws_region"], AWS_ACCOUNT=v.get("aws_account"),
             DEFAULT_BUCKET=v["launchpad_bucket"], MARKER_BUCKET=v["marker_bucket"], MARKER_PREFIX=v["marker_prefix"],
             MODEL_BUCKET=v["model_bucket"], MODEL_KEYS=dict(v["model_keys"] or {}),
             AGG_QUEUE=v["queues"]["aggregation"], DISAGG_QUEUE=v["queues"]["disagg"], NOTIF_QUEUE=v["queues"]["notifications"],
             ATTR_QUEUE=v["queues"]["user_attributes"], TOKEN_FILE=v["token_file"], DB_SCHEMA=v["db_schema"],
             DEFAULT_RATE_PLAN=v["rate_plans"]["flat"], DEFAULT_GAS_RATE_PLAN=v["rate_plans"].get("gas") or v["rate_plans"]["flat"],
             TOU_RATE_PLAN=v["rate_plans"].get("tou"), DEFAULT_BILL_CYCLE_CODE=str(v["bill_cycle_code"]), DEFAULT_TZ=v["time_zone"],
             UTILITY_NAME=v.get("utility_name"), KEYSPACE=v.get("keyspace"), DW_BUCKET=v.get("dw_bucket"),
             SHC_TEMPLATE_PILOT=v.get("shc_template_pilot"), PROFILE_BUCKET=v.get("profile_bucket"), PDF_BUCKET=v.get("pdf_bucket"),
             ETL_JAVA_ENV=v.get("etl_java_env"), ETL_QUEUE_SUFFIX=v.get("etl_queue_suffix"),
             DEFAULT_RECIPIENT=v.get("default_recipient"), LINK_HOSTS=tuple(v.get("link_hosts") or ()))
    globals()["_KIT_ENV"] = None


def need(name: str):
    """A target value an operation needs; None means the profile does not state it (fail closed)."""
    val = globals().get(name)
    if val in (None, "", {}, ()):
        raise SetupError(f"{name} is not set: the profile does not state it (email.provisioner.target / email.rate_plans)")
    return val


def kit_env() -> dict[str, str]:
    """Environment for the kit's scripts (envlogs, dbq.py): the SAME profile this run resolved, plus the values dbq.py
    reads directly. No fallback profile: without a resolved target this raises PROFILE_REQUIRED."""
    global _KIT_ENV
    if _KIT_ENV is None:
        mds_target.require()
        import shlex
        import subprocess
        env = dict(os.environ, BIDGELY_PROFILE=mds_target.PROFILE_PATH)
        p = subprocess.run([sys.executable, "-m", "emailkit.envshell", mds_target.PROFILE_PATH], capture_output=True, text=True,
                           env=dict(env, PYTHONPATH=str(SKILL_DIR / "scripts"), PYTHONDONTWRITEBYTECODE="1"), timeout=60)
        vals, want = {}, {"P_DB_CREDS_FILE", "P_ENV", "P_DEPLOY"}      # single-line values only (P_SERVICES spans lines)
        for line in p.stdout.splitlines():
            k, _, v = line.partition("=")
            if k in want:
                vals[k] = (shlex.split(v) or [""])[0]
        if p.returncode or not vals:
            raise SetupError(f"profile {mds_target.PROFILE_PATH} unusable for the kit's tools: {(p.stderr or '').strip()[-200:]}")
        for k, kv in (("DB_CREDS_FILE", "P_DB_CREDS_FILE"), ("ENV", "P_ENV"), ("DEPLOY", "P_DEPLOY")):
            if vals.get(kv):
                env[k] = vals[kv]
        _KIT_ENV = env
    return dict(_KIT_ENV)
INTERVAL = 900
RECEIVED_SUFFIX = "-R"
CCF_TO_WH = 1.037 * 29.3001111 * 1000        # UnitTypeConstants CCF_TO_THERMS * THERMS_TO_KWH
PHASES = ("preflight", "generate", "upload", "configure", "aggregate", "transition", "notify", "validate", "dashboard")

DOLLAR_TOL = 1.00
BILL_ANALYSIS_WAIT = 1800      # seconds to wait for HDD/CDD weather on a fresh user
REL_TOL = 0.01

# log locations come from the profile through envlogs (no host IPs here)


SOLAR_FALSE = "FALSE"           # SOLAR_USER of a non-solar enrollment row (Hawk MasterPilot02UserCreator value)
SOLAR_SECTION = "SOLAR_ENERGY_FLOW"


def user_configs(fuels: tuple[str, ...]) -> dict[str, dict[str, str]]:
    """USER-scope configs written on the new NON-SOLAR user only (no solar keys: no enable_solar,
    trigger_ami_disagg_for_solar_user, disagg_solar_estimation_data, show_cost_break_down_for_solar_users or
    solar_display_value_identifier -- a non-solar home never reads them)."""
    hm = {"electric_hybrid_disagg_enabled": "true",
          "csv_filename_format": "Home.country-User.pilotId-UserType.meterType"}
    if "GAS" in fuels:
        hm["gas_hybrid_disagg_enabled"] = "true"
    out = {"disagg_preference": {"enable_hybrid_v2": "true"},
           "hybrid_moderation": hm,
           # Monthly Summary in $ (non-solar homes use the plain display_value_identifier)
           "email_itemization_section_configs": {"display_value_identifier": "cost"}}
    if "GAS" in fuels:
        # empty meta_data.gas_unit -> UnitType.fromId("") -> kW -> every gas itemization / MS widget call
        # fails with "Units kWh and kW can't be converted". Read at USER scope (HybridApplianceDataService:620).
        out["meta_data"] = {"gas_unit": "com.bidgely.cloud.unitType.ccf"}
    return out


class SetupError(RuntimeError):
    pass


class WaitTimeout(SetupError):
    pass


# ---------------------------------------------------------------------------
# Logging / run state
# ---------------------------------------------------------------------------
_SECRETS: list[str] = []
_LOG_FH = None
_UUID_TAG = ""


def log(msg: str, level: str = "INFO") -> None:
    for s in _SECRETS:
        msg = msg.replace(s, "***")
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {level:<5} {_UUID_TAG}{msg}"
    print(line, flush=True)
    if _LOG_FH:
        _LOG_FH.write(line + "\n")
        _LOG_FH.flush()


def set_uuid_tag(uuid: str) -> None:
    global _UUID_TAG
    _UUID_TAG = f"[uuid={uuid}] "


def step(title: str) -> None:
    log("=" * 78)
    log(title)
    log("=" * 78)


@dataclass
class Run:
    out: Path
    m: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.m.get("uuid"):
            mds_target.pin_user(self.m["uuid"])

    def save(self) -> None:
        if self.m.get("uuid"):
            mds_target.pin_user(self.m["uuid"])
        # identity for the kit: the contract this run executed under and the kit's per-invocation token
        for key, env in (("kitContract", "BIDGELY_CONTRACT_SHA256"), ("kitRunToken", "BIDGELY_RUN_TOKEN")):
            if os.environ.get(env):
                self.m[key] = os.environ[env]
        self.m["target"] = {"environment": TARGET_ENVIRONMENT, "deployment": TARGET_DEPLOYMENT, "pilot": PILOT_ID}
        (self.out / "manifest.json").write_text(json.dumps(self.m, indent=2, default=str))

    def dir(self, name: str) -> Path:
        d = self.out / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def evidence(self, name: str, data: Any) -> None:
        d = self.dir("api_evidence")
        (d / f"{name}.json").write_text(json.dumps(data, indent=2, default=str))

    def mutate(self, kind: str, **kw: Any) -> None:
        self.m.setdefault("mutations", []).append({"kind": kind, "at": datetime.now().isoformat(), **kw})
        self.save()


# ---------------------------------------------------------------------------
# HTTP + configs
# ---------------------------------------------------------------------------
_TOKEN = ""


def get_token(token_file: str | None) -> str:
    if token_file:
        p = Path(token_file).expanduser()
        if p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise SetupError(f"{p} must be mode 0600 (chmod 600 {p})")
        tok = p.read_text().strip()
    else:
        tok = os.environ.get("BIDGELY_TOKEN") or ""
        if not tok and TOKEN_FILE:   # the profile's api.token_file (envlogs apitoken writes it)
            f = Path(TOKEN_FILE).expanduser()
            if f.exists() and not f.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                tok = f.read_text().strip()
    if not tok:
        raise SetupError("no token: export BIDGELY_TOKEN=... or pass --token-file")
    _SECRETS.append(tok)
    return tok


def api(method: str, path: str, body: Any = None, *, allow: tuple[int, ...] = (), timeout: int = 180) -> Any:
    url = f"{need('BASE_URL')}/{path.lstrip('/')}"
    if method != "GET":
        try:
            mds_target.check_write("api", method=method, path=path, body=body)
        except mds_target.WriteRefused as exc:
            raise SetupError(str(exc)) from exc
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {_TOKEN}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(1, 6):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode() or "null"
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw
        except urllib.error.HTTPError as exc:
            text = exc.read().decode(errors="replace")
            if exc.code in allow:
                try:
                    j = json.loads(text)
                except json.JSONDecodeError:
                    j = {"raw": text}
                return {"__status__": exc.code, **(j if isinstance(j, dict) else {"raw": j})}
            if method == "GET" and exc.code in (502, 503, 504) and attempt < 5:
                time.sleep(15 * attempt)
                continue
            raise SetupError(f"{method} {path} -> HTTP {exc.code}: {text[:400]}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if method == "GET" and attempt < 5:
                time.sleep(15 * attempt)
                continue
            raise SetupError(f"{method} {path} -> {exc}") from exc
    raise SetupError(f"{method} {path}: retries exhausted")


def payload(resp: Any) -> Any:
    if isinstance(resp, dict) and "payload" in resp and ("requestId" in resp or "error" in resp):
        return resp["payload"]
    return resp


def status_of(resp: Any) -> int:
    return resp.get("__status__", 200) if isinstance(resp, dict) else 200


def read_config(entity: str, eid: Any, ctype: str) -> dict[str, dict[str, Any]]:
    res = payload(api("GET", f"entities/{entity}/{eid}/configs/{ctype}", allow=(404,)))
    raw = res.get(ctype) if isinstance(res, dict) else None
    if raw in (None, "", "null"):
        return {}
    try:
        return {kv["key"]: kv for kv in json.loads(raw).get("kvs") or []}
    except (TypeError, json.JSONDecodeError):
        return {}


def cfg(c: dict[str, dict[str, Any]], key: str, default: str = "") -> str:
    return str(c.get(key, {}).get("val", default))


def post_config(run: Run, entity_id: Any, ctype: str, values: dict[str, str], scope: str) -> None:
    kvs = [{"configKey": k, "configVal": v, "configDataType": "TEXT", "configRegex": "(?s).*",
            "configDocumentation": f"set by mds_common.py on {date.today()}"} for k, v in values.items()]
    api("POST", f"entities/{entity_id}/configs", {"configType": ctype, "configKVs": kvs})
    run.m["lastConfigWriteAt"] = time.time()
    run.mutate(f"{scope}_config", entity=str(entity_id), configType=ctype, values=values)
    log(f"CONFIG {scope} {entity_id} {ctype} <- {values}")


# ---------------------------------------------------------------------------
# Time, rates, profiles
# ---------------------------------------------------------------------------
def add_months(d: date, n: int) -> date:
    k = d.month - 1 + n
    return date(d.year + k // 12, k % 12 + 1, 1)


def midnight(d: date, tz: ZoneInfo) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=tz).timestamp())


STORED_CHUNK = 14 * 86400          # gb.json window per request in E06 (~1344 rows)
RAW_LABEL_FMT = "%Y%m%d%H%M%S"      # pilot raw_data_date_type=DATE, raw_data_date_format=yyyyMMddHHmmss


def platform_ts(label: str, tz: ZoneInfo) -> int:
    """The epoch the ingester assigns to a local RAW label. LaunchpadRawDataParser parses with
    SimpleDateFormat in parser_time_zone, and Java resolves the repeated fall-back hour to
    STANDARD time (the second occurrence). Verified with the JDK on 2026-09-28:
    20251102010000 America/Anchorage -> 1762077600 (01:00 AKST, not 01:00 AKDT).
    Python fold=1 selects the same instant."""
    return int(datetime.strptime(label, RAW_LABEL_FMT).replace(tzinfo=tz, fold=1).timestamp())


def raw_label(ts: int, tz: ZoneInfo) -> str | None:
    """RAW label (interval END) for the interval starting at `ts`, or None when the local DATE
    format cannot represent it: its end label resolves to a different instant (first
    fall-back hour). Writing such a row would store the reading one hour late."""
    label = datetime.fromtimestamp(ts + INTERVAL, tz).strftime(RAW_LABEL_FMT)
    return label if platform_ts(label, tz) == ts + INTERVAL else None


class UnsupportedRate(SetupError):
    pass


@dataclass
class RateBook:
    """Prices generated RAW so the invoice file matches what the rate engine will compute.
    Only structures it can model exactly are accepted: anything else raises instead of
    silently pricing at zero (a zero-cost invoice would then "match" nothing real)."""
    rows: list[dict[str, Any]]
    flat: dict[str, float] | None = None
    _cache: dict[tuple, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._bounds = sorted({int(r.get("validLow") or 0) for r in self.rows} |
                              {int(r.get("validHigh") or 2**31) for r in self.rows})
        by: dict[tuple, set[float]] = {}
        for r in self.rows:
            if r.get("chargeType") != "CONSUMPTION_BASED":
                continue
            sig = (self._stream(r), r.get("monthLow"), r.get("dayLow"), r.get("monthHigh"), r.get("dayHigh"),
                   r.get("timeOfDayLow"), r.get("timeOfDayHigh"), r.get("weekLow"), r.get("weekHigh"),
                   r.get("validLow"), r.get("validHigh"))
            by.setdefault(sig, set()).add(float(r["rate"]))
            if int(r.get("consumptionLow") or 0) > 0:
                raise UnsupportedRate(f"tiered rate row (consumptionLow={r.get('consumptionLow')}) on "
                                      f"{self._stream(r)}: tiers are not modelled by the generator")
        diff = {k[0] for k, v in by.items() if len(v) > 1}
        if diff:
            raise UnsupportedRate(f"holiday and non-holiday (or tier) rows differ for {sorted(diff)}: "
                                  "holiday calendars and tiers are not modelled by the generator")

    @staticmethod
    def _stream(r: dict) -> str:
        return (r.get("attributes") or {}).get("data_stream", r.get("rateBandName"))

    @staticmethod
    def _within(v: int, lo: Any, hi: Any) -> bool:
        if lo is None or hi is None:
            return True
        lo, hi = int(lo), int(hi)
        return lo <= v <= hi if lo <= hi else (v >= lo or v <= hi)

    def structure(self) -> str:
        """'flat' when each stream has one rate for every hour/day/season, else 'time-varying'."""
        if self.flat is not None:
            return "flat"
        rates: dict[str, set[float]] = {}
        for r in self.rows:
            if r.get("chargeType") == "CONSUMPTION_BASED":
                rates.setdefault(self._stream(r), set()).add(float(r["rate"]))
        return "flat" if all(len(v) == 1 for v in rates.values()) else "time-varying"

    def rate(self, stream: str, ts: int, local: datetime) -> float:
        if self.flat is not None:
            return self.flat[stream]
        # the engine's weekday: Calendar.DAY_OF_WEEK - 1 with Sunday -> 7, i.e. Monday=1 .. Sunday=7
        # (calc.RateProvider.getDayOfWeek); weekend rows are weekLow 6..7
        week = local.isoweekday()
        epoch = sum(1 for b in self._bounds if b <= ts)          # validity window is part of the key
        key = (stream, epoch, local.month, local.day, week, local.hour)
        if key not in self._cache:
            md, found, best = local.month * 100 + local.day, None, -1
            for r in self.rows:
                if self._stream(r) != stream:
                    continue
                if r.get("chargeType") != "CONSUMPTION_BASED" or r.get("isHoliday"):
                    continue
                if not (int(r.get("validLow") or 0) <= ts <= int(r.get("validHigh") or 2**31)):
                    continue
                if not self._within(md, int(r.get("monthLow", 1)) * 100 + int(r.get("dayLow", 1)),
                                    int(r.get("monthHigh", 12)) * 100 + int(r.get("dayHigh", 31))):
                    continue
                if not self._within(week, r.get("weekLow"), r.get("weekHigh")):
                    continue
                lo, hi = r.get("timeOfDayLow"), r.get("timeOfDayHigh")
                if lo is not None and hi is not None and int(lo) > int(hi):          # wrap-around window
                    if self._within(local.hour, lo, hi):
                        found = float(r["rate"])
                        break
                    continue
                # the engine resolves the hour by LOWER bound (calc.Node.findChildNode): the row with the largest
                # timeOfDayLow <= hour wins, so NVE-style half-open rows (0-8, 8-18, 18-21, 21-23) price 08:00 at
                # the 8-18 rate even though 0-8 also "contains" 8
                tl = int(lo or 0)
                if best < tl <= local.hour:
                    found, best = float(r["rate"]), tl
            if found is None:
                raise UnsupportedRate(f"no {stream} CONSUMPTION_BASED rate for {local:%Y-%m-%d %H:%M} "
                                      f"(ts {ts}): the plan does not cover this interval")
            self._cache[key] = found
        return self._cache[key]


def resolve_plan(plan_name: str) -> tuple[int, RateBook]:
    cfgs = payload(api("GET", f"v3.0/rates/configuration/utilityId/{PILOT_ID}")) or []
    num = next((p["planNumber"] for p in cfgs if str(p.get("planName")) == plan_name), None)
    if num is None:
        raise SetupError(f"rate plan {plan_name} not found for utility {PILOT_ID}")
    r = api("GET", f"v3.0/rates/utilities/{PILOT_ID}/plans/{num}/structure", allow=(404,))
    rows = payload(r) if status_of(r) == 200 else None
    if not rows:
        # /structure is served from SQLRatesDao's per-JVM cache (never invalidated): a plan created after the
        # data-server pod loaded the target pilot answers 404 there although it exists. Read the rows from MySQL instead.
        rows = plan_rows_from_db(num) or plan_rows_from_local_post(num)
        if not rows:
            raise SetupError(f"plan {plan_name} ({num}) exists in eUtilityPlans but its rows are not readable: /structure "
                             "404 (JVM cache) and no DB access / local post_rates_body.json")
        log(f"plan {plan_name} ({num}): /structure 404 (data-server rate cache predates the plan); {len(rows)} rows "
            "read from MySQL/local post instead -- pricing JVMs may be stale too (E11 will show)", "WARN")
    return int(num), RateBook(rows)


def plan_rows_from_db(num: int) -> list[dict] | None:
    import subprocess
    skill = SKILL_DIR
    script = skill / "scripts/dbq.py"
    # the debug-kit installer keeps the venv outside the skill dir; older installs kept it inside
    py = next((c for c in (Path.home() / ".local/share/bidgely-debug/venv/bin/python", skill / ".venv/bin/python")
               if c.exists()), None)
    if not (py and script.exists()):
        log("plan rows from MySQL unavailable: config-fail-testcase-debugger skill/venv not found (run ~/Documents/bidgely-debug-kit/install.sh)", "WARN")
        return None
    cols = ("erateId monthLow dayLow monthHigh dayHigh consumptionLow consumptionHigh timeOfDayLow timeOfDayHigh "
            "weekLow weekHigh validLow validHigh rate chargeType touName groupName isHoliday rateBandId rateBandName "
            "attributes").split()
    p = subprocess.run([str(py), str(script), "--db", need("DB_SCHEMA"), "--max-rows", "1000", "--max-chars",
                        "500", f"SELECT {', '.join(cols)} FROM eRates WHERE utilityId={PILOT_ID} AND planNumber={int(num)}"],
                       capture_output=True, text=True, timeout=240, env=kit_env())
    lines = [x for x in p.stdout.splitlines() if "\t" in x]
    if p.returncode or len(lines) < 2:
        return None
    head, out = lines[0].split("\t"), []
    ints = set(cols[1:13])
    for ln in lines[1:]:
        d = {k: (None if v == "NULL" else v) for k, v in zip(head, ln.split("\t"))}
        for k in ints:
            d[k] = int(d[k]) if d.get(k) is not None else None
        d["rate"] = float(d["rate"])
        d["isHoliday"] = d.get("isHoliday") in ("1", "true", "True")
        d["attributes"] = json.loads(d["attributes"]) if d.get("attributes") else {}
        out.append(d)
    return out


def plan_rows_from_local_post(num: int) -> list[dict] | None:
    posts = sorted((Path(__file__).resolve().parent / "results" / "rate_plans").glob("*/post_rates_body.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for p in posts:
        rows = json.loads(p.read_text())
        if rows and rows[0].get("planNumber") == int(num) and rows[0].get("utilityId") == PILOT_ID:
            return rows
    return None


class ElectricProfile:
    """House load and rooftop solar, Wh per 15 min. Deterministic per run seed."""

    def __init__(self, seed: int, solar_kw: float, base_kw: float, peak_kw: float = 0.0):
        self.rng, self.solar_kw, self.base_kw, self.cloud = random.Random(seed), solar_kw, base_kw, {}
        self.peak_kw = peak_kw

    def wh(self, local: datetime) -> tuple[float, float]:
        h = local.hour + local.minute / 60
        season = 0.5 + 0.5 * math.cos(2 * math.pi * (local.month - 7) / 12)
        kw = self.base_kw + 1.0 * math.exp(-((h - 7.5) ** 2) / 1.5) + 1.8 * math.exp(-((h - 19) ** 2) / 4)
        kw *= (0.85 + 0.5 * season) if 12 <= h <= 20 else (1.0 + 0.3 * (1 - season))
        if self.peak_kw:
            # --peak-kw: evening cooling/cooking/EV load 17:00-22:00, summer weighted, so a TOU home really
            # consumes in the 18-21 On-Peak window (TOU bill-saving reco needs peak share >= 10% and an appliance
            # saving >= $5; the solar tail ends ~21:00 in summer and would otherwise cover most of it)
            kw += self.peak_kw * (0.35 + 0.65 * season) * math.exp(-((h - 19.5) ** 2) / 2.2)
        kw *= self.rng.uniform(0.85, 1.15)
        d = local.date()
        if d not in self.cloud:
            self.cloud[d] = self.rng.uniform(0.35, 1.0)
        rise, sset = 7.0 - 1.5 * season, 19.0 + 2.0 * season
        sol = 0.0
        if rise < h < sset:
            sol = self.solar_kw * (0.35 + 0.65 * season) * self.cloud[d] * math.sin(math.pi * (h - rise) / (sset - rise))
        return kw * 250.0, max(sol, 0.0) * 250.0


class GasProfile:
    """Space + water heating, ccf per 15 min, heating-season weighted."""

    def __init__(self, seed: int, scale: float):
        self.rng, self.scale = random.Random(seed + 7), scale

    def ccf(self, local: datetime) -> float:
        h = local.hour + local.minute / 60
        cold = 0.5 + 0.5 * math.cos(2 * math.pi * (local.month - 1) / 12)     # 1 in January
        water = 0.004 * (1 + 1.5 * math.exp(-((h - 7) ** 2) / 1.2) + 1.2 * math.exp(-((h - 20) ** 2) / 2))
        heat = 0.03 * cold ** 1.5 * (0.6 + 0.4 * math.exp(-((h - 6) ** 2) / 4) + 0.4 * math.exp(-((h - 21) ** 2) / 5))
        return max((water + heat) * self.scale * self.rng.uniform(0.8, 1.2), 0.0)


# ---------------------------------------------------------------------------
# Identity + generation
# ---------------------------------------------------------------------------
def digits(n: int) -> str:
    return str(secrets.randbelow(9) + 1) + "".join(str(secrets.randbelow(10)) for _ in range(n - 1))


def _default_recipient(customer: str, fuels) -> str:
    if not DEFAULT_RECIPIENT:
        raise SetupError("no --email given and the profile states no email.provisioner.target.default_recipient")
    return DEFAULT_RECIPIENT.replace("{customer}", customer).replace("{df}", "DF" if "GAS" in fuels else "")


def new_identity(fuels: tuple[str, ...], email: str | None, tag: str,
                 zipcode: str = "11691", city: str = "Far Rockaway", state: str = "NY") -> dict[str, Any]:
    c, a, p = digits(10), digits(10), digits(10)
    meters = {f: digits(9) for f in fuels}
    while len(set(meters.values())) != len(meters):
        meters = {f: digits(9) for f in fuels}
    base = f"{PILOT_ID}:{c}_{a}_{p}"
    return {
        "runId": tag, "customer": c, "account": a, "premise": p, "meters": meters,
        "phones": {f: "907" + digits(7) for f in fuels},
        "email": email or _default_recipient(c, fuels),
        "firstName": "MDS", "lastName": f"AUTO{tag}", "address": f"{secrets.randbelow(900) + 100} Main Street",
        "city": city, "state": state, "zip": zipcode,
        "externalUserId": f"{PILOT_ID}_{c}", "gatewayToken": base,
        "tokens": {"ELECTRIC:DELIVERED": f"{base}_{meters['ELECTRIC']}_AMI",
                   "ELECTRIC:RECEIVED": f"{base}_{meters['ELECTRIC']}{RECEIVED_SUFFIX}_AMI",
                   **({"GAS:DELIVERED": f"{base}_{meters['GAS']}_AMI"} if "GAS" in fuels else {})},
    }


def generate(run: Run, a: argparse.Namespace, fuels: tuple[str, ...], ctx: dict[str, Any]) -> None:
    step(f"GENERATE runtime launchpad files ({' + '.join(fuels)})")
    tz: ZoneInfo = ctx["tz"]
    tag = secrets.token_hex(3).upper()
    ident = new_identity(fuels, a.email, tag, a.zip, a.city, a.state)
    now = datetime.now(tz)
    today = now.date()
    cur = date(today.year, today.month, 1)
    first = add_months(cur, -a.cycles)
    completed = [(add_months(first, i), add_months(first, i + 1)) for i in range(a.cycles)]
    ongoing = (cur, add_months(cur, 1))
    # RAW stops at the last COMPLETE interval before now (a notification pass runs on
    # the date of the last datapoint), and never on the open cycle's end boundary.
    stop_ts = min(int(now.timestamp()) // INTERVAL * INTERVAL, midnight(ongoing[1], tz) - INTERVAL)
    # --raw-end-days-ago N: end the data N days before now, but at least 3 days into the open cycle, so a notification
    # pass runs MID-cycle (Budget Alert "N days left"). The platform skips users whose last datapoint is older than
    # maxEmailDelayPeriodInSecs (864000 = 10 days on the target pilot), so keep N below that.
    if getattr(a, "raw_end_days_ago", 0):
        want = int(now.timestamp()) - int(a.raw_end_days_ago * 86400)
        stop_ts = min(stop_ts, max(want, midnight(cur, tz) + 3 * 86400) // INTERVAL * INTERVAL)
    start_ts = midnight(completed[0][0], tz)
    code = a.bill_cycle_code
    c, ac, p = ident["customer"], ident["account"], ident["premise"]
    stamp = now.strftime("%Y%m%d%H%M%S")
    names = {"BC_SCHEDULE": f"BC_SCHEDULE_{PILOT_ID}_{code}_MDS{tag}{stamp[-6:]}.txt",
             "USERENROLL": f"USERENROLL_D_{stamp[:8]}_{PILOT_ID}_MDS{tag}.csv",
             "METERENROLL": f"METERENROLL_D_{stamp[:8]}_{PILOT_ID}_MDS{tag}.csv",
             "RAW_D": f"RAW_D_900_S_{PILOT_ID}_MDS{tag}_D_{stamp}.txt",
             "RAW_R": f"RAW_D_900_S_{PILOT_ID}_MDS{tag}_R_{stamp}.txt"}
    if "GAS" in fuels:
        names["RAW_G"] = f"RAW_D_900_S_{PILOT_ID}_MDS{tag}_G_{stamp}.txt"
    names["INVOICE"] = f"INVOICE_{PILOT_ID}_MDS{tag}_{stamp}.txt"
    fdir = run.out / "input_files"
    fdir.mkdir(exist_ok=True)
    order = [f for f in ("GAS", "ELECTRIC") if f in fuels]          # ELECTRIC last -> home plan

    # BC_SCHEDULE: existing rows reproduced verbatim, missing months added (exclusive end)
    sched = ctx.get("schedule") or {}
    months = [s for s, _ in completed] + [ongoing[0], add_months(ongoing[0], 1), add_months(ongoing[0], 2)]
    bc, new_months = [], []
    for ms in months:
        st = midnight(ms, tz)
        end = datetime.fromtimestamp(sched[st], tz).date() if st in sched else add_months(ms, 1)
        if st not in sched:
            new_months.append(ms.isoformat())
        bc.append(f"{code}|{ms.isoformat()}|{end.isoformat()}")
    (fdir / names["BC_SCHEDULE"]).write_text("\n".join(bc) + "\n")

    svc = completed[0][0].isoformat()
    ue_rows, me_rows = [], []
    for fuel in order:
        mid = ident["meters"][fuel]
        solar = SOLAR_FALSE if fuel == "ELECTRIC" else ""             # non-solar premise; blank = unchanged (gas)
        ue = [""] * 38
        ue[0:7] = [c, ac, p, "RES", ident["email"], ident["firstName"], ident["lastName"]]
        ue[7], ue[11], ue[12], ue[13] = ident["address"], ident["city"], ident["state"], ident["zip"]
        ue[15], ue[19], ue[20], ue[21] = ident["address"], ident["city"], ident["state"], ident["zip"]
        ue[22], ue[24], ue[27], ue[28], ue[29] = ident["phones"][fuel], "EN", mid, fuel, svc
        ue[32], ue[33], ue[34], ue[35], ue[36] = svc, code, svc, solar, "AMI"
        ue_rows.append("|".join(ue))
        plan = a.rate_plan if fuel == "ELECTRIC" else a.gas_rate_plan
        me_rows.append("|".join([c, ac, p, mid, fuel, svc, "", plan, svc, code, svc, solar, "AMI", ""]))
    (fdir / names["USERENROLL"]).write_text("\n".join(ue_rows) + "\n")
    (fdir / names["METERENROLL"]).write_text("\n".join(me_rows) + "\n")

    cyc = [{"start": s.isoformat(), "end": e.isoformat(), "startTs": midnight(s, tz), "endTs": midnight(e, tz),
            "E": {"dWh": 0.0, "rWh": 0.0, "dCost": 0.0, "rCost": 0.0}, "G": {"ccf": 0.0, "wh": 0.0, "cost": 0.0}}
           for s, e in completed]
    ong = {"start": ongoing[0].isoformat(), "end": ongoing[1].isoformat(),
           "startTs": midnight(ongoing[0], tz), "endTs": midnight(ongoing[1], tz)}
    ep = ElectricProfile(int(c), a.solar_kw, a.base_kw, getattr(a, "peak_kw", 0.0))
    gp = GasProfile(int(c), a.gas_scale)
    e_unit, g_unit = ctx["units"]["ELECTRIC"], ctx["units"].get("GAS", "ccf")
    e_scale = 1.0 if e_unit.lower() == "wh" else 0.001
    sample_day = date(completed[-1][0].year, completed[-1][0].month, 15)
    sample = {"D": {}, "R": {}, "G": {}}
    rates_e, rates_g = ctx["rates"]["ELECTRIC"], ctx["rates"].get("GAS")
    # --transition-rate-plan: the home starts on --rate-plan and moves to the transition plan at the start of the
    # last --transition-cycles completed cycles (one METERENROLL row with RATE_PLAN_EFFECTIVE_DATE). RAW is priced
    # with the plan in force at each interval, so invoices and BILLING_COST both follow the rate schedule.
    rates_t, t_from, t_ts = ctx["rates"].get("ELECTRIC_T"), None, 2 ** 31
    if rates_t is not None:
        n = max(1, min(a.transition_cycles, len(completed)))
        t_from = completed[-n][0]
        t_ts = midnight(t_from, tz)
        names["METERENROLL_T"] = f"METERENROLL_D_{stamp[:8]}_{PILOT_ID}_MDS{tag}_T.csv"
        names["USERENROLL_T"] = f"USERENROLL_D_{stamp[:8]}_{PILOT_ID}_MDS{tag}_T.csv"
    fh = {k: open(fdir / names[k], "w") for k in names if k.startswith("RAW_")}
    ci, count, unrep = 0, 0, []
    ts = start_ts
    while ts < stop_ts:
        local = datetime.fromtimestamp(ts, tz)
        label = raw_label(ts, tz)                                                     # interval END
        if label is None:
            # First fall-back hour: its local label parses to the SECOND occurrence, so the row
            # would be stored an hour late. The DATE format cannot carry it: record, don't write.
            unrep.append({"startTs": ts, "localStart": local.isoformat()})
            ts += INTERVAL
            continue
        while ci < len(cyc) and ts >= cyc[ci]["endTs"]:
            ci += 1
        house, sol = ep.wh(local)
        d_val = round(max(house - sol, 0.0) * e_scale, 3)
        r_val = -round(max(sol - house, 0.0) * e_scale, 3) or 0.0
        fh["RAW_D"].write(f"{c}|{ac}|{p}|{ident['meters']['ELECTRIC']}|{label}|{d_val:.3f}|0|AMI\n")
        fh["RAW_R"].write(f"{c}|{ac}|{p}|{ident['meters']['ELECTRIC']}{RECEIVED_SUFFIX}|{label}|{r_val:.3f}|0|AMI\n")
        g_ccf = 0.0
        if "GAS" in fuels:
            g_ccf = round(gp.ccf(local), 5)
            g_out = g_ccf if g_unit.lower() == "ccf" else g_ccf * CCF_TO_WH
            fh["RAW_G"].write(f"{c}|{ac}|{p}|{ident['meters']['GAS']}|{label}|{g_out:.5f}|0|AMI\n")
        if ci < len(cyc):
            b = cyc[ci]
            dwh, rwh = d_val / e_scale, r_val / e_scale
            b["E"]["dWh"] += dwh
            b["E"]["rWh"] += rwh
            rb = rates_t if ts >= t_ts else rates_e
            b["E"]["dCost"] += dwh / 1000 * rb.rate("DELIVERED", ts, local)
            b["E"]["rCost"] += rwh / 1000 * rb.rate("RECEIVED", ts, local)
            if "GAS" in fuels:
                b["G"]["ccf"] += g_ccf
                b["G"]["wh"] += g_ccf * CCF_TO_WH
                b["G"]["cost"] += g_ccf * CCF_TO_WH / 1000 * rates_g.rate("DELIVERED", ts, local)
        if local.date() == sample_day:
            sample["D"][str(ts)], sample["R"][str(ts)] = d_val / e_scale, r_val / e_scale
            if "GAS" in fuels:
                sample["G"][str(ts)] = g_ccf * CCF_TO_WH
        count += 1
        ts += INTERVAL
    for f in fh.values():
        f.close()

    inv = []
    for b in cyc:
        e = b["E"]
        e["dKwh"], e["rKwh"] = round(e["dWh"] / 1000, 3), round(e["rWh"] / 1000, 3)
        e["dCost"], e["rCost"] = round(e["dCost"], 2), round(e["rCost"], 2)
        e["fixed"] = round(ctx["transitionFixed"] if b["startTs"] >= t_ts and ctx.get("transitionFixed") is not None
                           else a.fixed_charge, 2)
        e["plan"] = a.transition_rate_plan if b["startTs"] >= t_ts else a.rate_plan
        e["energy"] = round(e["dCost"] + e["rCost"], 2)              # invoice energy = D + FEED_IN
        e["total"] = round(e["energy"] + e["fixed"], 2)
        s, en = date.fromisoformat(b["start"]), date.fromisoformat(b["end"])
        days = (en - s).days
        for fuel in order:
            pre = f"{c}|{ac}|{p}|{ident['meters'][fuel]}|{fuel}|{code}|{s.isoformat()}|{en.isoformat()}|{days}"
            if fuel == "ELECTRIC":
                inv += [f"{pre}|TOTAL|TOTAL|{e['dKwh']:.3f}|{e['total']:.2f}|AMI|",
                        f"{pre}|CONSUMPTION_BASED|CONSUMPTION_BASED|{e['dKwh']:.3f}|{e['dCost']:.2f}|AMI|",
                        f"{pre}|FEED_IN|CONSUMPTION_BASED|{e['rKwh']:.3f}|{e['rCost']:.2f}|AMI|"]
                if e["fixed"]:
                    inv.append(f"{pre}|MONTHLY_CONNECTION_CHARGE|FIXED|0.000|{e['fixed']:.2f}|AMI|")
            else:
                g = b["G"]
                g["ccf"], g["kwh"] = round(g["ccf"], 3), round(g["wh"] / 1000, 3)
                g["cost"], g["fixed"] = round(g["cost"], 2), round(a.gas_fixed_charge, 2)
                g["total"] = round(g["cost"] + g["fixed"], 2)
                inv += [f"{pre}|TOTAL|TOTAL|{g['ccf']:.3f}|{g['total']:.2f}|AMI|",
                        f"{pre}|CONSUMPTION_BASED|CONSUMPTION_BASED|{g['ccf']:.3f}|{g['cost']:.2f}|AMI|"]
                if g["fixed"]:
                    inv.append(f"{pre}|MONTHLY_CONNECTION_CHARGE|FIXED|0.000|{g['fixed']:.2f}|AMI|")
    (fdir / names["INVOICE"]).write_text("\n".join(inv) + "\n")
    transition = None
    if t_from is not None:
        mid = ident["meters"]["ELECTRIC"]
        row = [c, ac, p, mid, "ELECTRIC", svc, "", a.transition_rate_plan, t_from.isoformat(), code, svc, SOLAR_FALSE, "AMI", ""]
        (fdir / names["METERENROLL_T"]).write_text("|".join(row) + "\n")
        # Same transition as a USERENROLL update of the existing user (rate plan id + effective date columns 31/32).
        # Only LaunchpadUserCreator.updateRatePlans publishes TOU_ONBOARDING; LaunchpadMeterCreator.updateRatePlans
        # (the METERENROLL path) applies the same schedule but never calls TouEmailAlertManager.
        ue_t = ue_rows[-1].split("|")
        ue_t[31], ue_t[32] = a.transition_rate_plan, t_from.isoformat()
        (fdir / names["USERENROLL_T"]).write_text("|".join(ue_t) + "\n")
        transition = {"plan": a.transition_rate_plan, "planNumber": ctx["planNumbers"]["ELECTRIC_T"],
                      "fromPlan": a.rate_plan, "fromPlanNumber": ctx["planNumbers"]["ELECTRIC"],
                      "from": t_from.isoformat(), "fromTs": t_ts, "cycles": sum(1 for b in cyc if b["startTs"] >= t_ts),
                      "rateStructure": rates_t.structure()}

    run.m.update({
        "schema": 2, "pilotId": PILOT_ID, "fuels": list(fuels), "createdAt": now.isoformat(),
        "timezone": str(tz), "units": ctx["units"], "ratePlan": a.rate_plan, "gasRatePlan": a.gas_rate_plan,
        "planNumbers": ctx["planNumbers"], "billCycleCode": code, "identity": ident,
        "files": {k: str(fdir / v) for k, v in names.items()},
        "fileOrder": ["BC_SCHEDULE", "USERENROLL", "METERENROLL", "RAW_D", "RAW_R"]
                     + (["RAW_G"] if "GAS" in fuels else []) + ["INVOICE"],
        "bcNewMonths": new_months, "cycles": cyc, "ongoing": ong, "transition": transition,
        "raw": {"firstTs": start_ts, "lastTs": stop_ts - INTERVAL, "intervals": count,
                "expectedIntervals": (stop_ts - start_ts) // INTERVAL, "unrepresentable": unrep,
                "labelFormat": "yyyyMMddHHmmss local (interval end)"},
        "coverage": {"rateStructure": {"ELECTRIC": rates_e.structure(),
                                       **({"GAS": rates_g.structure()} if "GAS" in fuels else {})}},
        "sample": {"date": sample_day.isoformat(), "values": sample},
    })
    if getattr(a, "budget_billing", False):
        import mds_bb                       # lazy: mds_bb imports this module
        mds_bb.add_budget_billing(run, a, ctx, fdir, names, stamp)
    record_files(run)                       # final bytes of every produced file: never rewritten from here on
    last = cyc[-1]
    log(f"identity customer={c} account={ac} premise={p} meters={ident['meters']} email={ident['email']}")
    log(f"{len(cyc)} completed cycles {cyc[0]['start']}..{last['end']} (exclusive) + ongoing {ong['start']}; "
        f"RAW {count} intervals/stream through {datetime.fromtimestamp(stop_ts, tz):%Y-%m-%d %H:%M} local")
    if unrep:
        log(f"RAW coverage INCOMPLETE by format: {len(unrep)} interval(s)/stream in the first fall-back hour cannot be "
            f"expressed as a local yyyyMMddHHmmss label ({unrep[0]['localStart']} ..); not written, excluded from "
            "invoice sums, reported by E06", "WARN")
    for f_, st in run.m["coverage"]["rateStructure"].items():
        if st == "flat":
            log(f"{f_} rate plan is FLAT (one rate per stream all day): this run does not exercise TOU peak/off-peak")
    le = last["E"]
    log(f"MS cycle = last completed {last['start']}..{last['end']}: ELECTRIC D {le['dKwh']} kWh ${le['dCost']} | "
        f"FEED_IN {le['rKwh']} kWh ${le['rCost']} | energy ${le['energy']} | invoice total ${le['total']}")
    if "GAS" in fuels:
        lg = last["G"]
        log(f"MS cycle GAS {lg['ccf']} ccf ({lg['kwh']} kWh) energy ${lg['cost']} | invoice total ${lg['total']}")
    if transition:
        log(f"RATE TRANSITION {transition['fromPlan']} ({transition['fromPlanNumber']}) -> {transition['plan']} "
            f"({transition['planNumber']}) effective {transition['from']}: last {transition['cycles']} completed cycle(s) "
            f"+ ongoing priced on it ({transition['rateStructure']})")
    for k, v in names.items():
        log(f"  {k:<12} {v}")
    log_details(run, "USER DETAILS (generated)")


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------
def poll(desc: str, fn: Callable[[], tuple[bool, str]], timeout: int, every: int) -> str:
    t0, last = time.time(), None
    while True:
        try:
            ok, info = fn()
        except SetupError as exc:
            ok, info = False, f"(error: {str(exc)[:200]})"
        el = int(time.time() - t0)
        if info != last or ok:
            log(f"WAIT {desc}: {'DONE' if ok else 'pending'} after {el}s -- {info}")
            last = info
        if ok:
            return info
        if el >= timeout:
            raise WaitTimeout(f"timed out after {timeout}s waiting for {desc}: {info}")
        time.sleep(every)


def find_uuid(ext: str) -> str | None:
    res = payload(api("GET", "v2.0/pii/users?" + urllib.parse.urlencode(
        {"pilotId": PILOT_ID, "externalUserId": ext, "limit": 10, "offset": 0})))
    ids = {r["uuid"] for r in (res or {}).get("data", []) if r.get("externalUserId") == ext}
    if len(ids) > 1:
        raise SetupError(f"{len(ids)} users share externalUserId {ext}")
    return next(iter(ids), None)


def all_meters(uuid: str) -> dict[str, dict[str, Any]]:
    """token -> {gid, mid, gateway, meter} across every gateway of the home."""
    out: dict[str, dict[str, Any]] = {}
    gws = api("GET", f"meta/users/{uuid}/homes/{HID}/gws/", allow=(404, 500))
    if status_of(gws) != 200:
        return out
    for gpath, gw in gws.items():
        if not isinstance(gw, dict):
            continue
        gid = gpath.rstrip("/").split("/")[-1]
        mts = api("GET", f"meta/users/{uuid}/homes/{HID}/gws/{gid}/meters/", allow=(404, 500))
        if status_of(mts) != 200:
            continue
        for mpath, mt in mts.items():
            if isinstance(mt, dict) and mt.get("token"):
                out[mt["token"]] = {"gid": gid, "mid": mpath.rstrip("/").split("/")[-1], "gateway": gw, "meter": mt}
    return out


def ctype(uuid: str, kind: str, suffix: str, mt: str, t0: int, t1: int, mode: str = "month", extra: str = "") -> dict:
    q = f"t0={t0}&t1={t1}&mode={mode}&measurementType={mt}&skipInvoice=true&convertConsumptionUnit=false{extra}"
    r = api("GET", f"billingdata/users/{uuid}/homes/{HID}/consumption/ctypes/{kind}{suffix}?{q}")
    return r if isinstance(r, dict) else {}


def streams_of(resp: dict) -> dict[str, dict]:
    """all-streams responses are {stream: {ts: v}}; single-stream ones are {ts: v}."""
    if resp and all(not k.lstrip("-").isdigit() for k in resp):
        return resp
    return {"DELIVERED": resp}


def inv_cost(v: dict, mt: str) -> float:
    return float((v.get("gasCost") if mt == "GAS" else v.get("cost")) or 0)


def inv_value(v: dict, mt: str) -> float:
    return float((v.get("gasValue") if mt == "GAS" else v.get("value")) or 0)


def utility_data(uuid: str, mt: str, t0: int, t1: int) -> dict:
    r = api("GET", f"billingdata/users/{uuid}/homes/{HID}/utilitydata?t0={t0}&t1={t1}&measurementType={mt}")
    return r if isinstance(r, dict) else {}


def near(buckets: dict, ts: int, slack: int = 86400) -> tuple[int, Any] | None:
    best = None
    for k, v in (buckets or {}).items():
        try:
            kt = int(float(k))
        except ValueError:
            continue
        if abs(kt - ts) <= slack and (best is None or abs(kt - ts) < abs(best[0] - ts)):
            best = (kt, v)
    return best


def close(x: float, y: float, rel: float, absol: float) -> bool:
    return abs(x - y) <= max(absol, rel * abs(y))


FUEL_KEYS = {"ELECTRIC": ("electric", "electricity"), "GAS": ("gas",)}
# itemization API category -> display name used in the Monthly Summary email
EMAIL_LABELS = {"airConditioning": ["Cooling", "Air Conditioning"], "cooling": ["Cooling"],
                "spaceHeating": ["Heating", "Space Heating"], "heating": ["Heating"],
                "waterHeating": ["Water Heating"], "alwaysOn": ["Always On"], "poolPump": ["Pool Pump"],
                "electricVehicle": ["Electric Vehicle", "EV"], "ev": ["Electric Vehicle", "EV"],
                "refrigeration": ["Refrigeration"], "other": ["Other"]}


def itemization(uuid: str, endpoint_id: str, t0: int, t1: int) -> list[dict]:
    q = urllib.parse.urlencode({"t0": t0, "t1": t1, "extended": "true", "hybrid": "true", "mode": "month",
                                "totalFromInvoice": "true", "showFailedBillingCycles": "true",
                                "round": "false", "showPercentage": "false"})
    r = payload(api("GET", f"v2.0/users/{uuid}/endpoints/{endpoint_id}/itemizationDetails?{q}", allow=(404, 500)))
    return (r or {}).get("itemizationDetails") or [] if isinstance(r, dict) else []


def cycle_items(entries: list[dict], start_ts: int, fuel: str) -> list[dict] | None:
    for e in entries:
        if abs(int(float(e.get("startTs", 0))) - start_ts) <= 86400:
            for k in FUEL_KEYS[fuel]:
                if e.get(k):
                    return e[k]
            return None
    return None


def split_items(items: list[dict]) -> tuple[list[dict], dict | None, dict | None]:
    """-> (appliances excluding solar/total, total row id 17, solar row id 16)."""
    total = next((i for i in items if int(float(i.get("id", -1))) == 17), None)
    solar = next((i for i in items if int(float(i.get("id", -1))) == 16), None)
    apps = [i for i in items if int(float(i.get("id", -1))) not in (16, 17)]
    return apps, total, solar


# ---------------------------------------------------------------------------
# AWS
# ---------------------------------------------------------------------------
def aws(profile: str | None):
    try:
        import boto3
    except ImportError as exc:
        raise SetupError("boto3 is required: pip install boto3") from exc
    sess = boto3.Session(profile_name=profile or need("AWS_PROFILE"), region_name=need("AWS_REGION"))
    acct = sess.client("sts").get_caller_identity().get("Account")
    if str(acct) != str(need("AWS_ACCOUNT")):        # the declared account of the profile, validated before any S3/SQS use
        raise SetupError(f"AWS account {acct} != the profile's email.aws.account_id {AWS_ACCOUNT}: refusing to touch buckets/queues")
    return mds_target.GuardedSession(sess)            # every S3/SQS write passes the run's write policy


def s3_exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Enrollment-file integrity
# ---------------------------------------------------------------------------
# Every launchpad file the run produces (manifest "files": USERENROLL, METERENROLL, RAW_*, INVOICE, BC_SCHEDULE,
# *_T transitions, USERPROGRAM) is recorded ONCE in manifest "enrollmentFiles", keyed by file name:
#   {name, role, path, relPath (to the run dir), sha256, size, recordedAt,
#    uploads: [{bucket, key, sha256, size, at, journal: {path, line, sha256} | None}]}
# A recorded file is never rewritten. Every later use -- (re-)upload, a resumed phase (--manifest), the TOU transition
# that rebuilds a row from the ORIGINAL USERENROLL -- reads the stored copy only through verified_bytes(), which refuses
# (ENROLLMENT_INTEGRITY, nothing sent) when it is missing or its bytes differ from the record. The debug kit
# (emailkit.enrollment) verifies the same records before it adopts or aggregates a provisioned user.
class IntegrityError(SetupError):
    def __init__(self, msg: str):
        super().__init__(f"ENROLLMENT_INTEGRITY: {msg}")


def _records(run: Run) -> dict[str, dict]:
    return run.m.setdefault("enrollmentFiles", {})


def _role_of(run: Run, path: Path) -> str | None:
    return next((k for k, v in (run.m.get("files") or {}).items() if Path(v).name == path.name), None)


def _sha256(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def record_file(run: Run, path: Path, role: str | None = None) -> dict:
    """Record a produced file once (sha256 + size of its bytes now). An existing record is never replaced: the same
    name with other bytes is refused."""
    path = Path(path)
    if not path.is_file():
        raise IntegrityError(f"{path.name}: produced file is missing at {path}, cannot record it")
    data = path.read_bytes()
    recs = _records(run)
    rec = recs.get(path.name)
    if rec is not None:
        if rec.get("sha256") != _sha256(data):
            raise IntegrityError(f"{path.name} changed since it was recorded (sha256 {_sha256(data)[:12]} != manifest "
                                 f"{str(rec.get('sha256'))[:12]}): a recorded enrollment file is never rewritten")
        return rec
    try:
        rel = str(path.resolve().relative_to(run.out.resolve()))
    except ValueError:
        raise IntegrityError(f"{path.name} is outside the run directory {run.out}: not recordable as this run's original")
    rec = {"name": path.name, "role": role or _role_of(run, path), "path": str(path), "relPath": rel,
           "sha256": _sha256(data), "size": len(data), "recordedAt": datetime.now(timezone.utc).isoformat(), "uploads": []}
    recs[path.name] = rec
    return rec


def record_files(run: Run) -> None:
    for role, p in (run.m.get("files") or {}).items():
        record_file(run, Path(p), role)
    run.save()


def verified_bytes(run: Run, path: Path) -> bytes:
    """The stored original, only if it is byte-identical to its manifest record (missing/changed -> IntegrityError)."""
    path = Path(path)
    rec = _records(run).get(path.name)
    if rec is None:
        raise IntegrityError(f"{path.name} has no integrity record in {run.out / 'manifest.json'}: cannot prove it is the "
                             "file this run produced")
    p = run.out / rec["relPath"]
    if not p.is_file():
        raise IntegrityError(f"{path.name}: the stored original is missing ({p}); it is never regenerated or rebuilt")
    data = p.read_bytes()
    got = _sha256(data)
    if got != rec["sha256"] or len(data) != rec.get("size", len(data)):
        raise IntegrityError(f"{path.name} changed since it was recorded (sha256 {got[:12]} != manifest {rec['sha256'][:12]}, "
                             f"{len(data)} vs {rec.get('size')} bytes): refusing to use or re-upload it")
    return data


def verify_recorded_files(run: Run, adopt_unrecorded: bool = False) -> list[str]:
    """Before a resumed run touches anything: every file of the manifest must have a record and match it. A manifest
    from before integrity records exists only with the operator's explicit --adopt-unrecorded-files (logged and stamped
    into the manifest; the kit never passes it)."""
    files = run.m.get("files") or {}
    recs = run.m.get("enrollmentFiles") or {}
    missing = [Path(p).name for p in files.values() if Path(p).name not in recs]
    uploaded = {u.get("key") for u in run.m.get("uploads") or []}
    if missing:
        if not adopt_unrecorded:
            raise IntegrityError(f"no integrity record for {missing} in {run.out / 'manifest.json'} (manifest predates "
                                 "enrollment integrity, or the record was removed): the originals cannot be proven. An "
                                 "operator who has checked them may re-run with --adopt-unrecorded-files")
        for role, p in files.items():
            if Path(p).name in missing:
                rec = record_file(run, Path(p), role)
                rec["adopted"] = {"at": datetime.now(timezone.utc).isoformat(), "uploadedBefore": Path(p).name in uploaded}
        run.m.setdefault("integrityAdoptions", []).append({"at": time.time(), "files": missing})
        run.save()
        log(f"ADOPTED unrecorded originals {missing} at their CURRENT bytes (operator --adopt-unrecorded-files)", "WARN")
    stray = sorted(k for k in uploaded if k and k not in recs and k not in missing)
    if stray:
        raise IntegrityError(f"uploaded file(s) {stray} have no integrity record")
    for name in list(_records(run)):
        verified_bytes(run, Path(name))
    return sorted(_records(run))


def upload(run: Run, s3, bucket: str, path: Path) -> None:
    key = path.name
    if key not in _records(run):            # first use of a file produced outside generate() (USERPROGRAM, recovery _T)
        record_file(run, path)
    body = verified_bytes(run, path)        # exactly the recorded bytes, or nothing is sent
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="text/plain; charset=UTF-8",
                  ServerSideEncryption="AES256", Metadata={"utility_file_name": key})
    ref = mds_target.last_journal_ref()
    if s3.head_object(Bucket=bucket, Key=key).get("Metadata", {}).get("utility_file_name") != key:
        raise SetupError(f"s3://{bucket}/{key} lost its utility_file_name metadata")
    at = datetime.now(timezone.utc).isoformat()
    digest = _sha256(body)
    _records(run)[key]["uploads"].append({"bucket": bucket, "key": key, "sha256": digest, "size": len(body), "at": at,
                                         "journal": ref})
    run.m.setdefault("uploads", []).append({"key": key, "at": at, "sha256": digest})
    run.save()
    log(f"UPLOAD s3://{bucket}/{key} ({len(body)} bytes, sha256 {digest[:12]})")


def sqs_send(run: Run, sqs, queue: str, body: str, kind: str) -> None:
    (run.dir("sqs_messages") / f"SQS_{kind}_{int(time.time())}.{'json' if body.startswith('{') else 'xml'}").write_text(body)
    url = sqs.get_queue_url(QueueName=queue)["QueueUrl"]
    mid = sqs.send_message(QueueUrl=url, MessageBody=body)["MessageId"]
    run.m.setdefault("sqs", []).append({"kind": kind, "queue": queue, "messageId": mid, "at": time.time()})
    run.save()
    log(f"SQS {kind} -> {queue} ({mid})")


def disagg_xml(uuid: str, mt: str, zipcode: str, t0: int, t1: int) -> str:
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<gbTempDataReadyEvent country="US" disaggMode="COMPLETE_BILLING_CYCLE" end="{t1}" homeOrdinal="{HID}" '
            f'measurementType="{mt}" pilotId="{PILOT_ID}" retryCount="0" skipWriteToCassandra="false" start="{t0}" '
            f'storeTimebandInCassandra="false" triggerId="{uuid_lib.uuid4()}" triggerName="MANUAL_TEST_TRIGGER" '
            f'userId="{uuid}" zipCode="{zipcode}"/>')


def agg_xml(uuid: str, mt: str, streams: Iterable[str], t0: int, t1: int) -> str:
    """sendNotifications=true is REQUIRED: it is what runs hybrid itemization.
    deleteBeforeRun=true is REQUIRED: RAW-time aggregation buckets months on the pilot's
    BC_SCHEDULE rows (code 4 ends on the LAST day, e.g. Aug 1 -> Aug 31), so each month misses
    its final day until the months are rebuilt on the invoice cycles (Aug 1 -> Sep 1)."""
    ct = "".join(f"<consumptionType>{t}</consumptionType>" for t in
                 ("ENERGY_CONSUMPTION", "TOU", "BILLING_COST", "TIER", "APPLIANCE_DISAGG"))
    modes = "".join(f"<addMode>{m}</addMode>" for m in ("DAY", "MONTH"))       # JAXB element is addMode
    st = "".join(f"<streamType>{s}</streamType>" for s in streams)
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<aggregateUploadEvent><uuid>{uuid}</uuid><hid>{HID}</hid><startTs>{t0}</startTs><endTs>{t1}</endTs>'
            '<sendNotifications>true</sendNotifications><refreshRate>false</refreshRate>'
            f'<measurementType>{mt}</measurementType><pilotId>{PILOT_ID}</pilotId>'
            f'<triggerName>MANUAL_TEST_TRIGGER</triggerName><triggerId>{uuid_lib.uuid4()}</triggerId>'
            f'<raiseNSMEvent>false</raiseNSMEvent><consumptionTypes>{ct}</consumptionTypes><aggModes>{modes}</aggModes>'
            f'<streamTypes>{st}</streamTypes><aggregationEventStage>RUN_AGGREGATIONS</aggregationEventStage>'
            '<deleteBeforeRun>true</deleteBeforeRun><runHybridV2>false</runHybridV2>'
            '<storeTimebandInCassandra>false</storeTimebandInCassandra><eventHBATriggered>false</eventHBATriggered>'
            '</aggregateUploadEvent>')


FUEL_STREAMS = {"ELECTRIC": ("DELIVERED", "RECEIVED"), "GAS": ("DELIVERED",)}


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------
def preflight(run: Run, a: argparse.Namespace, fuels: tuple[str, ...]) -> dict[str, Any]:
    step("PREFLIGHT pilot readiness (read-only unless an --ensure-* flag is given)")
    ctx: dict[str, Any] = {}
    lic = read_config("pilot", PILOT_ID, "launchpad_ingestion_configs")
    if not lic:
        raise SetupError("cannot read launchpad_ingestion_configs -- token invalid?")
    bad = []
    if cfg(lic, "create_streams_based_on_supported_streams_config") != "true":
        bad.append("create_streams_based_on_supported_streams_config != true")
    if cfg(lic, "handle_meter_enrollment") != "true":
        bad.append("handle_meter_enrollment != true")
    if cfg(lic, "isEndDateInclusive", "false") != "false":
        bad.append("isEndDateInclusive is not false (this generator writes exclusive end dates)")
    ss = json.loads(cfg(lic, "supported_streams", "{}") or "{}")
    ss_fuels = {x.get("measurementType"): {s["streamType"] for s in x.get("streams", [])}
                for x in ss.get("supportedStreams", [])}
    if not {"DELIVERED", "RECEIVED"} <= ss_fuels.get("ELECTRIC", set()):
        bad.append(f"supported_streams ELECTRIC lacks DELIVERED/RECEIVED: {ss_fuels}")
    if "GAS" in fuels and "DELIVERED" not in ss_fuels.get("GAS", set()):
        if a.ensure_gas_stream:
            backup(run, "launchpad_ingestion_configs")
            ss.setdefault("supportedStreams", []).append(
                {"measurementType": "GAS", "streams": [{"streamType": "DELIVERED", "streamTokenSuffix": ""}]})
            post_config(run, PILOT_ID, "launchpad_ingestion_configs",
                        {"supported_streams": json.dumps(ss, separators=(",", ":"))}, "PILOT")
            log("waiting 60s for the ingester's pilot config cache to pick up supported_streams GAS")
            time.sleep(60)
        else:
            bad.append("supported_streams has no GAS entry: gas meters would silently never be created. "
                       "Re-run with --ensure-gas-stream to add "
                       '{"measurementType":"GAS","streams":[{"streamType":"DELIVERED","streamTokenSuffix":""}]} '
                       "(pilot-level, backed up first)")
    if bad:
        raise SetupError("pilot not ready: " + "; ".join(bad))
    dtype, dfmt = cfg(lic, "raw_data_date_type", "DATE"), cfg(lic, "raw_data_date_format", "yyyyMMddHHmmss")
    if dtype != "DATE" or dfmt != "yyyyMMddHHmmss" or cfg(lic, "isConsDataGivenByStartTime", "false") == "true":
        raise SetupError(f"RAW format raw_data_date_type={dtype} raw_data_date_format={dfmt} isConsDataGivenByStartTime="
                         f"{cfg(lic, 'isConsDataGivenByStartTime')}: the generator only writes local yyyyMMddHHmmss "
                         "interval-END labels")
    ctx["tz"] = ZoneInfo(cfg(lic, "parser_time_zone", DEFAULT_TZ) or DEFAULT_TZ)
    units = json.loads(cfg(lic, "raw_data_consumption_unit_map", "{}") or "{}")
    ctx["units"] = {"ELECTRIC": a.raw_unit or units.get("ELECTRIC") or "Wh", "GAS": units.get("GAS", "ccf")}
    if "GAS" in fuels and ctx["units"]["GAS"].lower() != "ccf":
        raise SetupError(f"GAS raw unit is {ctx['units']['GAS']}; this generator writes ccf")
    mt = read_config("pilot", PILOT_ID, "measurement_type")
    if "GAS" in fuels and "gas" not in cfg(mt, "allowed_measurement_type").lower():
        raise SetupError("measurement_type.allowed_measurement_type does not include Gas")
    di = read_config("pilot", PILOT_ID, "data_ingestion")
    # the destination is the profile's launchpad bucket; the pilot's own data_ingestion setting must agree (never re-routes)
    ctx["bucket"] = need("DEFAULT_BUCKET")
    pil_bucket = cfg(di, "s3BucketName", "")
    if a.bucket and a.bucket != ctx["bucket"]:
        raise SetupError(f"--bucket {a.bucket} differs from the profile's launchpad bucket {ctx['bucket']}")
    if pil_bucket and pil_bucket != ctx["bucket"]:
        raise SetupError(f"pilot data_ingestion.s3BucketName {pil_bucket} != the profile's launchpad bucket {ctx['bucket']}: "
                         "the profile does not describe this target")
    log(f"pilot {PILOT_ID}: tz={ctx['tz']} units={ctx['units']} bucket={ctx['bucket']} "
        f"supported_streams={ {k: sorted(v) for k, v in ss_fuels.items()} }")

    # itemization + MS prerequisites (pilot scope where the code only reads pilot scope)
    md = read_config("pilot", PILOT_ID, "meta_data")
    events = cfg(md, "supported_communication_events")
    if "MONTHLY_SUMMARY" not in events.split(","):
        # non-solar homes read the plain list (the solar list only matters for solar homes); never written from here.
        # Only the Monthly Summary needs it, so the run goes on (user, data, other emails) and E21 reports it.
        run.m["preflightWarnings"] = run.m.get("preflightWarnings", []) + ["supported_communication_events lacks MONTHLY_SUMMARY"]
        log("meta_data.supported_communication_events lacks MONTHLY_SUMMARY: no Monthly Summary will be generated for a "
            "non-solar user (PILOT config, ask the pilot owner); the other phases continue", "WARN")
    dp, hm = read_config("pilot", PILOT_ID, "disagg_preference"), read_config("pilot", PILOT_ID, "hybrid_moderation")
    log(f"pilot hybrid: enable_hybrid_v2={cfg(dp, 'enable_hybrid_v2')} electric_hybrid={cfg(hm, 'electric_hybrid_disagg_enabled')} "
        f"gas_hybrid={cfg(hm, 'gas_hybrid_disagg_enabled')} csv_format={cfg(hm, 'csv_filename_format')} "
        "(user scope is set on the new user regardless)")

    sess = aws(a.aws_profile)
    ctx["s3"], ctx["sqs"] = sess.client("s3"), sess.client("sqs")
    try:
        ctx["s3"].head_bucket(Bucket=ctx["bucket"])
    except Exception as exc:  # noqa: BLE001
        raise SetupError(f"no access to s3://{ctx['bucket']}: {exc}") from exc
    keys = need("MODEL_KEYS")
    models = [keys[f] for f in fuels if f in keys]
    if len(models) != len(fuels):
        raise SetupError(f"the profile's model_keys state no hybrid model for {[f for f in fuels if f not in keys]}")
    missing = [k for k in models if not s3_exists(ctx["s3"], need("MODEL_BUCKET"), k)]
    if missing:
        raise SetupError(f"hybrid model files missing in s3://{MODEL_BUCKET}/: {missing} (copy them from a configured pilot)")
    log(f"hybrid models present: {models}")

    num, rb = resolve_plan(a.rate_plan)
    ctx["rates"], ctx["planNumbers"] = {"ELECTRIC": rb}, {"ELECTRIC": num}
    if "GAS" in fuels:
        gnum, grb = resolve_plan(a.gas_rate_plan)
        ctx["rates"]["GAS"], ctx["planNumbers"]["GAS"] = grb, gnum
    if getattr(a, "transition_rate_plan", None):
        tnum, trb = resolve_plan(a.transition_rate_plan)
        ctx["rates"]["ELECTRIC_T"], ctx["planNumbers"]["ELECTRIC_T"] = trb, tnum
        fixed = [float(r["rate"]) for r in trb.rows if r.get("chargeType") == "MONTHLY_FIXED" and not r.get("isHoliday")]
        ctx["transitionFixed"] = sum(fixed) if fixed else None
        if trb.structure() == "flat":
            raise SetupError(f"--transition-rate-plan {a.transition_rate_plan} is flat: a TOU transition needs a TOU plan")
    log(f"rate plans {ctx['planNumbers']}")
    res = payload(api("GET", f"2.1/utilityBillingCycles/utility/{PILOT_ID}")) or {}
    rows = res.get("data", []) if isinstance(res, dict) else res
    ctx["schedule"] = {int(r["validFrom"]): int(r["validTo"]) for r in rows
                       if str(r.get("billCycleCode")) == a.bill_cycle_code}
    run.m["preflight"] = {"tz": str(ctx["tz"]), "units": ctx["units"], "bucket": ctx["bucket"],
                          "planNumbers": ctx["planNumbers"]}
    run.save()
    return ctx


def backup(run: Run, ctype_name: str) -> Path:
    raw = api("GET", f"entities/pilot/{PILOT_ID}/configs/{ctype_name}")
    p = run.dir("pilot_config_backups") / f"BACKUP_pilot{PILOT_ID}_{ctype_name}_{datetime.now():%Y%m%d_%H%M%S}.json"
    p.write_text(json.dumps(raw, indent=2))
    log(f"backed up pilot {ctype_name} -> {p}")
    return p


# Combined dual-fuel Monthly Summary: ONE email for both fuels. Verified end to end on the target pilot (user 6718b420,
# 2026-09-29): the per-fuel GAS pass NPEs on DCX solar homes (EmailDataProvider:980); the combined pass does not,
# and DF_ITEMIZATION renders the ELECTRIC and GAS appliance blocks. Five user-scope configs are required.
COMBINED_DF_SECTIONS_DEFAULT = ["TEASER", "HEADER_V4", "NEW_SERVICE_V2", "GREETING_V2", "MONTHLY_SUMMARY_V2",
                                "ENVIRONMENT_IMPACT", "DF_ITEMIZATION", "SPONSORED_RECO_V2",
                                "ENERGY_SAVINGS_TIPS_V2", "MONTHLY_USAGE_HISTORY_CHART_V2", "CTA_WITH_DES_SECTION_V2",
                                "FEEDBACK_V4", "FOOTER_V4"]


def combined_df_sections(uuid: str) -> list[dict]:
    """MONTHLY_SUMMARY.DF_EMAILS list from the user's live ELECTRIC MS list. The single-fuel itemization
    sections are replaced by DF_ITEMIZATION (GenericEventDataEmailContentBuilder:951-975 takes ITEMIZATION,
    then ITEMIZATION_V2, then DF_ITEMIZATION: any single-fuel one present wins and renders one fuel only);
    TOU_RATE_PROMOTION is electric-only. Falls back to the list verified on 2026-09-29."""
    try:
        r = payload(api("GET", f"entities/user/{uuid}/configs/email_template_sections_config"
                               "?context=MONTHLY_SUMMARY&measurement-type=ELECTRIC", allow=(404, 500))) or {}
        kv = {x["key"]: x for x in json.loads(r.get("email_template_sections_config") or "{}").get("kvs", [])}
        base = json.loads(kv["MONTHLY_SUMMARY.OPT_IN"]["val"])
    except (KeyError, TypeError, ValueError, SetupError):
        base = [{"emailTemplateSection": x, "mandatory": x == "DF_ITEMIZATION"} for x in COMBINED_DF_SECTIONS_DEFAULT]
    out, have_df = [], False
    for sec in base:
        name = sec.get("emailTemplateSection")
        if name == "TOU_RATE_PROMOTION":
            continue
        if name in ("ITEMIZATION", "ITEMIZATION_V2", "DF_ITEMIZATION"):
            if not have_df:
                out.append({"emailTemplateSection": "DF_ITEMIZATION", "mandatory": True})
                have_df = True
            continue
        out.append(sec)
    if not have_df:
        out.append({"emailTemplateSection": "DF_ITEMIZATION", "mandatory": True})
    return out


def combined_df_configs(uuid: str, gas_unit: str = "CCF") -> dict[str, dict[str, str]]:
    sections = json.dumps(combined_df_sections(uuid), separators=(",", ":"))
    return {
        # EventProcessorUtils.isCombinedDfEmail:1117 -> MonthlySummaryUserProcessor.sendEmailForCombinedDF
        "email_moderation": {"enable_combined_dual_fuel_email": "true",
                             "eligible_fuels_for_dual_fuel_email": '["ELECTRIC","GAS"]',
                             # EmailDataProvider.getConsumptionUnitMap: without the map only the FIRST fuel gets a
                             # unit and DF_ITEMIZATION NPEs on UnitType.getResolvedUnitName for GAS
                             "consumption_unit_type_map": json.dumps({"ELECTRIC": "kWh", "GAS": gas_unit}, separators=(",", ":"))},
        # the combined pass asks for delivery modes with measurementType=null -> legacy
        # event_subscriptions.MONTHLY_SUMMARY, which is GLOBAL [] on the target pilot ("No fuel type eligible")
        "event_subscriptions.MONTHLY_SUMMARY": {"delivery_modes": '["Email"]'},
        # sections for a combined email are read ONLY from <event>.DF_EMAILS.<userType>
        "email_template_sections_config": {"MONTHLY_SUMMARY.DF_EMAILS.OPT_IN": sections,
                                           "MONTHLY_SUMMARY.DF_EMAILS.OPT_OUT": sections},
    }


# User-scope strings for the combined email (string hierarchy: <uuid>.MONTHLY_SUMMARY.<FUEL>). Each DF_ITEMIZATION
# block takes its heading from its own fuel's string map, so identical headings are what made the gas block
# unlabelled; the MONTHLY_SUMMARY_V2 summary section and the subject are single-fuel (electric) in the product.
COMBINED_DF_STRINGS = {
    "ELECTRIC": {"com.bidgely.cloud.email.itemization.title.text.ami": "Your Electricity Use By Appliance",
                 "com.bidgely.cloud.email.sentences.applianceBreakdownTitle": "Your Electricity Use By Appliance",
                 "com.bidgely.cloud.email.ms.usage.summary.title.solar": "Your Electricity Summary",
                 "com.bidgely.cloud.email.ms.usageSummaryTitle": "Your Electricity Summary",
                 # the combined solar bill summary is kWh (code): label it as usage, as NVE did (PEREQ-3312)
                 "com.bidgely.cloud.email.ms.amountText.ami.elec": "Your Estimated Usage",
                 "com.bidgely.cloud.email.ms.amountText.amr.elec": "Your Estimated Usage"},
    # NOT settable here: the subject. User-scope overrides of com.bidgely.cloud.email.subject.ms.solar and
    # ...subject.monthlySummaryOptOutMail had no effect (2026-09-29); it stays "You spent <kWh> on electricity ..."
    "GAS": {"com.bidgely.cloud.email.itemization.title.text.ami": "Your Natural Gas Use By Appliance",
            "com.bidgely.cloud.email.sentences.applianceBreakdownTitle": "Your Natural Gas Use By Appliance"},
}


def put_user_string(run: Run, entity: str, resource_id: str, text: str, locale: str = "en_US") -> bool:
    body = [{"locale": locale, "text": text}]
    r = api("PUT", f"2.1/stringResources/{entity}/resource/{resource_id}", body, allow=(400, 404, 500))
    if status_of(r) != 200 or not (payload(r) or {}).get("status"):          # PUT only updates an existing id
        r = api("POST", f"2.1/stringResources/{entity}/resource/{resource_id}", body, allow=(400, 404, 409, 500))
    ok = status_of(r) == 200
    run.m["lastConfigWriteAt"] = time.time()
    run.mutate("USER_string", entity=entity, resource=resource_id, text=text, ok=ok)
    log(f"  string {entity} {resource_id} = {text!r} ({'ok' if ok else f'HTTP {status_of(r)}'})", "INFO" if ok else "WARN")
    return ok


def apply_user_configs(run: Run, configs: dict[str, dict[str, str]]) -> None:
    uuid = run.m["uuid"]
    for ct, vals in configs.items():
        post_config(run, uuid, ct, vals, "USER")
        cur = read_config("user", uuid, ct)
        for k, v in vals.items():
            got = cfg(cur, k)
            shown = got if len(got) < 90 else f"{got[:60]}... ({len(got)} chars)"
            log(f"  readback {ct}.{k} = {shown} ({cur.get(k, {}).get('configSource')})", "INFO" if got == v else "WARN")


def configure(run: Run, fuels: tuple[str, ...]) -> None:
    step("CONFIGURE user-scope solar + hybrid flags (new user only)")
    apply_user_configs(run, user_configs(fuels))
    if "GAS" in fuels and run.m.get("msMode") == "combined" and run.m.get("meterRefs"):
        configure_combined_ms(run)          # resumed run: re-apply (idempotent) so new settings reach the user


def config_cache_settle(run: Run, seconds: int = 60) -> None:
    """Config and string reads are cached by the notification/emailer services: an email triggered seconds after
    a write renders with a mix of old and new values (seen 2026-09-29: electric kWh, gas $)."""
    wait = int(run.m.get("lastConfigWriteAt", 0) + seconds - time.time())
    if wait > 0:
        log(f"waiting {wait}s for the services' config cache to pick up the user-scope writes")
        time.sleep(wait)


def configure_combined_ms(run: Run) -> None:
    """User-scope configs for the ONE combined dual-fuel Monthly Summary. Applied once the meters exist (the
    user's DCX section list depends on its clusters) and before RAW/INVOICE trigger any notification pass."""
    log("combined dual-fuel Monthly Summary: ONE email for ELECTRIC + GAS (user scope)")
    apply_user_configs(run, combined_df_configs(run.m["uuid"]))
    for fuel, strings in COMBINED_DF_STRINGS.items():
        for rid, text in strings.items():
            put_user_string(run, f"{run.m['uuid']}.MONTHLY_SUMMARY.{fuel}", rid, text)
    run.m["combinedMsConfigured"] = True
    run.save()


# optional hook run after the user exists and the generic configure, before any METERENROLL/RAW/INVOICE is ingested
AFTER_USER_CREATED = None
LAST_RUN_DIR: Path | None = None
# optional hook run after disagg converged, right before the notifications-enabled AGG (the system Monthly Summary pass)
BEFORE_FINAL_AGG = None
# optional hook run right after the transition phase applied the plan switch (before round 2)
AFTER_TRANSITION = None


def do_upload(run: Run, a: argparse.Namespace, ctx: dict[str, Any], fuels: tuple[str, ...], with_configure: bool) -> None:
    step("UPLOAD in dependency order, each confirmed through API state")
    m, s3, bucket, tz = run.m, ctx["s3"], ctx["bucket"], ZoneInfo(run.m["timezone"])
    ident, files, cyc = m["identity"], m["files"], m["cycles"]

    upload(run, s3, bucket, Path(files["BC_SCHEDULE"]))
    want = [midnight(date.fromisoformat(x), tz) for x in m["bcNewMonths"]]
    if want:
        def bc_ok():
            res = payload(api("GET", f"2.1/utilityBillingCycles/utility/{PILOT_ID}")) or {}
            rows = res.get("data", []) if isinstance(res, dict) else res
            have = {int(r["validFrom"]) for r in rows if str(r.get("billCycleCode")) == m["billCycleCode"]}
            miss = [w for w in want if w not in have]
            return not miss, f"{len(want) - len(miss)}/{len(want)} new schedule rows"
        poll("BC_SCHEDULE", bc_ok, a.bc_wait, a.poll)
    else:
        time.sleep(20)

    upload(run, s3, bucket, Path(files["USERENROLL"]))
    info = poll("USERENROLL -> new user", lambda: (bool(u := find_uuid(ident["externalUserId"])), f"uuid={u}"),
                a.user_wait, a.poll)
    m["uuid"] = info.split("=", 1)[1]
    mds_target.pin_user(m["uuid"])               # later user-scope writes may only name this user
    set_uuid_tag(m["uuid"])
    run.save()
    log(f"NEW USER CREATED uuid={m['uuid']} externalUserId={ident['externalUserId']} email={ident['email']}")
    dashboard_link(run, "user created")
    log_details(run, "USER DETAILS (created)")
    write_hints(run)
    if getattr(a, "hold_ms", False):
        hold_ms(run, fuels)
    if with_configure:
        configure(run, fuels)
    if AFTER_USER_CREATED:
        AFTER_USER_CREATED(run)             # e.g. TOU: user-scope email config BEFORE the system-triggered events fire

    upload(run, s3, bucket, Path(files["METERENROLL"]))

    def meters_ok():
        am = all_meters(m["uuid"])
        found = {k: am[t] for k, t in ident["tokens"].items() if t in am}
        home = payload(api("GET", f"meta/users/{m['uuid']}/homes/{HID}")) or {}
        ok = len(found) == len(ident["tokens"]) and home.get("ratePlanId") == m["ratePlan"]
        m["meterRefs"] = {k: {"gid": v["gid"], "mid": v["mid"], "endpointId": v["gateway"].get("endpointId"),
                              "streamType": v["meter"].get("streamType")} for k, v in found.items()}
        return ok, f"meters {sorted(found)} of {sorted(ident['tokens'])}, home ratePlanId={home.get('ratePlanId')}"
    poll("METERENROLL stream meters", meters_ok, a.meter_wait, a.poll)
    run.save()
    if getattr(a, "transition_at", "upload") == "upload":
        apply_transition(run, a, s3, bucket, home_api_fallback=a.transition_home_api_fallback)
    if "GAS" in fuels and m.get("msMode") == "combined" and with_configure:
        configure_combined_ms(run)

    raw = m["raw"]
    for key, ref in (("RAW_D", "ELECTRIC:DELIVERED"), ("RAW_R", "ELECTRIC:RECEIVED"), ("RAW_G", "GAS:DELIVERED")):
        if key not in files:
            continue
        upload(run, s3, bucket, Path(files[key]))

        def raw_ok(ref=ref):
            mt = all_meters(m["uuid"]).get(ident["tokens"][ref], {}).get("meter", {})
            f_, l_ = mt.get("firstDataTimestamp"), mt.get("lastDataTimestamp")
            ok = f_ not in (None, "", "0") and l_ not in (None, "", "0") and \
                int(f_) <= raw["firstTs"] + 3600 and int(l_) >= raw["lastTs"] - 3600
            return ok, f"{ref} first={f_} last={l_} (want {raw['firstTs']}..{raw['lastTs']})"
        poll(f"{key} ingestion", raw_ok, a.raw_wait, a.poll)

    upload(run, s3, bucket, Path(files["INVOICE"]))
    t0, t1 = cyc[0]["startTs"] - 86400, int(time.time()) + 86400

    def inv_ok():
        parts = []
        done = True
        for fuel in fuels:
            ud = utility_data(m["uuid"], fuel, t0, t1)
            key = "E" if fuel == "ELECTRIC" else "G"
            n = sum(1 for c in cyc if (h := near(ud, c["startTs"])) and not h[1].get("bidgelyGeneratedInvoice")
                    and abs(inv_cost(h[1], fuel) - c[key]["total"]) < 0.02)
            parts.append(f"{fuel} {n}/{len(cyc)}")
            done &= n == len(cyc)
        return done, "invoiced cycles " + ", ".join(parts)
    poll("INVOICE ingestion", inv_ok, a.invoice_wait, a.poll)
    if m.get("budgetBilling"):
        import mds_bb
        mds_bb.ingest_program(run, a, s3, bucket)
    check_markers(run, s3)
    m["ingestedAt"] = time.time()
    run.save()


def schedule_entries(home: dict) -> list[dict]:
    raw = home.get("ratesSchedule") or "[]"
    try:
        return json.loads(raw) if isinstance(raw, str) else list(raw)
    except ValueError:
        return []


def apply_transition(run: Run, a: argparse.Namespace, s3, bucket: str, *, home_api_fallback: bool) -> None:
    """Rate transition: a METERENROLL row carrying the new plan and its RATE_PLAN_EFFECTIVE_DATE (the launchpad
    product flow: LaunchpadMeterCreator.handleBCAndRateTransitionDuringUserUpdation -> updateRatePlans).
    The ingester resolves plan NAMES through SQLRatesDao.getPilotRatePlanInformation, a per-JVM cache of
    eUtilityPlans that nothing invalidates: a plan created after the ingester JVM started is unknown to it and the
    row cannot move the home. Then the same schedule is written through POST meta/users/{uuid}/homes/1 (the
    ratesSchedule + plannumber fields the ingester would have set), and the manifest records which path applied."""
    m = run.m
    t = m.get("transition")
    if not t or m.get("transitionApplied"):
        return
    via = getattr(a, "transition_via", "meterenroll")
    path = f"meta/users/{m['uuid']}/homes/{HID}"

    def ok():
        h = payload(api("GET", path)) or {}
        plans = [str((e.get("metaData") or {}).get("planNumber")) for e in schedule_entries(h)]
        done = str(h.get("plannumber")) == str(t["planNumber"]) and str(t["planNumber"]) in plans
        return done, f"home {h.get('ratePlanId')}/{h.get('plannumber')} schedule plans {plans}"
    how = "ingestion"
    try:
        if via == "userenroll-flag":
            how = "ingestion (USERENROLL, handle_meter_enrollment=false window)"
            userenroll_flag_window(run, a, s3, bucket, Path(m["files"]["USERENROLL_T"]), ok, t)
        else:
            upload(run, s3, bucket, Path(m["files"]["USERENROLL_T" if via == "userenroll" else "METERENROLL_T"]))
            poll(f"{via.upper()} rate transition -> {t['plan']}", ok, a.transition_wait, a.poll)
    except WaitTimeout as exc:
        if not home_api_fallback:
            m["transitionFailed"] = {"at": time.time(), "reason": str(exc)[:300], "homeApiWrite": False}
            run.save()
            raise SetupError(f"{exc}. The ingester did not apply {t['plan']}: ingestion-only transition, so NO home API "
                             "schedule write and no notification trigger were made")
        log(f"{exc}. The ingester did not apply {t['plan']} (plan created after its JVM loaded the target's plan list?): "
            "writing the same rate schedule through the home API", "WARN")
        before = payload(api("GET", path)) or {}
        # every other fuel's entries are kept verbatim: a dual-fuel home must not lose its GAS schedule
        others = [e for e in schedule_entries(before) if e.get("measurementType") != "ELECTRIC"]
        sched = others + [
            {"rateType": "SQL", "startTime": 0, "endTime": t["fromTs"], "metaData": {"planNumber": str(t["fromPlanNumber"])},
             "measurementType": "ELECTRIC"},
            {"rateType": "SQL", "startTime": t["fromTs"], "endTime": 2147483647,
             "metaData": {"planNumber": str(t["planNumber"])}, "measurementType": "ELECTRIC"}]
        api("POST", path, {"ratesSchedule": json.dumps(sched, separators=(",", ":")),
                           "plannumber": str(t["planNumber"]), "ratePlanId": t["plan"]})
        run.mutate("home_rate_schedule", path=path, before={k: before.get(k) for k in ("ratesSchedule", "plannumber", "ratePlanId")},
                   after=sched)
        poll(f"home API rate transition -> {t['plan']}", ok, 180, 15)
        how = "home API (ingester plan cache)"
        # the ingester would also have raised RATE_EVALUATOR + TOU_RATE_EVALUATOR: DCX emails read the STORED
        # GLOBAL.TOU_ELECTRIC attribute (EventProcessorUtils.checkUserAttributes), still false from creation on 905
        raise_user_attributes(run, a, ["RATE_EVALUATOR", "TOU_RATE_EVALUATOR"])
    m["transitionApplied"] = {"at": time.time(), "via": how}
    h = payload(api("GET", path)) or {}
    m["homeAtTransition"] = {"at": time.time(), "solarUser": h.get("solarUser"), "plannumber": h.get("plannumber")}
    run.save()
    log(f"RATE TRANSITION applied via {how}")


FLAG_KEY = "handle_meter_enrollment"


def userenroll_flag_window(run: Run, a: argparse.Namespace, s3, bucket: str, f: Path, ok, t: dict) -> None:
    """The one product path that publishes TOU_ONBOARDING on the target pilot today: LaunchpadUserCreator.updateRatePlans on a
    USERENROLL update, which only runs while launchpad_ingestion_configs.handle_meter_enrollment=false (the target pilot keeps
    it true: METERENROLL owns meters + rate plans, and LaunchpadMeterCreator publishes nothing until the
    TouOnboardingEmailTrigger fix is deployed). PILOT-LEVEL: opened only for this one file, backed up first, always
    restored, and recorded in the manifest (pilotFlagWindow) so an interrupted run can be repaired with
    --restore-pilot-flag."""
    prev = run.m.get("pilotFlagWindow") or {}
    if prev and not prev.get("closedAt"):
        raise SetupError(f"an earlier {FLAG_KEY} window of this run is unresolved ({prev}): repair it first with "
                         "--restore-pilot-flag; no new window opened")
    orig = cfg(read_config("pilot", PILOT_ID, "launchpad_ingestion_configs"), FLAG_KEY)
    if orig != "true":
        # never adopt another run's open window (or a failed read) as "the original": restoring it would leave the
        # pilot with USERENROLL owning meters + rate plans
        raise SetupError(f"pilot {PILOT_ID} {FLAG_KEY} reads {orig!r}, expected 'true' (another run's window open, or the "
                         "config could not be read): no window opened, nothing written")
    bk = backup(run, "launchpad_ingestion_configs")
    run.m["pilotFlagWindow"] = {"key": FLAG_KEY, "original": orig, "openedAt": time.time(), "backup": str(bk)}
    run.save()
    try:
        post_config(run, PILOT_ID, "launchpad_ingestion_configs", {FLAG_KEY: "false"}, "PILOT")
        poll(f"pilot {FLAG_KEY}=false readback",
             lambda: (cfg(read_config("pilot", PILOT_ID, "launchpad_ingestion_configs"), FLAG_KEY) == "false", "API"),
             120, 10)
        log(f"waiting {a.flag_settle}s for the ingester's pilot config cache to see {FLAG_KEY}=false")
        time.sleep(a.flag_settle)
        upload(run, s3, bucket, f)
        try:
            poll(f"USERENROLL rate transition -> {t['plan']}", ok, a.flag_transition_wait, a.poll)
        except WaitTimeout:
            # the ingester may still have served the cached flag: same-name re-uploads are reprocessed
            log(f"{f.name} did not move the home yet (ingester flag cache?): re-uploading once", "WARN")
            upload(run, s3, bucket, f)
            poll(f"USERENROLL rate transition (re-upload) -> {t['plan']}", ok, a.flag_transition_wait, a.poll)
    finally:
        restore_pilot_flag(run)


def restore_pilot_flag(run: Run, attempts: int = 3, wait: int = 10) -> None:
    """Put the pilot flag back. The window is marked closed ONLY after a readback equal to the original; otherwise it
    stays unresolved in the manifest (so every later call retries) and SetupError carries the exact repair steps."""
    w = run.m.get("pilotFlagWindow") or {}
    if not w or w.get("closedAt"):
        return
    want, got = w["original"], None
    for i in range(1, attempts + 1):
        try:
            post_config(run, PILOT_ID, "launchpad_ingestion_configs", {FLAG_KEY: want}, "PILOT")
            got = cfg(read_config("pilot", PILOT_ID, "launchpad_ingestion_configs"), FLAG_KEY)
        except Exception as exc:  # noqa: BLE001 -- API outage: keep trying, then report unresolved
            got = f"<error: {str(exc)[:120]}>"
        w.setdefault("restoreAttempts", []).append({"at": time.time(), "readback": got})
        if got == want:
            w["closedAt"], w["restoredValue"] = time.time(), got
            w.pop("unresolved", None)
            run.save()
            log(f"pilot {FLAG_KEY} restored to {got!r} (window {int(w['closedAt'] - w['openedAt'])}s, attempt {i})")
            return
        log(f"pilot {FLAG_KEY} restore attempt {i}/{attempts}: readback {got!r}, want {want!r}", "ERROR")
        if i < attempts:
            time.sleep(wait)
    w["unresolved"] = True
    run.save()
    raise SetupError(
        f"PILOT {PILOT_ID} launchpad_ingestion_configs.{FLAG_KEY} NOT restored: readback {got!r}, want {want!r} after "
        f"{attempts} attempts. Repair now: (1) POST /entities/{PILOT_ID}/configs "
        f'{{"configType":"launchpad_ingestion_configs","configKVs":[{{"configKey":"{FLAG_KEY}","configVal":"{want}",'
        f'"configDataType":"TEXT","configRegex":"(?s).*","configDocumentation":"restore"}}]}}, or re-run with '
        f"--manifest {run.out / 'manifest.json'} --restore-pilot-flag; (2) GET /entities/pilot/{PILOT_ID}/configs/"
        f"launchpad_ingestion_configs must show {FLAG_KEY}={want}; backup of the pre-window config: {w.get('backup')}")


def raise_user_attributes(run: Run, a: argparse.Namespace, types: list[str], settle: int = 60) -> None:
    """Re-evaluate the user's DCX attributes (UserAttributesEvent -> user-attributes-handler). The evaluators read
    live state (TouRateEvaluator: home schedule + uncached ERateDao; SolarEvaluator: home.solarUser); notifications
    read only the stored values, and there is no read API for them, so this waits `settle` seconds."""
    sqs = aws(getattr(a, "aws_profile", None)).client("sqs")
    sqs_send(run, sqs, ATTR_QUEUE, json.dumps({"uuid": run.m["uuid"], "userAttributeEvaluatorTypes": types}),
             "USER_ATTRIBUTES " + ",".join(types))
    log(f"waiting {settle}s for user-attributes-handler to store {types}")
    time.sleep(settle)


def check_markers(run: Run, s3) -> dict[str, str]:
    """Launchpad writes s3://MARKER_BUCKET/Error/<file>.processed after it finishes a file: zero bytes =
    every record accepted, otherwise a per-record rejection report. The marker job runs separately and can
    lag hours, so a missing marker is not a failure: each upload has already been confirmed through the
    API (user, meters, RAW bounds, invoices). Rejection reports are saved to logs/REJECT_<file>.txt."""
    out: dict[str, str] = {}
    for k in run.m["fileOrder"]:
        mk = MARKER_PREFIX + Path(run.m["files"][k]).name.rsplit(".", 1)[0] + ".processed"
        try:
            size = s3.head_object(Bucket=MARKER_BUCKET, Key=mk)["ContentLength"]
        except Exception:  # noqa: BLE001
            out[k] = "pending"
            continue
        out[k] = "accepted" if size == 0 else f"rejections ({size} bytes)"
        if size:
            (run.dir("logs") / f"REJECT_{k}.txt").write_bytes(s3.get_object(Bucket=MARKER_BUCKET, Key=mk)["Body"].read())
            log(f"ingestion marker {k}: {size}-byte REJECTION report -> logs/REJECT_{k}.txt (some records were dropped)", "WARN")
    run.m["markers"] = out
    run.save()
    done = {k: v for k, v in out.items() if v != "pending"}
    pending = [k for k, v in out.items() if v == "pending"]
    log(f"ingestion markers: {sum(v == 'accepted' for v in done.values())} accepted, "
        f"{sum(v != 'accepted' for v in done.values())} with rejections, {len(pending)} not written yet"
        + (f" ({', '.join(pending)})" if pending else "")
        + ". Informational: every file was already confirmed through the API above; markers are written by a "
          f"separate job and can lag hours. Re-check later: aws s3 ls s3://{MARKER_BUCKET}/{MARKER_PREFIX} | "
          f"grep MDS{run.m['identity']['runId']}")
    return out


def converge_state(run: Run, fuels: tuple[str, ...]) -> tuple[dict[str, bool], str]:
    m = run.m
    uuid, cyc, last = m["uuid"], m["cycles"], m["cycles"][-1]
    t0, t1 = cyc[0]["startTs"] - 86400, int(time.time()) + 86400
    flags, info = {}, []
    ec = streams_of(ctype(uuid, "energy_consumption", "/all-streams", "ELECTRIC", t0, t1))
    bc = streams_of(ctype(uuid, "billing_cost", "/all-streams", "ELECTRIC", t0, t1))
    n = sum(1 for c in cyc if (d := near(ec.get("DELIVERED", {}), c["startTs"])) and
            (r := near(ec.get("RECEIVED", {}), c["startTs"])) and close(float(d[1].get("value", 0)), c["E"]["dWh"], REL_TOL, 2)
            and close(float(r[1].get("value", 0)), c["E"]["rWh"], REL_TOL, 2)
            and (x := near(bc.get("DELIVERED", {}), c["startTs"])) and float(x[1].get("energyCharges") or 0) > 0)
    flags["E_agg"] = n == len(cyc)
    info.append(f"ELECTRIC agg {n}/{len(cyc)}")
    eid = m["meterRefs"]["ELECTRIC:DELIVERED"]["endpointId"]
    items = cycle_items(itemization(uuid, eid, last["startTs"], last["endTs"] - 1), last["startTs"], "ELECTRIC")
    flags["E_item"] = bool(items)
    info.append(f"ELECTRIC itemization {'yes' if items else 'no'}")
    if "GAS" in fuels:
        gc = streams_of(ctype(uuid, "energy_consumption", "/all-streams", "GAS", t0, t1))
        g = sum(1 for c in cyc if (h := near(gc.get("DELIVERED", {}), c["startTs"])) and
                close(float(h[1].get("value", 0)), c["G"]["wh"], REL_TOL, 2))
        flags["G_agg"] = g == len(cyc)
        info.append(f"GAS agg {g}/{len(cyc)}")
        gid = m["meterRefs"]["GAS:DELIVERED"]["endpointId"]
        gi = cycle_items(itemization(uuid, gid, last["startTs"], last["endTs"] - 1), last["startTs"], "GAS") or \
            cycle_items(itemization(uuid, eid, last["startTs"], last["endTs"] - 1), last["startTs"], "GAS")
        flags["G_item"] = bool(gi)
        info.append(f"GAS itemization {'yes' if gi else 'no'}")
    return flags, ", ".join(info)


def hybrid_ts(uuid: str, mt: str, bill_start: int, bill_end: int) -> int | None:
    """Epoch of the last SUCCESSFUL hybrid run for one cycle (BillCycleDetails.applianceConsumption.
    hybridSucessRunTimeStamp), or None when no endpoint exposes it. This is the only completion
    timestamp the APIs give for the disagg -> hybrid -> itemization chain; aggregates carry none."""
    urls = (f"v2.0/bill-analysis/users/{uuid}/homes/{HID}/billingcycle-details?billStart={bill_start}&measurementType={mt}",
            f"v2.0/bill-analysis/users/{uuid}/homes/{HID}?locale=en_US&billStart={bill_start}&billEnd={bill_end - 1}"
            f"&measurementType={mt}&useNormalCompareBillCycle=true")
    for u in urls:
        try:
            r = api("GET", u, allow=(400, 404, 500))
        except SetupError:
            continue
        if status_of(r) != 200:
            continue
        p = payload(r) or {}
        d = p.get("billingCycleDetails", p) if isinstance(p, dict) else {}
        v = ((d or {}).get("applianceConsumption") or {}).get("hybridSucessRunTimeStamp")
        if v:
            return int(v)
    return None


def completion_probe(run: Run, fuels: tuple[str, ...]) -> dict[str, int | None]:
    last = run.m["cycles"][-1]
    return {f: hybrid_ts(run.m["uuid"], f, last["startTs"], last["endTs"]) for f in fuels}


def merge_completion(seen: dict[str, int | None], probe: dict[str, int | None]) -> dict[str, int | None]:
    """Keep the NEWEST completion observed per fuel. A success timestamp only moves forward, but reads
    are served by several data-server pods with their own caches: seen live on 2026-09-28, a read one
    second after 1790617609 returned the older 1790617472. A lower later read is a stale replica."""
    for f, v in probe.items():
        if v is not None and (seen.get(f) is None or v > seen[f]):
            seen[f] = v
        seen.setdefault(f, None)
    return dict(seen)


def completion_state(after: int | None, before: int | None, sent_at: float) -> str:
    """Classify one second-resolution completion timestamp against the pre-trigger baseline and the
    (sub-second) trigger time. Only a run that advanced past BOTH, in a later second than the trigger,
    is 'verified'; the same second cannot be ordered against the trigger, so it stays unverified."""
    if after is None:
        return "unverified: completion timestamp not readable"
    if before is not None and after <= before:
        return "not observed: completion did not advance past the pre-trigger baseline"
    if after < int(sent_at):
        return "not observed: completion predates the trigger"
    if after == int(sent_at):
        return "unverified: completion in the same second as the trigger"
    return "verified"


def fresh_wait(run: Run, fuels: tuple[str, ...], need: Callable[[dict[str, bool]], bool], sent_at: float,
               check_fuels: tuple[str, ...], baseline: dict[str, int | None]) -> Callable[[], tuple[bool, str]]:
    """Poll fn: data converged per `need` AND, for every fuel in check_fuels whose completion timestamp
    is readable, it is 'verified' by completion_state. Pre-existing output (for example from the
    ingestion-triggered pipeline) cannot satisfy it."""
    seen: dict[str, int | None] = {}

    def fn() -> tuple[bool, str]:
        flags, info = converge_state(run, fuels)
        probe = merge_completion(seen, completion_probe(run, check_fuels))
        st = {f: completion_state(v, baseline.get(f), sent_at) for f, v in probe.items()}
        readable = {f for f, v in probe.items() if v is not None}
        ok = need(flags) and all(st[f] == "verified" for f in readable)
        return ok, f"{info}; hybrid completion {probe} baseline {baseline} trigger {sent_at:.1f}: {st}"
    fn.seen = seen          # newest completion observed while waiting, reused by record_trigger
    return fn


def record_trigger(run: Run, kind: str, sent_at: float, before: dict, fuels: tuple[str, ...], converged: bool,
                   observed: dict[str, int | None] | None = None) -> None:
    after = merge_completion(dict(observed or {}), completion_probe(run, fuels))
    ev = {"kind": kind, "sentAt": round(sent_at, 3), "before": before, "after": after, "dataConverged": converged}
    ev["completion"] = {f: completion_state(after[f], before.get(f), sent_at) for f in fuels}
    run.m.setdefault("triggerEvidence", []).append(ev)
    run.save()
    log(f"trigger {kind}: data converged={converged}, completion {ev['completion']}",
        "INFO" if all(v == "verified" for v in ev["completion"].values()) else "WARN")


def aggregate(run: Run, a: argparse.Namespace, ctx: dict[str, Any], fuels: tuple[str, ...]) -> None:
    step("AGGREGATE: disagg (V2 electric / V1 gas) -> aggregation with notifications -> itemization")
    m = run.m
    uuid, zipc = m["uuid"], m["identity"]["zip"]
    sqs = ctx.get("sqs") or aws(a.aws_profile).client("sqs")
    t0, t1 = m["cycles"][0]["startTs"], int(time.time())
    log(f"settling {a.settle}s so ingestion-triggered aggregation finishes first")
    time.sleep(a.settle)
    # After disagg only the ELECTRIC itemization (hybrid V2, PyAMI) can be fresh; GAS is hybrid V1,
    # computed inside the aggregation service on the notifications-enabled AGG that follows.
    e_only = ("ELECTRIC",)
    for attempt in range(1, a.attempts + 1):
        log(f"attempt {attempt}/{a.attempts}")
        before = completion_probe(run, fuels)
        log(f"completion snapshot before DISAGG: {before}")
        sent = time.time()
        for fuel in fuels:
            sqs_send(run, sqs, DISAGG_QUEUE, disagg_xml(uuid, fuel, zipc, t0, t1), f"DISAGG_{fuel}")
        ok = False
        w = fresh_wait(run, fuels, lambda f: f.get("E_item", False), sent, e_only, before)
        try:
            poll("disagg -> fresh hybrid itemization", w, a.disagg_resend_after, a.poll)
            ok = True
        except WaitTimeout:
            # No completion newer than the trigger was observed. That does not prove the message was
            # lost (the job may be slow); one resend is cheap and idempotent.
            log("no hybrid completion newer than the DISAGG trigger yet -- resending once", "WARN")
            sent = time.time()
            for fuel in fuels:
                sqs_send(run, sqs, DISAGG_QUEUE, disagg_xml(uuid, fuel, zipc, t0, t1), f"DISAGG_{fuel}_RESEND")
            w = fresh_wait(run, fuels, lambda f: f.get("E_item", False), sent, e_only, before)
            try:
                poll("disagg (resent) -> fresh hybrid itemization", w, a.disagg_wait, a.poll)
                ok = True
            except WaitTimeout as exc:
                log(str(exc), "WARN")
        record_trigger(run, f"DISAGG attempt {attempt}", sent, before, e_only, ok, w.seen)
        before = merge_completion(dict(w.seen), completion_probe(run, fuels))     # newest known = AGG baseline
        if getattr(a, "transition_at", "upload") == "before-final-agg" and not m.get("transitionApplied"):
            # a year of flat-plan history + itemization exists; now the customer moves to the TOU plan
            apply_transition(run, a, ctx["s3"], ctx["bucket"], home_api_fallback=a.transition_home_api_fallback)
        if BEFORE_FINAL_AGG:
            BEFORE_FINAL_AGG(run)
        sent = time.time()
        for fuel in fuels:
            sqs_send(run, sqs, AGG_QUEUE, agg_xml(uuid, fuel, FUEL_STREAMS[fuel], t0, t1), f"AGG_{fuel}")
        w = fresh_wait(run, fuels, lambda f: all(f.values()), sent, e_only, before)
        try:
            poll("aggregation + fresh itemization converged",
                 # freshness gates ELECTRIC only: the V2 timestamp was seen to advance on this AGG
                 # (2026-09-28, +36 s); whether V1 gas writes it is unverified, so gas is reported, not awaited
                 w, a.agg_wait, a.poll)
            record_trigger(run, f"AGG attempt {attempt}", sent, before, fuels, True, w.seen)
            m["aggregatedAt"] = time.time()
            run.save()
            return
        except WaitTimeout as exc:
            log(str(exc), "WARN")
            record_trigger(run, f"AGG attempt {attempt}", sent, before, fuels, False, w.seen)
    flags, info = converge_state(run, fuels)
    m["aggregateOutcome"] = info
    run.save()
    log(f"did not fully converge ({info}); validation will show exactly what is missing", "WARN")


def ms_rows(uuid: str, since: int) -> list[dict]:
    r = payload(api("GET", f"2.1/utility_notifications/users/{uuid}?from={since - 3600}&to={int(time.time()) + 3600}",
                    allow=(404,))) or {}
    rows = [n for n in (r.get("notificationsList") or []) if n.get("notificationType") == "MONTHLY_SUMMARY"
            and n.get("deliveryMode") == "Email"]
    return sorted(rows, key=lambda n: -int(n.get("generationTimestamp") or 0))


def email_text(htmlb: str) -> str:
    t = re.sub(r"<style.*?</style>", "", htmlb or "", flags=re.S)
    return html_lib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t)))


def email_fuels(title: str, text: str) -> set[str]:
    """Which fuel(s) a Monthly Summary email covers, from its subject and body."""
    blob = f"{title} {text}".lower()
    out = set()
    if "electric" in blob or " kwh" in blob:
        out.add("ELECTRIC")
    if re.search(r"\bgas\b|\bccf\b|therm", blob):
        out.add("GAS")
    return out


def collect_ms(run: Run, since: int) -> dict[str, list[dict]]:
    """Fetch every MS email since `since`, save each body, index by fuel covered."""
    m, uuid = run.m, run.m["uuid"]
    by_fuel: dict[str, list[dict]] = {}
    m["msNotifications"] = []
    for i, r in enumerate(ms_rows(uuid, since)):
        body = payload(api("GET", f"2.1/utility_notifications/notifications/{r['notificationId']}")) or {}
        htmlb = body.get("notificationBody") or ""
        f = run.dir("email") / f"MS_email_{i + 1}.html"
        f.write_text(htmlb)
        fu = email_fuels(r.get("notificationTitle") or "", email_text(htmlb))
        row = {k: r.get(k) for k in ("notificationId", "notificationTitle", "status", "deliveryDestination",
                                     "generationTimestamp", "notificationType", "deliveryMode")} | {
            "variant": (r.get("eventData") or {}).get("email_variant_code"), "fuels": sorted(fu), "file": str(f)}
        m["msNotifications"].append(row)
        for x in fu:
            by_fuel.setdefault(x, []).append(row)
    run.save()
    return by_fuel


def ms_state_path(uuid: str, fuel: str | None) -> str:
    """Sent-state path. The combined dual-fuel email writes its state WITHOUT a measurement type."""
    return f"notification/notificationStatus/{uuid}/{HID}/MONTHLY_SUMMARY/" + (f"{fuel}/" if fuel else "") + "Email"


def rewind_ms_state(run: Run, fuel: str | None) -> None:
    """Make the MS cycle sendable again. isEmailToBeSent only resends when sentbillStartTs < billStart (with
    allow_multiple_ms_emails_for_same_bc=false, as on the target pilot): zeroing sentCount alone does nothing, so
    billStartTs is rewound to the previous cycle start. POST is a whole-object replace."""
    m = run.m
    path = ms_state_path(m["uuid"], fuel)
    before = api("GET", path, allow=(404, 500))
    prev = m["cycles"][-2]["startTs"]
    api("POST", path, {"billStartTs": prev, "sent": 0, "sentCount": 0, "sentWithSurveyCount": 0})
    run.mutate("ms_state_rewind", path=path, before=before, billStartTs=prev)
    log(f"MS state {fuel or 'COMBINED'} rewound for resend: {json.dumps(before)[:120]} -> billStartTs={prev} sentCount=0")


def hold_ms(run: Run, fuels: tuple[str, ...]) -> None:
    """--hold-ms: mark the MS cycle as already sent BEFORE any data lands, so the aggregation-driven notification pass
    (sendNotifications=true, needed for hybrid itemization) does not send a Monthly Summary on its own. isEmailToBeSent
    only sends when the stored billStartTs < the cycle start, so billStartTs = MS cycle start + sentCount 1 blocks it.
    A caller that wants the MS later rewinds the state (rewind_ms_state) and triggers it, e.g. TOU Coaching."""
    m = run.m
    start = m["cycles"][-1]["startTs"]
    for fuel in ([None] if "GAS" in fuels and m.get("msMode") == "combined" else list(fuels)):
        path = ms_state_path(m["uuid"], fuel)
        api("POST", path, {"billStartTs": start, "sent": int(time.time()), "sentCount": 1, "sentWithSurveyCount": 0})
        run.mutate("ms_state_hold", path=path, billStartTs=start)
        log(f"MS held for {fuel or 'COMBINED'}: state marks cycle {m['cycles'][-1]['start']} sent, so the automatic "
            "aggregation-driven Monthly Summary is skipped (the caller sends it later)")
    m["msHeld"] = True
    run.save()


def notify(run: Run, a: argparse.Namespace, ctx: dict[str, Any], fuels: tuple[str, ...]) -> None:
    combined = "GAS" in fuels and run.m.get("msMode") == "combined"
    step(f"NOTIFY: Monthly Summary email for the last completed cycle ({' + '.join(fuels)}"
         f"{', ONE combined dual-fuel email' if combined else ''})")
    m = run.m
    uuid = m["uuid"]
    since = int(m.get("ingestedAt") or time.time() - 7200)
    sqs = ctx.get("sqs") or aws(a.aws_profile).client("sqs")
    if combined and not m.get("combinedMsConfigured"):
        configure_combined_ms(run)          # resumed run created before combined mode existed
    config_cache_settle(run)
    if getattr(a, "ms_resend", False):
        since = int(time.time())
        for f in ([None] if combined else list(fuels)):
            rewind_ms_state(run, f)

    def covered() -> tuple[bool, str]:
        # ms_rows looks back an hour before `since`; only emails generated at/after `since` count
        rows = [r for r in ms_rows(uuid, since) if int(r.get("generationTimestamp") or 0) // 1000 >= since]
        got: set[str] = set()
        for r in rows:
            b = payload(api("GET", f"2.1/utility_notifications/notifications/{r['notificationId']}")) or {}
            got |= email_fuels(r.get("notificationTitle") or "", email_text(b.get("notificationBody") or ""))
        return set(fuels) <= got, f"{len(rows)} MS email(s) covering {sorted(got) or 'none'} (need {list(fuels)})"

    done, info = covered()
    if getattr(a, "ms_resend", False):
        done = False                        # the state was just rewound: always send
    if done:
        log(f"already generated by the aggregation-driven notification pass: {info}")
    else:
        if combined:
            st = api("GET", ms_state_path(uuid, None), allow=(404, 500))
            if isinstance(st, dict) and st.get("billStartTs") == m["cycles"][-1]["startTs"] and (st.get("sentCount") or 0) > 0:
                # an aggregation-driven combined pass already sent for this cycle, before gas was ready:
                # the state blocks a second send, so rewind it (test user only)
                log(f"combined state already marks the MS cycle sent ({info}); rewinding for a complete email", "WARN")
                rewind_ms_state(run, None)
        # one explicit pass; MS processes every fuel it finds (ELECTRIC then GAS)
        sqs_send(run, sqs, NOTIF_QUEUE, json.dumps({"userId": uuid, "homeOrdinal": HID,
                                                    "metaData": {"EventName": "MONTHLY_SUMMARY"}}), "MS")
        try:
            poll("MONTHLY_SUMMARY email(s) for every fuel", covered, a.ms_wait, a.poll)
        except WaitTimeout as exc:
            log(f"{exc}. Check log_grep_hints.txt: notifications ('No invoice available', 'Delivery modes') and "
                "emailer ('Received exception', 'Unable to render')", "WARN")
    by_fuel = collect_ms(run, since)
    for n in m.get("msNotifications", []):
        log(f"MS email '{n['notificationTitle']}' variant={n['variant']} fuels={n['fuels']} "
            f"to={n['deliveryDestination']} -> {n['file']}")
    if combined:
        st = api("GET", ms_state_path(uuid, None), allow=(404, 500))
        log(f"MS notification state COMBINED (no measurement type): {json.dumps(st)[:200]}")
    for fuel in fuels:
        if not combined:
            st = api("GET", ms_state_path(uuid, fuel), allow=(404, 500))
            log(f"MS notification state {fuel}: {json.dumps(st)[:200]}")
        if fuel not in by_fuel:
            log(f"no Monthly Summary email covers {fuel}", "WARN")


def dashboard_link(run: Run, why: str) -> dict[str, Any]:
    """Passwordless dashboard URL for the run's user, generated by the platform and verified by
    deciphering it back to the uuid:
      GET /v2.0/user-auth/cipher?user-id={uuid}&pilot-id={pilot}   -> payload = user-hash
      GET /v2.0/user-auth/{user-hash}?pilot-id={pilot}             -> payload.userCipherDetails.userId
    The link is DASHBOARD_URL/dashboard?user-hash=<url-encoded hash>, the same form the platform puts in
    email CTAs. The decipher response also carries an OAuth token (tokenDetails): never logged or saved."""
    uuid = run.m["uuid"]
    r = api("GET", f"v2.0/user-auth/cipher?user-id={uuid}&pilot-id={PILOT_ID}", allow=(400, 404, 500))
    hsh = payload(r) if status_of(r) == 200 else None
    info: dict[str, Any] = {"generatedAt": datetime.now(timezone.utc).isoformat(), "reason": why}
    if not isinstance(hsh, str) or not hsh.strip():
        info["error"] = f"cipher API HTTP {status_of(r)}: {str((r or {}).get('error') if isinstance(r, dict) else r)[:200]}"
        log(f"DASHBOARD link could not be generated ({info['error']})", "WARN")
    else:
        hsh = hsh.strip().strip('"')
        enc = urllib.parse.quote(hsh, safe="")
        info.update(userHash=hsh, url=f"{DASHBOARD_URL}/dashboard?user-hash={enc}&locale=en_US")
        d = api("GET", f"v2.0/user-auth/{enc}?pilot-id={PILOT_ID}", allow=(400, 401, 403, 404, 500))
        det = ((payload(d) or {}) if status_of(d) == 200 else {}).get("userCipherDetails") or {}
        got = str(det.get("userId") or "")
        info["verified"] = got == uuid
        info["decipheredUserId"] = got or None
        if det.get("expiryTimeInMillis"):
            info["expiresAt"] = datetime.fromtimestamp(int(det["expiryTimeInMillis"]) / 1000, timezone.utc).isoformat()
        if not info["verified"]:
            err = (d.get("error") or {}) if isinstance(d, dict) else {}
            msg = re.sub(r"Error ID: [0-9a-f-]+\. ?", "", str(err.get("message") or ""))[:160]
            info["verifyError"] = f"decipher HTTP {status_of(d)} code={err.get('code')} message={msg!r}"
            # UserAuthenticationEndpoint decrypts + expiry-checks the hash BEFORE minting an OAuth token; this
            # message comes from the OAuth step, so the hash itself is valid (uuid just isn't returned).
            info["hashValid"] = "clientId is empty" in msg
        log(f"DASHBOARD URL ({why}; passwordless login for this test user"
            f"{', expires ' + info['expiresAt'] if info.get('expiresAt') else ''}; "
            f"{'verified -> uuid ' + uuid if info['verified'] else 'NOT verified: ' + info['verifyError']}):")
        log(f"  {info['url']}")
    run.m["dashboard"] = info
    run.save()
    return info


def write_hints(run: Run) -> None:
    """Log greps through the debug kit (profile-bound envlogs: hosts, replicas and paths come from the profile)."""
    m = run.m
    keys = [Path(f).name for f in m["files"].values()]
    lines = [f"# log greps for {run.out.name}  uuid={m.get('uuid')}  externalUserId={m['identity']['externalUserId']}",
             f"# export BIDGELY_PROFILE={mds_target.PROFILE_PATH}; E={SKILL_DIR}/scripts/envlogs", ""]
    for svc in ("ingestion", "aggregation", "aggregation-priority", "disagg", "disagg-priority", "notifications", "emailer"):
        needles = [m.get("uuid")] + (keys if svc == "ingestion" else [])
        lines += [f"## {svc}", f"$E grep {svc} '{'|'.join(n for n in needles if n)}'", ""]
    (run.dir("logs") / "log_grep_hints.txt").write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
@dataclass
class Check:
    cid: str
    name: str
    api: str
    status: str = "PASS"
    details: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.status = "FAIL"
        self.details.append("FAIL: " + msg)

    def warn(self, msg: str) -> None:
        if self.status == "PASS":
            self.status = "WARN"
        self.details.append("WARN: " + msg)

    def ok(self, msg: str) -> None:
        self.details.append(msg)


def mmddyy(d: date) -> str:
    return d.strftime("%m/%d/%y")


# ---------------------------------------------------------------------------
# Pure checks: no I/O, every input passed in, unit-tested in tests/test_validators.py
# ---------------------------------------------------------------------------
CENT = 0.005                # stored cent values must match exactly (half-cent slack is float noise only)
DQ_CODES = {0x000: "OK", 0x001: "DISCONNECT", 0x002: "SOLAR", 0x004: "DUPLICATE", 0x008: "NONUNIFORM",
            0x010: "TOO_HIGH", 0x020: "TOO_LOW", 0x040: "NO_CDD", 0x080: "NO_HDD", 0x100: "NOT_ENOUGH_DATA",
            0x120: "NO_DATA", 0x200: "NEW_USER", 0x400: "PERFECT_DATA", 0x800: "INCONSISTENT_DATA",
            0x900: "BLENDED_RATE_NOT_AVAILABLE"}          # core-api DataQualityIssue


def dq_name(code: Any) -> str:
    try:
        c = int(code)
    except (TypeError, ValueError):
        return f"unknown({code})"
    if c in DQ_CODES:
        return DQ_CODES[c]
    return "|".join(n for b, n in DQ_CODES.items() if b and b & (b - 1) == 0 and c & b) or f"unknown({c})"


def finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def envelope_ok(ch: Check, resp: Any, name: str) -> Any:
    """HTTP 200, no error object, non-empty payload -> payload, else None (and FAIL)."""
    if status_of(resp) != 200:
        ch.fail(f"{name} HTTP {status_of(resp)} {str((resp or {}).get('error') or '')[:150]}")
        return None
    if isinstance(resp, dict) and resp.get("error"):
        ch.fail(f"{name} error envelope {str(resp['error'])[:150]}")
        return None
    p = payload(resp)
    if not p:
        ch.fail(f"{name} empty payload")
        return None
    return p


def check_projection(ch: Check, bp: Any, g: Any, ong: dict) -> None:
    """Both projection endpoints: semantic status, exact ongoing-cycle bounds, numeric fields, agreement.
    Zero or negative projected cost is valid (net exporter); an unavailable status is not."""
    lp = envelope_ok(ch, bp, "billprojections")
    gp = envelope_ok(ch, g, "bill-projection-graph-data")
    l_ok = g_ok = False
    if isinstance(lp, dict):
        st = lp.get("projectionStatus")
        if st != 0: ch.fail(f"billprojections projectionStatus {st} {dq_name(st)} ({lp.get('projectionStatusText')})")
        if (lp.get("billStart"), lp.get("billEnd")) != (ong["startTs"], ong["endTs"]):
            ch.fail(f"billprojections period {lp.get('billStart')}..{lp.get('billEnd')} != ongoing {ong['startTs']}..{ong['endTs']}")
        for k in ("projectionPrice", "currentPrice", "projectionConsumption"):
            if not finite(lp.get(k)): ch.fail(f"billprojections {k}={lp.get(k)} not a number")
        l_ok = st == 0 and finite(lp.get("projectionPrice"))
    if isinstance(gp, dict):
        st = gp.get("projectionStatus")
        if st != 0: ch.fail(f"graph projectionStatus {st} {dq_name(st)}: projection unavailable "
                            f"(billProjectionCost {gp.get('billProjectionCost')})")
        if (gp.get("startTime"), gp.get("endtime")) != (ong["startTs"], ong["endTs"]):
            ch.fail(f"graph period {gp.get('startTime')}..{gp.get('endtime')} != ongoing {ong['startTs']}..{ong['endTs']}")
        for k in ("billProjectionCost", "billProjectionConsumption"):
            if not finite(gp.get(k)): ch.fail(f"graph {k}={gp.get(k)} not a number")
        cum = gp.get("cumulativeCost") or []
        if not cum: ch.fail("graph cumulativeCost empty")
        elif isinstance(lp, dict) and finite(lp.get("currentPrice")) and finite(cum[-1].get("value")) and \
                not close(cum[-1]["value"], lp["currentPrice"], 0, 0.05):
            ch.fail(f"cost-to-date disagrees: graph ${cum[-1]['value']:.2f} vs billprojections ${lp['currentPrice']:.2f}")
        g_ok = st == 0 and finite(gp.get("billProjectionCost"))
    if isinstance(lp, dict) and isinstance(gp, dict):
        if l_ok and g_ok and not close(gp["billProjectionCost"], lp["projectionPrice"], 0.02, 1.0):
            ch.fail(f"projections disagree: graph ${gp['billProjectionCost']:.2f} vs billprojections ${lp['projectionPrice']:.2f}")
        elif l_ok != g_ok:
            ch.fail(f"endpoints disagree on availability: billprojections {dq_name(lp.get('projectionStatus'))} "
                    f"${lp.get('projectionPrice')} vs graph {dq_name(gp.get('projectionStatus'))} ${gp.get('billProjectionCost')}")
    if l_ok and g_ok:
        ch.ok(f"ongoing {ong['start']}: projected ${lp['projectionPrice']:.2f} (graph ${gp['billProjectionCost']:.2f}), "
              f"to date ${lp['currentPrice']:.2f}, both status OK")


def check_usage_chart(ch: Check, res: Any, cyc: list[dict], ong: dict, zipc: str) -> None:
    """Year chart contract (observed 2026-09-28): one row per bill cycle over the trailing year plus
    the ongoing cycle, intervalEnd = cycle end - 1, consumption/stream cost rounded to whole units,
    energyCharges exact, the fixed charge carried on the DELIVERED stream cost."""
    rows = (res or {}).get("usageChartDataList") if isinstance(res, dict) else None
    if not rows:
        ch.fail("usageChartDataList empty")
        return
    keys = [int(r["intervalStart"]) for r in rows]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup: ch.fail(f"duplicate cycle rows {dup}")
    by = {int(r["intervalStart"]): r for r in rows}
    known = {c["startTs"]: c for c in cyc}
    year_ago = add_months(date.fromisoformat(ong["start"]), -12).isoformat()
    expected = [c for c in cyc if c["start"] >= year_ago]
    miss = [c["start"] for c in expected if c["startTs"] not in by]
    if miss: ch.fail(f"{len(miss)} expected in-window cycle(s) missing: {miss}")
    odd = [k for k in by if k not in known and k != ong["startTs"]]
    if odd: ch.fail(f"rows at unexpected cycle keys {odd} (shifted/unknown periods)")
    o = by.get(ong["startTs"])
    if not o: ch.fail(f"ongoing cycle {ong['start']} row missing")
    elif o.get("isOngoingInterval") is not True: ch.fail(f"ongoing row isOngoingInterval={o.get('isOngoingInterval')}")
    no_wx, n = [], 0
    for c in cyc:
        r = by.get(c["startTs"])
        if not r:
            continue
        n += 1
        e, s = c["E"], c["start"]
        if int(r.get("intervalEnd", -1)) != c["endTs"] - 1: ch.fail(f"{s} intervalEnd {r.get('intervalEnd')} != {c['endTs'] - 1}")
        sd = {x.get("streamType"): x for x in r.get("streamDetails") or []}
        if not {"DELIVERED", "RECEIVED"} <= set(sd):
            ch.fail(f"{s} streamDetails {sorted(map(str, sd))}")
        else:
            d, rr = sd["DELIVERED"], sd["RECEIVED"]
            if not finite(d.get("consumption")) or abs(d["consumption"] - e["dKwh"]) > 0.5 + 1e-6:
                ch.fail(f"{s} D {d.get('consumption')} kWh != {e['dKwh']}")
            if not finite(rr.get("consumption")) or abs(rr["consumption"] - e["rKwh"]) > 0.5 + 1e-6:
                ch.fail(f"{s} R {rr.get('consumption')} kWh != {e['rKwh']}")
            if not finite(d.get("cost")) or abs(d["cost"] - (e["dCost"] + e["fixed"])) > 1.0 + 1e-6:
                ch.fail(f"{s} D cost {d.get('cost')} != D ${e['dCost']} + fixed ${e['fixed']} (whole-dollar rounding)")
            if not finite(rr.get("cost")) or abs(rr["cost"] - e["rCost"]) > 1.0 + 1e-6:
                ch.fail(f"{s} R cost {rr.get('cost')} != ${e['rCost']} (whole-dollar rounding)")
        if not finite(r.get("energyCharges")) or not close(r["energyCharges"], e["energy"], 0, CENT):
            ch.fail(f"{s} energyCharges {r.get('energyCharges')} != {e['energy']}")
        if r.get("temperature") is None: no_wx.append(s)
    if no_wx: ch.fail(f"no weather (temperature) for zip {zipc} in {no_wx} -> bill analysis HDD/CDD NPE")
    ch.ok(f"{n}/{len(cyc)} completed cycle rows ({len(expected)} required in the trailing year from {year_ago}) + ongoing; "
          "D/R kWh, D/R cost, energy and weather per row")


def check_bill_analysis(ch: Check, ba: Any, last: dict) -> None:
    """useNormalCompareBillCycle=true compares the cycle with its weather-NORMALISED self
    (BillAnalysisDataManager:114): same period, billCycleType NORMAL_BILLING_CYCLE."""
    p = envelope_ok(ch, ba, "bill-analysis")
    if not isinstance(p, dict):
        return
    cur, cmp_ = p.get("billingCycleDetails") or {}, p.get("compareBillingCycleDetails") or {}
    if not cur: ch.fail("billingCycleDetails missing")
    if not cmp_: ch.fail("compareBillingCycleDetails missing")
    if not cur or not cmp_:
        return
    e, want = last["E"], (last["startTs"], last["endTs"])
    if (cur.get("billStart"), cur.get("billEnd")) != want: ch.fail(f"period {cur.get('billStart')}..{cur.get('billEnd')} != MS cycle {want}")
    if (cmp_.get("billStart"), cmp_.get("billEnd")) != want: ch.fail(f"comparison period {cmp_.get('billStart')}..{cmp_.get('billEnd')} != {want}")
    if cmp_.get("billCycleType") != "NORMAL_BILLING_CYCLE": ch.fail(f"comparison billCycleType {cmp_.get('billCycleType')} != NORMAL_BILLING_CYCLE")
    ub = cur.get("utilityBillingData") or {}
    if not close(float(ub.get("cost") or 0), e["total"], 0, CENT): ch.fail(f"invoice cost {ub.get('cost')} != {e['total']}")
    if not close(float(ub.get("value") or 0), e["dKwh"], 0, 0.002): ch.fail(f"invoice kWh {ub.get('value')} != {e['dKwh']}")
    lines = ub.get("invoiceDataList") or []
    tot = [x for x in lines if x.get("chargeType") == "TOTAL"]
    if len(tot) != 1: ch.fail(f"{len(tot)} TOTAL invoice lines")
    elif not close(sum(float(x.get("cost") or 0) for x in lines if x.get("chargeType") != "TOTAL"), float(tot[0].get("cost") or 0), 0, CENT):
        ch.fail("invoice lines do not add up to TOTAL")
    if not finite(cur.get("totalCost")) or not close(cur["totalCost"], e["energy"], 0, CENT):
        ch.fail(f"totalCost {cur.get('totalCost')} != invoice energy {e['energy']}")
    net_wh = e["dWh"] + e["rWh"]
    if not finite(cur.get("consumption")) or not close(cur["consumption"], net_wh, 0.001, 2):
        ch.fail(f"consumption {cur.get('consumption')} Wh != net D+R {net_wh:.0f} Wh")
    days = (date.fromisoformat(last["end"]) - date.fromisoformat(last["start"])).days
    if (cur.get("hddCddWrapper") or {}).get("totalDays") != days: ch.fail(f"hddCdd totalDays {(cur.get('hddCddWrapper') or {}).get('totalDays')} != {days}")
    iw = cur.get("itemizationWithCost") or {}
    if iw.get("startTs") != last["startTs"] or not iw.get("electric"): ch.fail("itemizationWithCost missing for the MS cycle")
    for k in ("totalCost", "consumption"):
        if not finite(cmp_.get(k)): ch.fail(f"comparison {k}={cmp_.get(k)} not a number")
    if not isinstance(p.get("billCompareReasons"), dict): ch.fail("billCompareReasons missing")
    if ch.status != "FAIL":
        ch.ok(f"MS cycle ${cur['totalCost']:.2f} / {cur['consumption'] / 1000:.1f} kWh net vs weather-normal "
              f"${cmp_['totalCost']:.2f} / {cmp_['consumption'] / 1000:.1f} kWh; invoice ${ub.get('cost')}")


def parse_invoice_file(path: str | Path) -> dict[tuple[str, str, str], dict[str, dict]]:
    """(fuel, start, end) -> chargeName -> {type, usage, cost}, read straight from the generated file."""
    out: dict[tuple[str, str, str], dict[str, dict]] = {}
    for ln in Path(path).read_text().splitlines():
        f = ln.split("|")
        if len(f) < 13:
            continue
        key = (f[4], f[6], f[7])
        if f[9] in out.setdefault(key, {}):
            raise ValueError(f"duplicate {f[9]} line for {key}")
        out[key][f[9]] = {"type": f[10], "usage": float(f[11]), "cost": float(f[12])}
    return out


def parse_raw_file(path: str | Path, tz: ZoneInfo) -> tuple[dict[int, float], int]:
    """interval-start ts (as the ingester resolves the label) -> value, and the duplicate count."""
    vals: dict[int, float] = {}
    dups = 0
    for ln in Path(path).read_text().splitlines():
        f = ln.split("|")
        if len(f) < 6:
            continue
        ts = platform_ts(f[4], tz) - INTERVAL
        dups += ts in vals
        vals[ts] = float(f[5])
    return vals, dups


def cycle_sums(vals: dict[int, float], cyc: list[dict]) -> list[float]:
    out = [0.0] * len(cyc)
    for ts, v in vals.items():
        for i, c in enumerate(cyc):
            if c["startTs"] <= ts < c["endTs"]:
                out[i] += v
                break
    return out


def check_raw_coverage(ch: Check, streams: dict[str, dict[int, float]], dups: dict[str, int], m: dict,
                       tz: ZoneInfo) -> None:
    """Per stream over the whole run window: expected intervals, gaps, duplicates, bounds, per-cycle
    counts. Intervals the local DATE format cannot express are reported, never counted as covered."""
    first, lastts, cyc = m["raw"]["firstTs"], m["raw"]["lastTs"], m["cycles"]
    grid = range(first, lastts + INTERVAL, INTERVAL)
    unrep = {ts for ts in grid if raw_label(ts, tz) is None}
    for name, vals in streams.items():
        got = set(vals)
        missing = set(grid) - got
        extra = got - set(grid)
        if dups.get(name): ch.fail(f"{name}: {dups[name]} duplicate interval(s)")
        if extra: ch.fail(f"{name}: {len(extra)} interval(s) outside {first}..{lastts}, e.g. {sorted(extra)[:3]}")
        real = sorted(missing - unrep)
        if real: ch.fail(f"{name}: {len(real)} gap(s), first {[datetime.fromtimestamp(t, tz).isoformat() for t in real[:4]]}")
        for i, c in enumerate(cyc):
            exp = (c["endTs"] - c["startTs"]) // INTERVAL
            n = sum(1 for t in got if c["startTs"] <= t < c["endTs"])
            u = sum(1 for t in unrep if c["startTs"] <= t < c["endTs"])
            if n != exp - u: ch.fail(f"{name} {c['start']}: {n} rows, expected {exp - u} ({exp} - {u} unrepresentable)")
        ch.ok(f"{name}: {len(got)}/{len(grid)} intervals, 0 gaps beyond format limits, 0 duplicates, "
              f"bounds {datetime.fromtimestamp(min(got), tz):%Y-%m-%d %H:%M}..{datetime.fromtimestamp(max(got), tz):%Y-%m-%d %H:%M}")
    recorded = {x["startTs"] for x in m["raw"].get("unrepresentable") or []}
    if "unrepresentable" in m["raw"] and recorded != unrep:
        ch.fail(f"manifest records {len(recorded)} unrepresentable intervals, recomputed {len(unrep)}")
    if unrep:
        ch.warn(f"continuous coverage INCOMPLETE: {len(unrep)} interval(s) per stream in the first fall-back hour "
                f"({', '.join(datetime.fromtimestamp(t, tz).isoformat() for t in sorted(unrep))}) cannot be written: "
                "raw_data_date_type=DATE labels carry no UTC offset and the ingester resolves repeated local times "
                "to standard time")


def check_stored_stream(ch: Check, ref: str, rows: list[dict], want: dict[int, float], tz: ZoneInfo) -> None:
    """Stored readings (gb.json rows over the whole window, time = interval END) == the generated file
    for one stream: no missing, extra or duplicate intervals, and every value equal. Duplicates are
    counted on the raw row list, before it is keyed by time."""
    times = [int(x["time"]) - INTERVAL for x in rows if "time" in x]
    dup = len(times) - len(set(times))
    got = {int(x["time"]) - INTERVAL: float(x["value"]) for x in rows if "time" in x}
    miss, extra = sorted(set(want) - set(got)), sorted(set(got) - set(want))
    bad = sorted(t for t in want if t in got and not close(got[t], want[t], 1e-4, 0.01))
    fmt = lambda ts: [datetime.fromtimestamp(t, tz).isoformat() for t in ts[:3]]
    if dup: ch.fail(f"{ref} stored: {dup} duplicate interval(s)")
    if miss: ch.fail(f"{ref} stored: {len(miss)} interval(s) missing, first {fmt(miss)}")
    if extra: ch.fail(f"{ref} stored: {len(extra)} unexpected interval(s), first {fmt(extra)}")
    if bad: ch.fail(f"{ref} stored: {len(bad)} value(s) differ, first {fmt(bad)} got {got[bad[0]]} want {want[bad[0]]}")
    if not (dup or miss or extra or bad):
        ch.ok(f"{ref} stored: all {len(got)} intervals present once and equal to the file")


_FLOW_TOKEN = re.compile(r'<img[^>]*\bsrc="([^"]*)"|<p[^>]*>\s*(-?[\d,]+(?:\.\d+)?)\s*kWh\s*</p>', re.S | re.I)


def parse_energy_flow(htmlb: str) -> dict[str, Any]:
    """The SOLAR_ENERGY_FLOW block of a Monthly Summary (solar_energy_flow_section[_v2].vm):
    {'solar': generation kWh (between the generation and home icons), 'grid': grid kWh (between the
    home and grid icons), 'net': signed headline (+ import 'You used N more than you generated',
    - export 'You generated N more than you used')}. Values are None when absent."""
    out: dict[str, Any] = {"found": False, "solar": None, "grid": None, "net": None, "title": None}
    mt = re.search(r'<tr[^>]*\bid="SOLAR_ENERGY_FLOW"', htmlb or "")
    if not mt:
        return out
    nxt = re.search(r'<tr[^>]*\bid="', htmlb[mt.end():])
    block = htmlb[mt.start(): mt.end() + (nxt.start() if nxt else len(htmlb))]
    out["found"] = True
    h3 = re.search(r"<h3[^>]*>(.*?)</h3>", block, re.S)
    title = email_text(h3.group(1)).strip() if h3 else ""
    out["title"] = title
    num = lambda x: float(x.replace(",", ""))
    if (t := re.fullmatch(r"You used ([\d,]+(?:\.\d+)?) kWh more than you generated last period", title)):
        out["net"] = num(t.group(1))
    elif (t := re.fullmatch(r"You generated ([\d,]+(?:\.\d+)?) kWh more than you used last period", title)):
        out["net"] = -num(t.group(1))
    seq = []
    for t in _FLOW_TOKEN.finditer(block):
        if t.group(1):
            src = t.group(1).rsplit("/", 1)[-1].lower()
            icon = "solar" if "generation" in src else "home" if src.startswith("home") else "grid" if src.startswith("grid") else None
            if icon:
                seq.append(("icon", icon))
        else:
            seq.append(("val", num(t.group(2))))
    pos = {v: i for i, (k, v) in enumerate(seq) if k == "icon"}
    def between(a: str, b: str) -> float | None:
        if a not in pos or b not in pos:
            return None
        lo, hi = sorted((pos[a], pos[b]))
        vals = [v for k, v in seq[lo + 1:hi] if k == "val"]
        return vals[0] if len(vals) == 1 else None
    out["solar"], out["grid"] = between("solar", "home"), between("home", "grid")
    return out


def check_energy_flow(ch: Check, htmlb: str, solar_kwh: float, net_kwh: float) -> None:
    """Energy-flow block values (integers in the email) against the itemization solar row and the
    invoice net (D + FEED_IN). Tolerance 1 kWh: the email truncates or rounds to whole kWh."""
    f = parse_energy_flow(htmlb)
    if not f["found"]:
        ch.fail("email has no SOLAR_ENERGY_FLOW section")
        return
    if f["net"] is None: ch.fail(f"energy-flow headline not an import/export statement: '{f['title']}'")
    elif abs(f["net"] - net_kwh) > 1.0: ch.fail(f"energy-flow headline net {f['net']:+g} kWh != invoice net {net_kwh:+.1f}")
    if f["solar"] is None: ch.fail("energy-flow solar generation value missing")
    elif abs(f["solar"] - solar_kwh) > 1.0: ch.fail(f"energy-flow solar {f['solar']:g} kWh != itemization solar {solar_kwh:.1f}")
    if f["grid"] is None: ch.fail("energy-flow grid value missing")
    elif abs(f["grid"] - abs(net_kwh)) > 1.0: ch.fail(f"energy-flow grid {f['grid']:g} kWh != |net| {abs(net_kwh):.1f}")
    if f["net"] is not None and f["grid"] is not None and f["grid"] != abs(f["net"]):
        ch.fail(f"energy-flow grid {f['grid']:g} != headline {abs(f['net']):g}")
    if f["net"] is not None and f["solar"] is not None and f["grid"] is not None and ch.status != "FAIL":
        ch.ok(f"energy flow: solar {f['solar']:g} kWh, grid {f['grid']:g} kWh, net {f['net']:+g} kWh")


def check_invoice_lines(ch: Check, fuel: str, ud: dict, inv: dict, cyc: list[dict]) -> None:
    """Every persisted invoice line == the generated INVOICE file line (name, type, usage, cost), on the
    exact cycle period (no nearest-bucket matching), and no extra persisted lines."""
    by_period = {(int(v.get("billingStartTs", -1)), int(v.get("billingEndTs", -1))): v
                 for v in (ud or {}).values() if isinstance(v, dict)}
    for c in cyc:
        s = c["start"]
        want = inv.get((fuel, c["start"], c["end"]))
        if not want:
            ch.fail(f"{s}: no {fuel} lines in the generated INVOICE file")
            continue
        v = by_period.get((c["startTs"], c["endTs"]))
        if not v:
            near_ = [k for k in by_period if abs(k[0] - c["startTs"]) <= 86400]
            ch.fail(f"{s}: no persisted invoice on exactly {c['startTs']}..{c['endTs']}" + (f" (found {near_})" if near_ else ""))
            continue
        if v.get("bidgelyGeneratedInvoice"): ch.fail(f"{s}: bidgelyGeneratedInvoice")
        got = {}
        for x in v.get("invoiceDataList") or []:           # utilitydata is already per measurementType
            nm =x.get("chargeNameString") or x.get("chargeName")
            if nm in got: ch.fail(f"{s}: duplicate persisted line {nm}")
            got[nm] = x
        if set(got) != set(want): ch.fail(f"{s}: persisted lines {sorted(map(str, got))} != file {sorted(want)}")
        for nm, w in want.items():
            x = got.get(nm)
            if not x:
                continue
            if x.get("chargeType") != w["type"]: ch.fail(f"{s} {nm}: chargeType {x.get('chargeType')} != {w['type']}")
            cu = float(x.get("consumption") or 0)
            ok_use = close(cu, w["usage"], 0, 0.0015) or (fuel == "GAS" and close(cu, w["usage"] * CCF_TO_WH / 1000, 0.0005, 0.01))
            if not ok_use: ch.fail(f"{s} {nm}: usage {cu} != file {w['usage']}")
            if not close(float(x.get("cost") or 0), w["cost"], 0, 0.005): ch.fail(f"{s} {nm}: cost {x.get('cost')} != file {w['cost']}")
        t = want.get("TOTAL")
        if t:
            if not close(inv_cost(v, fuel), t["cost"], 0, 0.005): ch.fail(f"{s}: invoice cost {inv_cost(v, fuel)} != TOTAL {t['cost']}")
            if not close(sum(w["cost"] for k, w in want.items() if k != "TOTAL"), t["cost"], 0, 0.011):
                ch.fail(f"{s}: file lines do not add up to TOTAL")
    ch.ok(f"{fuel}: every persisted line of {len(cyc)} invoices == generated file (name/type/usage/cost, exact period)")


def check_invoice_vs_raw(ch: Check, fuel: str, inv: dict, sums: dict[str, list[float]], unit_scale: float,
                         agg: dict[str, dict], cyc: list[dict]) -> None:
    """Independent reconciliation for every cycle: generated INVOICE file usage == sums parsed from the
    generated RAW files == the platform's per-stream ENERGY_CONSUMPTION aggregates (exact cycle key).
    unit_scale converts RAW file units to invoice units (Wh -> kWh = 0.001, ccf -> ccf = 1)."""
    pairs = [("CONSUMPTION_BASED", "DELIVERED")] + ([("FEED_IN", "RECEIVED")] if fuel == "ELECTRIC" else [])
    for i, c in enumerate(cyc):
        lines = inv.get((fuel, c["start"], c["end"])) or {}
        for line, stream in pairs:
            raw_sum = sums[stream][i]
            if line not in lines:
                ch.fail(f"{c['start']}: no {line} line")
                continue
            if not close(lines[line]["usage"], raw_sum * unit_scale, 0, 0.0015):
                ch.fail(f"{c['start']} {line} {lines[line]['usage']} != RAW {stream} {raw_sum * unit_scale:.3f}")
            b = (agg.get(stream) or {}).get(str(c["startTs"]))
            want_wh = raw_sum * (CCF_TO_WH if fuel == "GAS" else (1.0 if unit_scale == 0.001 else 1000.0))
            if not b:
                ch.fail(f"{c['start']}: no {stream} aggregate at exactly {c['startTs']}")
            elif not close(float(b.get("value", 0)), want_wh, 0.001, 2):
                ch.fail(f"{c['start']} {stream} aggregate {float(b.get('value', 0)):.0f} Wh != RAW {want_wh:.0f} Wh")
        if "TOTAL" in lines and "CONSUMPTION_BASED" in lines and not close(lines["TOTAL"]["usage"], lines["CONSUMPTION_BASED"]["usage"], 0, 0.0005):
            ch.fail(f"{c['start']}: TOTAL usage != CONSUMPTION_BASED usage")
    ch.ok(f"{fuel}: {len(cyc)} cycles, invoice file == RAW files (parsed independently) == stream aggregates")


def check_items(ch: Check, label: str, items: list[dict] | None, c: dict, energy: float,
                solar_expected: bool, net_kwh: float | None) -> tuple | None:
    """Invoice-backed PRESENTATION reconciliation (totalFromInvoice=true): required rows, finite values,
    appliance sum == total row == invoice energy, and for solar total = net + solar estimate.
    It does not measure disaggregation accuracy."""
    s = c["start"]
    if not items:
        ch.fail(f"{label} {s}: itemization empty")
        return None
    ids = [int(float(i.get("id", -1))) for i in items]
    if len(ids) != len(set(ids)): ch.fail(f"{label} {s}: duplicate appliance ids")
    bad = [i.get("category") for i in items if not finite(i.get("usage")) or not finite(i.get("cost"))]
    if bad: ch.fail(f"{label} {s}: non-numeric usage/cost for {bad}")
    apps, total, solar = split_items(items)
    if not total: ch.fail(f"{label} {s}: required total row (id 17) missing")
    if solar_expected and not solar: ch.fail(f"{label} {s}: required solar row (id 16) missing")
    if not solar_expected and solar: ch.fail(f"{label} {s}: non-solar home has a solar row (id 16): {solar.get('usage')}")
    if not apps: ch.fail(f"{label} {s}: no appliances besides total/solar")
    neg = [i.get("category") for i in apps if float(i.get("cost") or 0) < 0 or float(i.get("usage") or 0) < 0]
    if neg: ch.fail(f"{label} {s}: negative appliance usage/cost {neg}")
    s_cost = sum(float(i.get("cost") or 0) for i in apps)
    s_use = sum(float(i.get("usage") or 0) for i in apps)
    if total:
        if not close(s_cost, float(total.get("cost") or 0), 0, CENT): ch.fail(f"{label} {s}: sum(appliances) ${s_cost:.2f} != total row ${float(total.get('cost') or 0):.2f}")
        if not close(s_use, float(total.get("usage") or 0), 0, 0.05): ch.fail(f"{label} {s}: usage sum {s_use:.2f} != total {total.get('usage')}")
        if not close(float(total.get("cost") or 0), energy, 0, CENT): ch.fail(f"{label} {s}: total ${float(total.get('cost') or 0):.2f} != invoice energy ${energy:.2f}")
        if solar and net_kwh is not None:
            tu, su = float(total.get("usage") or 0), float(solar.get("usage") or 0)
            if su < 0: ch.fail(f"{label} {s}: negative solar estimate {su}")
            if not close(tu, net_kwh + su, 0.003, 1.0):
                ch.fail(f"{label} {s}: energy flow total {tu:.1f} kWh != net {net_kwh:.1f} + solar {su:.1f}")
    return apps, total, solar, s_cost


def check_widget_items(ch: Check, label: str, widget_items: list[dict], api_items: list[dict]) -> None:
    """ID-matched cross-surface check: the widget shows each appliance rounded to whole kWh and dollars."""
    w = {int(float(i["id"])): i for i in widget_items}
    a = {int(float(i["id"])): i for i in split_items(api_items)[0]}
    a = {k: v for k, v in a.items() if k in w or float(v.get("usage") or 0) or float(v.get("cost") or 0)}  # widget hides zero rows
    if set(w) != set(a): ch.fail(f"{label} widget appliance ids {sorted(w)} != itemization API {sorted(a)}")
    # The widget rounds sum-preservingly (95.52 kWh shows as 95 so the rows still add to the rounded
    # total), so a row may differ by < 1 unit, but the rounded rows must add up to the rounded API sum.
    for f in ("usage", "cost"):
        for k in sorted(set(w) & set(a)):
            wv, av = w[k].get(f), float(a[k].get(f) or 0)
            if not finite(wv) or abs(wv - av) >= 1.0:
                ch.fail(f"{label} {w[k].get('category')} {f} widget {wv} != API {av:.2f} (sum-preserving whole-unit rounding)")
        ws = sum(float(w[k].get(f) or 0) for k in w)
        as_ = sum(float(a[k].get(f) or 0) for k in a)
        if abs(ws - round(as_)) > 1.0 + 1e-6:
            ch.fail(f"{label} widget {f} rows add to {ws:g}, API rows to {as_:.2f}")


def check_sent_state(ch: Check, fuel: str, row: dict, state: Any, last: dict, since: int, email: str) -> None:
    """Separates three claims. Generated: a notification record for this event, fuel, recipient and
    run. Application-sent: current notificationStatus for the MS cycle with sentCount >= 1, written
    by this run. Delivered: not observable here (deliveryStatus is always {}), so never claimed."""
    gen = int(row.get("generationTimestamp") or 0) // 1000
    if row.get("notificationType", "MONTHLY_SUMMARY") != "MONTHLY_SUMMARY": ch.fail(f"event {row.get('notificationType')}")
    if row.get("deliveryMode", "Email") != "Email": ch.fail(f"deliveryMode {row.get('deliveryMode')}")
    if row.get("deliveryDestination") != email: ch.fail(f"sent to {row.get('deliveryDestination')} not {email}")
    if row.get("status") not in ("GENERATED", "SENT", "DELIVERED"): ch.fail(f"notification status {row.get('status')}")
    if gen < since: ch.fail(f"notification generated {gen} before this run's data landed ({since}): stale")
    if status_of(state) != 200 or not isinstance(state, dict):
        ch.fail(f"notificationStatus {fuel} read failed: HTTP {status_of(state)}")
        return
    st = payload(state) if isinstance(payload(state), dict) else state
    if st.get("billStartTs") != last["startTs"]: ch.fail(f"sent-state billStartTs {st.get('billStartTs')} != MS cycle {last['startTs']}")
    if not isinstance(st.get("sentCount"), int) or st["sentCount"] < 1: ch.fail(f"sent-state sentCount {st.get('sentCount')}")
    sent = int(st.get("sent") or 0)
    if sent < since: ch.fail(f"sent-state 'sent' {sent} predates this run ({since})")
    elif gen and abs(sent - gen) > 900: ch.fail(f"sent-state 'sent' {sent} is {sent - gen:+d}s from the notification generation {gen}")
    if ch.status != "FAIL":
        ch.ok(f"claim = generated + application-sent: record {row.get('status')} at {gen}, sent-state billStartTs "
              f"{st['billStartTs']} sentCount {st['sentCount']} sent {sent}; inbox delivery NOT verified")


def check_trigger_evidence(ch: Check, m: dict, fuels: tuple[str, ...]) -> None:
    ev = m.get("triggerEvidence") or []
    if not ev:
        ch.warn("no trigger-completion evidence in the manifest (aggregate phase not run with completion tracking): "
                "current data is checked, but completion of the triggered jobs is UNVERIFIED")
        return
    for kind in ("DISAGG", "AGG"):
        e = next((x for x in reversed(ev) if x["kind"].startswith(kind + " ")), None)
        if not e:
            ch.warn(f"no {kind} trigger recorded")
            continue
        for f, v in e["completion"].items():
            (ch.ok if v == "verified" else ch.warn)(f"{e['kind']} {f}: {v} (sent {e['sentAt']}, hybrid ts {e['after'].get(f)})")


def validate(run: Run, fuels: tuple[str, ...]) -> list[Check]:
    step(f"VALIDATE ({' + '.join(fuels)})")
    m = run.m
    uuid, ident, cyc, ong = m["uuid"], m["identity"], m["cycles"], m["ongoing"]
    last = cyc[-1]
    refs = m["meterRefs"]
    eid = refs["ELECTRIC:DELIVERED"]["endpointId"]
    t0, t1 = cyc[0]["startTs"] - 86400, int(time.time()) + 86400
    checks: list[Check] = []
    cache: dict[str, Any] = {}
    tz = ZoneInfo(m["timezone"])

    def gen_files() -> dict[str, Any]:
        """The generated launchpad files, parsed independently of the manifest's own sums."""
        if "files" not in cache:
            inv = parse_invoice_file(m["files"]["INVOICE"])
            streams, dups = {}, {}
            for key, name in (("RAW_D", "ELECTRIC:DELIVERED"), ("RAW_R", "ELECTRIC:RECEIVED"), ("RAW_G", "GAS:DELIVERED")):
                if key in m["files"]:
                    streams[name], dups[name] = parse_raw_file(m["files"][key], tz)
            cache["files"] = {"inv": inv, "streams": streams, "dups": dups,
                              "sums": {k: cycle_sums(v, cyc) for k, v in streams.items()}}
        return cache["files"]

    def run_check(cid: str, name: str, api_desc: str, fn: Callable[[Check], None]) -> None:
        ch = Check(cid, name, api_desc)
        try:
            fn(ch)
        except SetupError as exc:
            ch.fail(f"API error: {str(exc)[:300]}")
        except Exception as exc:  # noqa: BLE001
            ch.fail(f"{type(exc).__name__}: {exc}")
        checks.append(ch)
        log(f"[{ch.status:<4}] {cid:<4} {name}" + (f" -- {ch.details[-1]}" if ch.details else ""))

    # ---------------- ELECTRIC ----------------
    def e01(ch):
        u = payload(api("GET", f"v2.0/users/{uuid}"))
        run.evidence("E01_user", u)
        if u.get("pilotId") != PILOT_ID: ch.fail(f"pilotId {u.get('pilotId')}")
        if u.get("status") != "ENABLED": ch.fail(f"status {u.get('status')}")
        if u.get("partnerUserId") != ident["externalUserId"]: ch.fail(f"partnerUserId {u.get('partnerUserId')}")
        ch.ok(f"uuid {uuid} {u.get('status')} partnerUserId {u.get('partnerUserId')}")
    run_check("E01", "User created and enabled", "GET /v2.0/users/{uuid}", e01)

    def e02(ch):
        h = payload(api("GET", f"meta/users/{uuid}/homes/{HID}"))
        run.evidence("E02_home", h)
        t = m.get("transition") or {}
        want_plan, want_num = t.get("plan") or m["ratePlan"], t.get("planNumber") or m["planNumbers"]["ELECTRIC"]
        if h.get("ratePlanId") != want_plan: ch.fail(f"ratePlanId {h.get('ratePlanId')} != {want_plan} (last enrollment row must be ELECTRIC)")
        if str(h.get("plannumber")) != str(want_num): ch.fail(f"plannumber {h.get('plannumber')} != {want_num}")
        if str(h.get("solarUser")).lower() == "true": ch.fail(f"non-solar home has solarUser {h.get('solarUser')}")
        if str(h.get("billCycleCode")) != m["billCycleCode"]: ch.fail(f"billCycleCode {h.get('billCycleCode')}")
        ch.ok(f"plan {h.get('ratePlanId')}/{h.get('plannumber')} solarUser {h.get('solarUser')} BCC {h.get('billCycleCode')} tz {h.get('timezone')}")
    run_check("E02", "Home: rate plan, non-solar, bill cycle code", "GET /meta/users/{uuid}/homes/1", e02)

    def e03(ch):
        am = all_meters(uuid)
        run.evidence("E03_meters", am)
        base = f"{ident['gatewayToken']}_{ident['meters']['ELECTRIC']}"
        active = {t: v["meter"].get("streamType") for t, v in am.items() if t.startswith(base) and not t.endswith(":inactive")}
        if active.get(ident["tokens"]["ELECTRIC:DELIVERED"]) != "DELIVERED": ch.fail(f"DELIVERED meter missing: {active}")
        if active.get(ident["tokens"]["ELECTRIC:RECEIVED"]) != "RECEIVED": ch.fail(f"RECEIVED meter missing: {active}")
        if any("-R-R" in t for t in am): ch.fail("-R-R duplicate meter")
        if "DELIVERED_AND_RECEIVED" in active.values(): ch.fail("legacy DELIVERED_AND_RECEIVED meter active")
        gw = am.get(ident["tokens"]["ELECTRIC:DELIVERED"], {}).get("gateway", {})
        if str(gw.get("type")) != "75" or gw.get("decommissioned") == "true": ch.fail(f"AMI gateway type {gw.get('type')} decommissioned {gw.get('decommissioned')}")
        ch.ok(f"active electric streams {sorted(set(active.values()))}, gateway gid {refs['ELECTRIC:DELIVERED']['gid']}")
    run_check("E03", "AMI gateway + one D and one -R stream meter", "GET /meta/.../gws/ and /gws/{gid}/meters/", e03)

    def e04(ch):
        rows = payload(api("GET", f"user-attributes/v1.0/users/{uuid}/attributes")) or []
        run.evidence("E04_attributes", rows)
        vals = {r["attributeId"]: (r.get("attributeValues") or [{}])[-1].get("value") for r in rows}
        cache["attrs"] = vals
        if vals.get("GLOBAL.AMI_ELECTRIC") is not True: ch.fail(f"GLOBAL.AMI_ELECTRIC={vals.get('GLOBAL.AMI_ELECTRIC')}")
        if vals.get("GLOBAL.SOLAR") is True: ch.fail("non-solar user has GLOBAL.SOLAR=True")
        ch.ok(f"AMI_ELECTRIC={vals.get('GLOBAL.AMI_ELECTRIC')} SOLAR={vals.get('GLOBAL.SOLAR')}")
    run_check("E04", "User attributes AMI_ELECTRIC, SOLAR not true", "GET /user-attributes/v1.0/users/{uuid}/attributes", e04)

    def raw_sample(ch, ref, key, sign):
        vals = {int(k): v for k, v in m["sample"]["values"][key].items()}
        ts = sorted(vals)
        r = refs[ref]
        rows = api("GET", f"streams/users/{uuid}/homes/{HID}/gws/{r['gid']}/meters/{r['mid']}/gb.json?t0={ts[0] + INTERVAL}&t1={ts[-1] + INTERVAL}")
        rows = rows if isinstance(rows, list) else []
        run.evidence(f"RAW_{ref.replace(':', '_')}", rows)
        # gb.json reports each reading at its interval END (= the RAW label = start + 900);
        # meter first/last timestamps and aggregates use the interval start.
        raw_got = {int(x["time"]): float(x["value"]) for x in rows if "time" in x}
        got = {t - INTERVAL: v for t, v in raw_got.items()}
        miss = [t for t in vals if t not in got]
        bad = [t for t in vals if t in got and not close(got[t], vals[t], 1e-4, 0.01)]
        if miss: ch.fail(f"{ref}: {len(miss)}/{len(vals)} intervals missing on {m['sample']['date']}")
        if bad: ch.fail(f"{ref}: {len(bad)} values differ, e.g. ts {bad[0]} got {got[bad[0]]} want {vals[bad[0]]}")
        if [v for v in got.values() if v * sign < -1e-9]: ch.fail(f"{ref}: wrong-sign values")
        if not miss and not bad:
            ch.ok(f"{ref}: {len(vals)} intervals on {m['sample']['date']} match generated RAW exactly (gb.json time = interval end)")

    def e05(ch):
        raw_sample(ch, "ELECTRIC:DELIVERED", "D", +1)
        raw_sample(ch, "ELECTRIC:RECEIVED", "R", -1)
    run_check("E05", "RAW per stream stored exactly (D >= 0, R <= 0)", "GET /streams/.../meters/{mid}/gb.json", e05)

    def e06(ch):
        gf = gen_files()
        check_raw_coverage(ch, gf["streams"], gf["dups"], m, tz)
        lt = api("GET", f"streams/users/{uuid}/homes/{HID}/labelTimestamps/ELECTRIC/Default")
        run.evidence("E06_label_ts", lt)
        if int(lt.get("first", 0)) > m["raw"]["firstTs"]: ch.fail(f"stored first {lt.get('first')} > {m['raw']['firstTs']}")
        if int(lt.get("last", 0)) < m["raw"]["lastTs"]: ch.fail(f"stored last {lt.get('last')} < {m['raw']['lastTs']}")
        # stored readings over the WHOLE window, per stream, fetched in bounded chunks (gb.json time =
        # interval END, both bounds inclusive): every stored interval is compared, DST days included
        lo, hi = m["raw"]["firstTs"], m["raw"]["lastTs"] + INTERVAL
        e_scale = 1.0 if m["units"]["ELECTRIC"].lower() == "wh" else 1000.0
        for ref, vals in gf["streams"].items():
            r, rows = refs[ref], []
            scale = CCF_TO_WH if ref.startswith("GAS") else e_scale
            t_start, chunks = time.time(), -(-(hi - lo) // STORED_CHUNK)
            for k, a_ in enumerate(range(lo, hi, STORED_CHUNK), 1):
                b_ = min(a_ + STORED_CHUNK, hi)
                if k == 1 or k % 10 == 0 or k == chunks:
                    log(f"  E06 {ref}: fetching stored chunk {k}/{chunks}, {len(rows)} rows so far, {time.time() - t_start:.0f}s")
                part = api("GET", f"streams/users/{uuid}/homes/{HID}/gws/{r['gid']}/meters/{r['mid']}/gb.json"
                                  f"?t0={a_ + INTERVAL}&t1={b_}")
                rows += part if isinstance(part, list) else []
            run.evidence(f"E06_stored_{ref.replace(':', '_')}", rows)
            check_stored_stream(ch, ref, rows, {t: v * scale for t, v in vals.items()}, tz)
        ch.ok(f"stored interval bounds {lt.get('first')}..{lt.get('last')}")
    run_check("E06", "Interval coverage per stream (gaps, duplicates, DST days, cycle counts)",
              "generated RAW files + GET /streams/.../labelTimestamps + gb.json (whole window, chunked)", e06)

    def bcs(mt: str) -> list[tuple[int, int]]:
        if mt not in cache:
            r = api("GET", f"billingdata/users/{uuid}/homes/{HID}/billingcycles?t0=1&t1=2147483647&measurementType={mt}")
            cache[mt] = sorted((int(x["key"]), int(x["value"])) for x in (r if isinstance(r, list) else []))
        return cache[mt]

    def check_cycles(ch, mt):
        b = bcs(mt)
        run.evidence(f"BILLING_CYCLES_{mt}", b)
        have = dict(b)
        for c in cyc:
            if have.get(c["startTs"]) != c["endTs"]:
                ch.fail(f"{mt} cycle {c['start']}..{c['end']} expected ({c['startTs']},{c['endTs']}) got end {have.get(c['startTs'])}")
        mine = [x for x in b if x[0] >= cyc[0]["startTs"]]
        for (s1, e1), (s2, _) in zip(mine, mine[1:]):
            if s2 < e1: ch.fail(f"{mt} overlap at {s2}")
            elif s2 > e1: ch.fail(f"{mt} gap {e1}..{s2}")
        if not any(abs(s - ong["startTs"]) <= 86400 for s, _ in b): ch.warn(f"{mt} ongoing cycle {ong['start']} not listed yet")
        ch.ok(f"{mt}: {len(cyc)} completed cycles exact and continuous, last completed (MS cycle) {last['start']}..{last['end']}")

    run_check("E07", "Billing cycles exact, continuous, last completed = MS cycle", "GET /billingdata/.../billingcycles", lambda ch: check_cycles(ch, "ELECTRIC"))

    def e08(ch):
        ud = utility_data(uuid, "ELECTRIC", t0, t1)
        cache["udE"] = ud
        run.evidence("E08_utilitydata_ELECTRIC", ud)
        check_invoice_lines(ch, "ELECTRIC", ud, gen_files()["inv"], cyc)
        for v in ud.values():
            if isinstance(v, dict) and v.get("billingStartTs") in {c["startTs"] for c in cyc}:
                c = next(c for c in cyc if c["startTs"] == v["billingStartTs"])
                if not close(float(v.get("costForItemization") or 0), c["E"]["energy"], 0, CENT):
                    ch.fail(f"{c['start']} costForItemization {v.get('costForItemization')} != D + FEED_IN {c['E']['energy']}")
        le = last["E"]
        ch.ok(f"MS cycle {last['start']}: total ${le['total']} = D ${le['dCost']} + FEED_IN ${le['rCost']} + fixed ${le['fixed']}")
    run_check("E08", "Persisted invoice lines == generated INVOICE file (every line, every cycle)",
              "generated INVOICE + GET /billingdata/.../utilitydata?measurementType=ELECTRIC", e08)

    def e09(ch):
        gf = gen_files()
        ec = streams_of(ctype(uuid, "energy_consumption", "/all-streams", "ELECTRIC", t0, t1))
        cache["ecE"] = ec
        scale = 0.001 if m["units"]["ELECTRIC"].lower() == "wh" else 1.0
        check_invoice_vs_raw(ch, "ELECTRIC", gf["inv"], {"DELIVERED": gf["sums"]["ELECTRIC:DELIVERED"],
                                                          "RECEIVED": gf["sums"]["ELECTRIC:RECEIVED"]}, scale, ec, cyc)
    run_check("E09", "Invoice file == RAW files == stream aggregates (every cycle, independent parse)",
              "generated INVOICE + RAW_D/RAW_R + GET .../ctypes/energy_consumption/all-streams", e09)

    def e10(ch):
        ec = streams_of(ctype(uuid, "energy_consumption", "/all-streams", "ELECTRIC", t0, t1))
        cache["ecE"] = ec
        run.evidence("E10_energy_all_streams", ec)
        for c in cyc:
            d, r = near(ec.get("DELIVERED", {}), c["startTs"]), near(ec.get("RECEIVED", {}), c["startTs"])
            if not d or not r:
                ch.fail(f"{c['start']} missing stream bucket")
                continue
            dv, rv = float(d[1].get("value", 0)), float(r[1].get("value", 0))
            if not close(dv, c["E"]["dWh"], REL_TOL, 2): ch.fail(f"{c['start']} D {dv:.0f} != RAW {c['E']['dWh']:.0f} Wh")
            if not close(rv, c["E"]["rWh"], REL_TOL, 2): ch.fail(f"{c['start']} R {rv:.0f} != RAW {c['E']['rWh']:.0f} Wh")
            if abs(dv - rv) < 1e-6: ch.fail(f"{c['start']} D == R: legacy (non stream-aware) storage")
        ch.ok(f"MS cycle D {last['E']['dWh']:.0f} Wh, R {last['E']['rWh']:.0f} Wh")
    run_check("E10", "ENERGY_CONSUMPTION per stream == RAW", "GET .../ctypes/energy_consumption/all-streams", e10)

    def e11(ch):
        bc = streams_of(ctype(uuid, "billing_cost", "/all-streams", "ELECTRIC", t0, t1))
        cache["bcE"] = bc
        run.evidence("E11_billing_cost_all_streams", bc)
        for c in cyc:
            d, r = near(bc.get("DELIVERED", {}), c["startTs"]), near(bc.get("RECEIVED", {}), c["startTs"])
            if not d or not r:
                ch.fail(f"{c['start']} missing cost bucket")
                continue
            de, re_ = float(d[1].get("energyCharges") or 0), float(r[1].get("energyCharges") or 0)
            if not close(de, c["E"]["dCost"], REL_TOL, 0.05):
                ch.fail(f"{c['start']} D ${de:.2f} != ${c['E']['dCost']:.2f}" + (" (1000x: RAW unit)" if de and close(de * 1000, c['E']['dCost'], 0.05, 1) else ""))
            if not close(re_, c["E"]["rCost"], REL_TOL, 0.05): ch.fail(f"{c['start']} R ${re_:.2f} != ${c['E']['rCost']:.2f}")
        ch.ok(f"MS cycle D ${last['E']['dCost']} R ${last['E']['rCost']}")
    run_check("E11", "BILLING_COST per stream == RAW x rate plan", "GET .../ctypes/billing_cost/all-streams", e11)

    def e12(ch):
        nb = ctype(uuid, "billing_cost", "/net-billable", "ELECTRIC", t0, t1, extra="&streamTypes=DELIVERED&streamTypes=RECEIVED")
        run.evidence("E12_net_billable", nb)
        for c in cyc:
            n = near(nb, c["startTs"])
            if not n or not close(float(n[1].get("energyCharges") or 0), c["E"]["energy"], 0.002, 0.05):
                ch.fail(f"{c['start']} net-billable {n and n[1].get('energyCharges')} != D+R {c['E']['energy']}")
        ch.ok("net-billable == DELIVERED + RECEIVED == invoice energy for every cycle")
    run_check("E12", "Net-billable cost == D + R", "GET .../ctypes/billing_cost/net-billable", e12)

    def e13(ch):
        st = payload(api("GET", f"v2.0/disaggregation/users/{uuid}/homes/{HID}/all-bc-app-aggregation-status")) or []
        run.evidence("E13_hybrid_status", st)
        ok = {int(x["startTs"]) for x in st if x.get("hybridDisaggRunSuccessfully")}
        miss = [c["start"] for c in cyc if c["startTs"] not in ok]
        if miss: ch.fail(f"hybrid not successful for {miss}")
        ch.ok(f"hybridDisaggRunSuccessfully for {len(cyc) - len(miss)}/{len(cyc)} cycles")
        check_trigger_evidence(ch, m, fuels)
    run_check("E13", "Hybrid status per cycle + triggered-job completion evidence",
              "GET .../all-bc-app-aggregation-status + manifest triggerEvidence (hybridSucessRunTimeStamp)", e13)

    def item_rule(ch, fuel, entries, c, energy, label):
        net = (c["E"]["dKwh"] + c["E"]["rKwh"]) if fuel == "ELECTRIC" else None
        return check_items(ch, label, cycle_items(entries, c["startTs"], fuel), c, energy,
                           solar_expected=False, net_kwh=net)

    def e14(ch):
        entries = itemization(uuid, eid, last["startTs"], last["endTs"] - 1)
        run.evidence("E14_itemization_ms_cycle", entries)
        r = item_rule(ch, "ELECTRIC", entries, last, last["E"]["energy"], "ELECTRIC")
        if r:
            apps, total, solar, s = r
            ch.ok(f"MS cycle {len(apps)} appliances sum ${s:.2f} == total ${float(total['cost']) if total else s:.2f} == invoice energy "
                  f"${last['E']['energy']} (D ${last['E']['dCost']} + FEED_IN ${last['E']['rCost']}); "
                  f"solar {solar and round(float(solar['usage']), 1)} kWh; DELIVERED-only energy ${last['E']['dCost']} for reference")
    run_check("E14", "Itemization presentation (MS cycle): rows, sum == total == invoice energy, solar flow", "GET /v2.0/users/{uuid}/endpoints/{eid}/itemizationDetails", e14)

    def e15(ch):
        entries = itemization(uuid, eid, cyc[0]["startTs"], last["endTs"] - 1)
        run.evidence("E15_itemization_all_cycles", entries)
        n = 0
        for c in cyc:
            if item_rule(ch, "ELECTRIC", entries, c, c["E"]["energy"], "ELECTRIC"):
                n += 1
        if n != len(cyc): ch.fail(f"only {n}/{len(cyc)} cycles itemized")
        ch.ok(f"{n}/{len(cyc)} cycles: required rows present, appliances == total == invoice energy (totalFromInvoice=true)")
    run_check("E15", "Itemization presentation, every cycle (invoice-backed, not model accuracy)", "GET .../itemizationDetails (all cycles)", e15)

    def e16(ch):
        res = payload(api("GET", f"v2.0/dashboard/users/{uuid}/usage-chart-details?measurement-type=ELECTRIC&mode=year"))
        run.evidence("E16_usage_chart_year", res)
        check_usage_chart(ch, res, cyc, ong, ident["zip"])
    run_check("E16", "Usage chart (year): every in-window cycle, D/R usage+cost, energy, weather",
              "GET /v2.0/dashboard/users/{uuid}/usage-chart-details", e16)

    def widget(ch, mt, energy, fixed, streams_required):
        w = payload(api("GET", f"v2.0/dashboard/users/{uuid}/usage-widget-data?measurement-type={mt}&date-format=MONTH_DAY_SPACE&locale=en_US"))
        run.evidence(f"WIDGET_{mt}", w)
        if abs(int(w.get("billStart", 0)) - last["startTs"]) > 86400: ch.fail(f"{mt} billStart {w.get('billStart')} != MS cycle {last['startTs']}")
        bd = w.get("billCostBreakdowns") or {}
        en = float((bd.get("energyCharges") or {}).get("chargeValue") or 0)
        fx = float((bd.get("fixedCharges") or {}).get("chargeValue") or 0)
        if not close(en, energy, 0, DOLLAR_TOL): ch.fail(f"{mt} energyCharges {en} != {energy}")
        if not close(fx, fixed, 0, DOLLAR_TOL): ch.fail(f"{mt} fixedCharges {fx} != {fixed}")
        if not close(float(w.get("cost") or 0), en + fx, 0, DOLLAR_TOL): ch.fail(f"{mt} cost {w.get('cost')} != {en + fx:.2f}")
        if streams_required:
            st = {s.get("streamType") for s in w.get("streamDetails") or []}
            if not {"DELIVERED", "RECEIVED"} <= st: ch.fail(f"{mt} streamDetails {sorted(map(str, st))}")
        ch.ok(f"{mt}: cost {w.get('cost')} = energy {en} + fixed {fx} for {w.get('billStartDateFormatted')}")

    run_check("E17", "Bill summary widget (MS cycle)", "GET /v2.0/dashboard/users/{uuid}/usage-widget-data",
              lambda ch: widget(ch, "ELECTRIC", last["E"]["energy"], last["E"]["fixed"], True))

    def api_items(fuel: str) -> list[dict]:
        """MS-cycle itemization for `fuel` from the itemization API (gas may live on either endpoint)."""
        k = f"items_{fuel}"
        if k not in cache:
            endpoint = refs[f"{fuel}:DELIVERED"]["endpointId"]
            cache[k] = cycle_items(itemization(uuid, endpoint, last["startTs"], last["endTs"] - 1), last["startTs"], fuel) or \
                cycle_items(itemization(uuid, eid, last["startTs"], last["endTs"] - 1), last["startTs"], fuel) or []
        return cache[k]

    def msw(ch, mt, energy):
        w = payload(api("GET", f"v2.0/dashboard/users/{uuid}/monthly-summary-widget-data?bill-start={last['startTs']}"
                               f"&bill-end={last['endTs'] - 1}&measurement-type={mt}&locale=en_US"))
        run.evidence(f"MONTHLY_SUMMARY_WIDGET_{mt}", w)
        if not w:
            ch.fail(f"{mt} empty payload")
            return
        disc = str(w.get("itemizationDisclaimer") or "")
        if "not available" in disc.lower(): ch.fail(f"{mt} disclaimer: {disc}")
        elif disc: ch.ok(f"{mt} disclaimer text '{disc}' (informational)")
        det = w.get("itemizationDetails") or {}
        if (det.get("startTs"), det.get("endTs")) != (last["startTs"], last["endTs"] - 1):
            ch.fail(f"{mt} widget period {det.get('startTs')}..{det.get('endTs')} != MS cycle")
        items = None
        for k in FUEL_KEYS[mt]:
            if isinstance(det, dict) and det.get(k):
                items = det[k]
        if not items:
            ch.fail(f"{mt} no appliance breakdown")
            return
        if not finite(w.get("energyCharges")) or not close(w["energyCharges"], energy, 0, CENT):
            ch.fail(f"{mt} energyCharges {w.get('energyCharges')} != invoice energy {energy}")
        ai = api_items(mt)
        if not ai: ch.fail(f"{mt} itemization API empty: cannot cross-check the widget")
        else: check_widget_items(ch, mt, [i for i in items if int(float(i.get("id", -1))) not in (16, 17)], ai)
        if mt == "ELECTRIC":
            sd = {x.get("streamType"): x for x in w.get("streamDetails") or []}
            for st, want in (("DELIVERED", last["E"]["dKwh"]), ("RECEIVED", last["E"]["rKwh"])):
                if not finite((sd.get(st) or {}).get("consumption")) or not close(sd[st]["consumption"], want, 0, 0.002):
                    ch.fail(f"widget {st} {(sd.get(st) or {}).get('consumption')} kWh != invoice {want}")
        ch.ok(f"{mt}: {len(items)} appliances ID-matched to the itemization API (usage+cost), energy ${w.get('energyCharges')}")

    run_check("E18", "Monthly summary widget itemization", "GET /v2.0/dashboard/users/{uuid}/monthly-summary-widget-data",
              lambda ch: msw(ch, "ELECTRIC", last["E"]["energy"]))

    def e19(ch):
        bp = api("GET", f"2.1/users/{uuid}/homes/{HID}/billprojections", allow=(400, 404, 500))
        g = api("GET", f"v2.0/dashboard/users/{uuid}/bill-projection-graph-data?measurement-type=ELECTRIC", allow=(400, 404, 500))
        run.evidence("E19_projection", {"billprojections": bp, "graph": g})
        check_projection(ch, bp, g, ong)
    run_check("E19", "Bill projection (ongoing cycle): status, period, amounts, both endpoints agree",
              "GET /2.1/.../billprojections + /v2.0/dashboard/.../bill-projection-graph-data", e19)

    def e20(ch):
        url = (f"v2.0/bill-analysis/users/{uuid}/homes/{HID}?locale=en_US&billStart={last['startTs']}"
               f"&billEnd={last['endTs'] - 1}&useNormalCompareBillCycle=true")
        deadline = time.time() + getattr(run, "bill_analysis_wait", BILL_ANALYSIS_WAIT)
        while True:          # HDD/CDD weather for the cycle is populated some time after user creation
            ba = api("GET", url, allow=(400, 404, 500))
            if status_of(ba) == 200 or time.time() > deadline:
                break
            log(f"bill-analysis HTTP {status_of(ba)} ({((ba.get('error') or {}).get('message') or '')[:90]}); retrying in 60s")
            time.sleep(60)
        run.evidence("E20_bill_analysis", ba)
        check_bill_analysis(ch, ba, last)
    run_check("E20", "Bill analysis: MS cycle vs its weather-normal cycle, amounts reconcile",
              "GET /v2.0/bill-analysis/users/{uuid}/homes/1?useNormalCompareBillCycle=true", e20)

    def ms_email_check(ch, fuel):
        """The MS email covering `fuel`: recipient, cycle, invoice total, and every appliance line
        of the email equal to the itemization API (the disagg output) for the MS cycle."""
        since = int(m.get("ingestedAt") or 0)
        if "ms_fresh" not in cache:          # always re-fetch: never trust a previous run's manifest/HTML
            collect_ms(run, since)
            cache["ms_fresh"] = True
        rows = [r for r in m.get("msNotifications") or [] if fuel in (r.get("fuels") or [])]
        if not rows:
            hint = ""
            if fuel == "GAS" and m.get("msMode") != "combined":
                hint = (" -- use --ms-mode combined (default) or wait for the code fix: known platform bug on DCX solar dual-fuel homes: EmailDataProvider:392 sets isSolarElecEmail "
                        "from GLOBAL_SOLAR for the GAS pass too, and populateSolarEnergyFlowBlock:980 then NPEs on "
                        "itemization.get(ELECTRIC) (notifications log: 'Error while processing MONTHLY_SUMMARY email ... "
                        "measurement type GAS'). Needs the code fix; no config avoids it.")
            ch.fail(f"no MONTHLY_SUMMARY email covering {fuel} (log_grep_hints.txt: notifications / emailer){hint}")
            return
        r0 = rows[0]
        htmlb = (Path(r0["file"]).read_text() if r0.get("file") and Path(r0["file"]).exists() else
                 (payload(api("GET", f"2.1/utility_notifications/notifications/{r0['notificationId']}")) or {}).get("notificationBody", ""))
        text = email_text(htmlb)
        run.evidence(f"MS_email_text_{fuel}", {"title": r0.get("notificationTitle"), "text": text})
        combined_ms = "GAS" in fuels and m.get("msMode") == "combined"
        stt = api("GET", ms_state_path(uuid, None if combined_ms else fuel), allow=(404, 500))
        run.evidence(f"MS_sent_state_{'COMBINED' if combined_ms else fuel}", stt)
        check_sent_state(ch, fuel, r0, stt, last, since, ident["email"])
        s, e = date.fromisoformat(last["start"]), date.fromisoformat(last["end"]) - timedelta(days=1)
        if f"{mmddyy(s)} - {mmddyy(e)}" not in text: ch.fail(f"MS cycle {mmddyy(s)} - {mmddyy(e)} not in email")
        key = "E" if fuel == "ELECTRIC" else "G"
        subj = r0.get("notificationTitle") or ""
        want_tot = last[key]["total"]
        combined = last["E"]["total"] + (last["G"]["total"] if "GAS" in fuels else 0)
        def shows(amount: float) -> bool:       # the email prints $61 for 61.00 and $61.43 otherwise
            forms = {f"${amount:.2f}", f"${amount:,.2f}"} | ({f"${int(round(amount))}"} if abs(amount - round(amount)) < 0.005 else set())
            return any(re.search(re.escape(f) + r"(?![\d.])", blob) for f in forms for blob in (subj, text))
        if combined_ms:
            # EmailDataProvider:397 forces CONSUMPTION for a combined email on a solar home, so amounts are kWh;
            # the subject and MONTHLY_SUMMARY_V2 section are single-fuel (electric) in the product
            e_kwh = f"{last['E']['dKwh']:.2f} kWh"
            neutral = "electricity and natural gas summary" in subj.lower()
            if fuel == "ELECTRIC" and not (any(shows(x) for x in (want_tot, combined)) or e_kwh in subj or e_kwh in text or neutral):
                ch.fail(f"subject '{subj}' / body lacks the ELECTRIC total ${want_tot:.2f} or {e_kwh}")
            heading = COMBINED_DF_STRINGS[fuel]["com.bidgely.cloud.email.itemization.title.text.ami"]
            if heading not in text:
                ch.fail(f"combined email has no '{heading}' heading: the {fuel} itemization block is not labelled")
            else:
                ch.ok(f"{fuel} block labelled '{heading}'")
            if fuel == "GAS" and not any(shows(x) for x in (want_tot, last["G"]["cost"], combined)):
                ch.warn(f"gas bill total ${want_tot:.2f} (energy ${last['G']['cost']:.2f}) is not in the combined email: the "
                        "subject and bill-summary section are electric-only, kWh on a solar home (EmailDataProvider:397); "
                        "gas appears only in its itemization block")
        elif not any(shows(x) for x in (want_tot, combined)):
            ch.fail(f"subject '{subj}' / body lacks the {fuel} invoice total ${want_tot:.2f}")
        endpoint = refs[f"{fuel}:DELIVERED"]["endpointId"]
        items = cycle_items(itemization(uuid, endpoint, last["startTs"], last["endTs"] - 1), last["startTs"], fuel) or \
            cycle_items(itemization(uuid, eid, last["startTs"], last["endTs"] - 1), last["startTs"], fuel) or []
        apps, total, solar = split_items(items)
        # in a combined email both fuels have a "Water Heating" line: scope gas to its own (non-kWh) units
        unit = "kWh" if fuel == "ELECTRIC" else (r"(?:ccf|CCF|therms?|Therms?)" if combined_ms else r"(?:kWh|ccf|CCF|therms?|Therms?)")
        # the block for this fuel: in a combined email both fuels list e.g. "Water Heating"
        block = text
        if combined_ms:
            e_head = COMBINED_DF_STRINGS["ELECTRIC"]["com.bidgely.cloud.email.itemization.title.text.ami"]
            g_head = COMBINED_DF_STRINGS["GAS"]["com.bidgely.cloud.email.itemization.title.text.ami"]
            # DF_ITEMIZATION orders the blocks by bill amount (getSortedMtypeListByBillAmount): either may be first
            me, other = (e_head, g_head) if fuel == "ELECTRIC" else (g_head, e_head)
            i0, i1 = text.find(me), text.find(other)
            if i0 >= 0:
                end = i1 if i1 > i0 else i0 + 1500
                block = text[i0:end]
        cost_mode = bool(re.search(r"(?:Lighting|Cooling|Heating|Water Heating|Other|Always On) \$\d", block))
        matched = 0
        for i in apps:
            cat = str(i.get("category") or "")
            want = float(i.get("usage") or 0)
            want_cost = float(i.get("cost") or 0)
            labels = EMAIL_LABELS.get(cat, []) + [re.sub(r"(?<!^)([A-Z])", r" \1", cat).title()]
            hit = None
            for label in labels:
                pat = rf"{re.escape(label)} \$([\d.,]+)" if cost_mode else rf"{re.escape(label)} ([\d.,]+) {unit}"
                hit = re.search(pat, block, flags=re.I)
                if hit:
                    break
            if not hit:
                if cost_mode and want_cost < 0.5:
                    matched += 1          # rounds to $0: the widget/email drops zero rows
                    continue
                ch.fail(f"email lacks {fuel} appliance '{cat}' ({'$%.2f' % want_cost if cost_mode else f'{want:.0f}'}) under any of {labels}")
                continue
            val = float(hit.group(1).replace(",", ""))
            if cost_mode:
                ok_line = abs(val - want_cost) < 1.0 + 1e-6          # whole-dollar, sum-preserving rounding
            else:
                cands = [want] if fuel == "ELECTRIC" else [want, want * 1000 / CCF_TO_WH, want / 1.037 * 1000 / CCF_TO_WH * 1.037]
                ok_line = any(abs(val - c) <= max(1, 0.02 * c) for c in cands)
            if not ok_line:
                ch.fail(f"email {label} {val} != itemization {want_cost if cost_mode else want:.2f}")
            else:
                matched += 1
        if not apps: ch.fail(f"{fuel} itemization API empty, cannot reconcile email")
        if fuel == "ELECTRIC":
            if total:
                if cost_mode:
                    tw = float(total.get("cost") or 0)
                    hit = re.search(r"Total (?:Estimated )?Energy Usage(?: Charges)? \$([\d.,]+)", block)   # non-solar: "Total Energy Usage Charges"
                    if not hit or abs(float(hit.group(1).replace(",", "")) - tw) >= 1.0 + 1e-6:
                        ch.fail(f"email total {hit and hit.group(1)} != itemization total ${tw:.2f}")
                else:
                    tw = round(float(total.get("usage") or 0))
                    hit = re.search(r"Total (?:Estimated )?Energy Usage(?: Charges)? (\d+) kWh", block)
                    if not hit or abs(int(hit.group(1)) - tw) > 1: ch.fail(f"email total {hit and hit.group(1)} kWh != itemization total {tw} kWh")
            if solar: ch.fail(f"non-solar itemization has a solar row (id 16): {solar.get('usage')}")
            if re.search(rf'<tr[^>]*\bid="{SOLAR_SECTION}"', htmlb or ""): ch.fail(f"non-solar Monthly Summary renders {SOLAR_SECTION}")
            else: ch.ok(f"no {SOLAR_SECTION} block (non-solar)")
        ch.ok(f"{fuel} email: {matched}/{len(apps)} appliance lines == itemization API (disagg), shown in {'$' if cost_mode else unit.strip('(?:)').split('|')[0]}")
        if not cost_mode:
            ch.warn(f"{fuel} itemization is shown in usage, not $: set email_itemization_section_configs.solar_display_value_identifier=cost")
        ch.ok(f"'{subj}' variant {r0.get('variant')} status {r0.get('status')} to {r0.get('deliveryDestination')}, cycle {mmddyy(s)} - {mmddyy(e)}")
    run_check("E21", "Monthly Summary email (ELECTRIC" + (", combined dual-fuel" if "GAS" in fuels and m.get("msMode") == "combined" else "")
              + "): generated + app sent-state, MS cycle, content == itemization", "GET /2.1/utility_notifications/users/{uuid} + /notifications/{id}",
              lambda ch: ms_email_check(ch, "ELECTRIC"))

    # ---------------- GAS ----------------
    if "GAS" in fuels:
        gref = refs["GAS:DELIVERED"]
        geid = gref["endpointId"]

        def g01(ch):
            am = all_meters(uuid)
            v = am.get(ident["tokens"]["GAS:DELIVERED"])
            if not v:
                ch.fail(f"gas meter {ident['tokens']['GAS:DELIVERED']} missing")
                return
            if str(v["meter"].get("measurement")) not in ("1", "GAS"): ch.warn(f"gas meter measurement {v['meter'].get('measurement')}")
            if v["gateway"].get("decommissioned") == "true": ch.fail("gas gateway decommissioned")
            ch.ok(f"gas gateway gid {v['gid']} type {v['gateway'].get('type')} meter {v['mid']} stream {v['meter'].get('streamType')}")
        run_check("G01", "Gas gateway + meter", "GET /meta/.../gws/{gid}/meters/", g01)
        run_check("G02", "Gas RAW stored exactly (ccf -> Wh)", "GET /streams/.../gb.json", lambda ch: raw_sample(ch, "GAS:DELIVERED", "G", +1))
        run_check("G03", "Gas billing cycles == electric cycles", "GET /billingdata/.../billingcycles?measurementType=GAS",
                  lambda ch: (check_cycles(ch, "GAS"), [ch.fail("GAS cycles differ from ELECTRIC") for _ in [0] if
                              [x for x in bcs("GAS") if x[0] >= cyc[0]["startTs"]][:len(cyc)] != [x for x in bcs("ELECTRIC") if x[0] >= cyc[0]["startTs"]][:len(cyc)]]))

        def g04(ch):
            ud = utility_data(uuid, "GAS", t0, t1)
            run.evidence("G04_utilitydata_GAS", ud)
            check_invoice_lines(ch, "GAS", ud, gen_files()["inv"], cyc)
            for v in ud.values():       # gasValue is reported in meta_data.gas_unit (ccf) or, if unset, stored kWh
                c = next((c for c in cyc if isinstance(v, dict) and c["startTs"] == v.get("billingStartTs")), None)
                if c and not (close(inv_value(v, "GAS"), c["G"]["ccf"], 0.005, 0.01) or
                              close(inv_value(v, "GAS"), c["G"]["ccf"] * CCF_TO_WH / 1000, 0.005, 0.1)):
                    ch.fail(f"{c['start']} gasValue {inv_value(v, 'GAS')} != {c['G']['ccf']} ccf")
            ch.ok(f"MS cycle {last['G']['ccf']} ccf = {last['G']['ccf'] * CCF_TO_WH / 1000:.1f} kWh, total ${last['G']['total']}")
        run_check("G04", "Gas persisted invoice lines == generated INVOICE file", "GET /billingdata/.../utilitydata?measurementType=GAS", g04)

        def g05(ch):
            gc = streams_of(ctype(uuid, "energy_consumption", "/all-streams", "GAS", t0, t1))
            run.evidence("G05_gas_energy", gc)
            check_invoice_vs_raw(ch, "GAS", gen_files()["inv"], {"DELIVERED": gen_files()["sums"]["GAS:DELIVERED"]}, 1.0, gc, cyc)
        run_check("G05", "Gas invoice file == RAW file == ENERGY_CONSUMPTION (every cycle)",
                  "generated INVOICE + RAW_G + GET .../ctypes/energy_consumption?measurementType=GAS", g05)

        def g06(ch):
            gb = streams_of(ctype(uuid, "billing_cost", "/all-streams", "GAS", t0, t1))
            run.evidence("G06_gas_cost", gb)
            for c in cyc:
                h = near(gb.get("DELIVERED", {}), c["startTs"])
                if not h or not close(float(h[1].get("energyCharges") or 0), c["G"]["cost"], REL_TOL, 0.05):
                    ch.fail(f"{c['start']} gas energyCharges {h and h[1].get('energyCharges')} != {c['G']['cost']}")
            ch.ok(f"gas BILLING_COST == RAW x rate; MS cycle ${last['G']['cost']}")
        run_check("G06", "Gas BILLING_COST == RAW x rate plan", "GET .../ctypes/billing_cost?measurementType=GAS", g06)

        def g07(ch):
            entries = itemization(uuid, geid, last["startTs"], last["endTs"] - 1)
            if not cycle_items(entries, last["startTs"], "GAS"):
                entries = itemization(uuid, eid, last["startTs"], last["endTs"] - 1)
            run.evidence("G07_gas_itemization", entries)
            r = item_rule(ch, "GAS", entries, last, last["G"]["cost"], "GAS")
            if r:
                ch.ok(f"GAS MS cycle {len(r[0])} appliances sum ${r[3]:.2f} == invoice energy ${last['G']['cost']}")
        run_check("G07", "Gas itemization (V1) non-empty and == energy cost", "GET .../itemizationDetails (gas)", g07)
        run_check("G08", "Gas bill summary widget", "GET /v2.0/dashboard/.../usage-widget-data?measurement-type=GAS",
                  lambda ch: widget(ch, "GAS", last["G"]["cost"], last["G"]["fixed"], False))
        run_check("G09", "Gas monthly summary widget itemization", "GET .../monthly-summary-widget-data?measurement-type=GAS",
                  lambda ch: msw(ch, "GAS", last["G"]["cost"]))
        run_check("G10", "Monthly Summary email (GAS" + (", in the combined dual-fuel email" if m.get("msMode") == "combined" else "")
                  + "): generated + app sent-state, MS cycle, content == itemization",
                  "GET /2.1/utility_notifications/users/{uuid} + /notifications/{id}", lambda ch: ms_email_check(ch, "GAS"))

        def df01(ch):
            vals = cache.get("attrs") or {}
            gas_attr = {k: v for k, v in vals.items() if "GAS" in k}
            if not any(v is True for v in gas_attr.values()): ch.warn(f"no true GAS attribute: {gas_attr}")
            ud_e, ud_g = cache.get("udE") or utility_data(uuid, "ELECTRIC", t0, t1), utility_data(uuid, "GAS", t0, t1)
            for c in cyc:
                he, hg = near(ud_e, c["startTs"]), near(ud_g, c["startTs"])
                if not he or not hg or he[1].get("billingEndTs") != hg[1].get("billingEndTs"):
                    ch.fail(f"{c['start']} electric/gas invoice periods differ")
            ch.ok(f"both fuels invoiced on identical periods; gas attributes {gas_attr}")
        run_check("DF01", "Dual fuel: both fuels, identical invoice periods", "user-attributes + utilitydata both fuels", df01)

    def d01(ch):
        info = dashboard_link(run, "validate")
        if not info.get("url"):
            ch.fail(f"cipher API: {info.get('error')}")
        elif info.get("verified"):
            ch.ok(f"user-hash deciphers to {uuid}" + (f", expires {info['expiresAt']}" if info.get("expiresAt") else ""))
        elif info.get("hashValid"):
            ch.warn("hash decrypts and is not expired, but the uuid is not returned: the decipher endpoint needs "
                    "a dashboard OAuth client-id (" + str(info.get("verifyError")) + "); dashboard rendering is not checked")
        else:
            ch.fail(f"link not usable: {info.get('verifyError')}")
    run_check("D01", "Dashboard login link (user-hash) generated and valid", "GET /v2.0/user-auth/cipher + /v2.0/user-auth/{hash}", d01)

    write_report(run, checks, fuels)
    return checks


def write_report(run: Run, checks: list[Check], fuels: tuple[str, ...]) -> None:
    m = run.m
    summ = {s: sum(1 for c in checks if c.status == s) for s in ("PASS", "WARN", "FAIL")}
    (run.dir("reports") / "validation_report.json").write_text(json.dumps(
        {"uuid": m.get("uuid"), "identity": m.get("identity"), "summary": summ, "checks": [c.__dict__ for c in checks]}, indent=2))
    last = m["cycles"][-1]
    md = [f"# MDS {'dual fuel (ELECTRIC + GAS)' if 'GAS' in fuels else 'single fuel (ELECTRIC)'} -- pilot {PILOT_ID}", "",
          f"- uuid `{m.get('uuid')}`", f"- externalUserId `{m['identity']['externalUserId']}`, email `{m['identity']['email']}`",
          f"- MS cycle {last['start']} .. {last['end']} (exclusive)", f"- run dir `{run.out}`", "",
          f"**{summ['PASS']} PASS / {summ['WARN']} WARN / {summ['FAIL']} FAIL**", "",
          "| Check | Name | Status | API | Details |", "|---|---|---|---|---|"]
    md += [f"| {c.cid} | {c.name} | {c.status} | `{c.api}` | {'<br>'.join(c.details).replace('|', '/')} |" for c in checks]
    (run.dir("reports") / "validation_report.md").write_text("\n".join(md) + "\n")
    m["validation"] = summ
    run.save()
    log(f"REPORT {summ} -> {run.out / 'reports' / 'validation_report.md'}")


def detail_lines(run: Run) -> list[str]:
    """Every identifying detail of the run, one 'key : value' per line."""
    m = run.m
    i = m["identity"]
    last = m["cycles"][-1]
    rows = [("pilotId", m.get("pilotId")), ("uuid", m.get("uuid") or "(not created yet)"),
            ("externalUserId", i["externalUserId"]), ("customerId", i["customer"]), ("accountId", i["account"]),
            ("premiseId", i["premise"]), ("email", i["email"]), ("name", f"{i['firstName']} {i['lastName']}"),
            ("address", i["address"]), ("city", i["city"]), ("state", i["state"]), ("zipcode", i["zip"]),
            ("timezone", m.get("timezone")), ("fuels", ", ".join(m["fuels"]))]
    for fuel, mid in i["meters"].items():
        rows.append((f"meterId {fuel}", mid))
        rows.append((f"phone {fuel}", i["phones"][fuel]))
    for k, t in i["tokens"].items():
        rows.append((f"token {k}", t))
    rows += [("gatewayToken", i["gatewayToken"]),
             ("ratePlan", f"{m['ratePlan']} (plan {m['planNumbers'].get('ELECTRIC')})"),
             ("billCycleCode", m["billCycleCode"]),
             ("cycles", f"{len(m['cycles'])} completed {m['cycles'][0]['start']} .. {last['end']} (exclusive)"),
             ("MS cycle", f"{last['start']} .. {last['end']} (exclusive)"),
             ("MS invoice ELECTRIC", f"D {last['E']['dKwh']} kWh ${last['E']['dCost']} | FEED_IN {last['E']['rKwh']} kWh "
                                     f"${last['E']['rCost']} | energy ${last['E']['energy']} | fixed ${last['E']['fixed']} | total ${last['E']['total']}")]
    if "GAS" in m["fuels"]:
        g = last["G"]
        rows.append(("MS invoice GAS", f"{g['ccf']} ccf | energy ${g['cost']} | fixed ${g['fixed']} | total ${g['total']}"))
    rows.append(("RAW intervals/stream", m["raw"]["intervals"]))
    dsh = m.get("dashboard") or {}
    if dsh.get("url"):
        rows.append(("dashboard url", dsh["url"]))
        rows.append(("dashboard link check", ("verified (deciphers to uuid)" if dsh.get("verified") else
                                              "NOT verified: " + str(dsh.get("verifyError")))
                     + (f", expires {dsh['expiresAt']}" if dsh.get("expiresAt") else "") + f", generated {dsh['generatedAt']}"))
    elif m.get("uuid"):
        rows.append(("dashboard url", f"(not generated: {dsh.get('error', 'run the dashboard phase')})"))
    rows.append(("output dir", str(run.out)))
    return [f"  {k:<26}: {v}" for k, v in rows]


def log_details(run: Run, title: str) -> None:
    log("-" * 78)
    log(title)
    for ln in detail_lines(run):
        log(ln)
    log("-" * 78)


def write_summary(run: Run, checks: list[Check] | None) -> None:
    m = run.m
    lines = ["=" * 78, f"RUN {run.out.name}", "user details:"] + detail_lines(run) + \
            ["files:"] + [f"  {k:<12} {Path(v).name}" for k, v in m["files"].items()]
    if m.get("msNotifications"):
        n = m["msNotifications"][0]
        lines.append(f"MS email        '{n.get('notificationTitle')}' ({n.get('variant')}) -> {n.get('deliveryDestination')}")
    if checks:
        lines.append("validation:")
        lines += [f"  [{c.status:<4}] {c.cid:<4} {c.name}" for c in checks]
        lines.append(f"  {m.get('validation')}")
    lines.append("=" * 78)
    (run.out / "SUMMARY.txt").write_text("\n".join(lines) + "\n")
    (run.dir("reports") / "SUMMARY.txt").write_text("\n".join(lines) + "\n")
    for ln in lines:
        log(ln)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(doc: str, default_out: str, fuels: tuple[str, ...], argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=doc, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    p.add_argument("--phases", default="all", help=f"comma list of {','.join(PHASES)} or all")
    p.add_argument("--dry-run", action="store_true", help="generate files only, offline")
    p.add_argument("--manifest", help="resume a previous run (skips generate)")
    p.add_argument("--token-file")
    p.add_argument("--aws-profile")
    p.add_argument("--output-dir", default=default_out)
    p.add_argument("--email")
    # weather (HDD/CDD) is looked up by ZIP; 99501 has none in <environment> and bill-analysis then NPEs
    # for any home with an AC/space-heater appliance. 11691 has full weather history.
    p.add_argument("--zip", default="11691")
    p.add_argument("--city", default="Far Rockaway")
    p.add_argument("--state", default="NY")
    p.add_argument("--rate-plan", default=DEFAULT_RATE_PLAN)
    p.add_argument("--gas-rate-plan", default=DEFAULT_GAS_RATE_PLAN, help="gas METERENROLL plan (default: same plan, DELIVERED rate)")
    p.add_argument("--transition-rate-plan", help="move the home to this plan (METERENROLL rate transition) for the "
                                                  "last --transition-cycles completed cycles, e.g. the profile's email.rate_plans.tou")
    p.add_argument("--transition-cycles", type=int, default=2)
    p.add_argument("--no-transition-home-api-fallback", dest="transition_home_api_fallback", action="store_false",
                   help="ingestion-only transition: on an ingestion timeout fail instead of writing the schedule "
                        "through the home API")
    p.add_argument("--reaggregate-after-transition", action="store_true",
                   help="transition phase: run disagg + aggregation a second time after the plan switch")
    p.add_argument("--transition-at", choices=("upload", "before-final-agg", "phase"), default="upload",
                   help="when the rate-transition file is uploaded: with the enrolment files, or after RAW/INVOICE/disagg "
                        "(history and itemization exist, as for a real customer switching plans)")
    p.add_argument("--flag-settle", type=int, default=120,
                   help="userenroll-flag: seconds to let the ingester see handle_meter_enrollment=false")
    p.add_argument("--flag-transition-wait", type=int, default=240)
    p.add_argument("--transition-via", choices=("userenroll", "meterenroll", "userenroll-flag"), default="meterenroll",
                   help="file that carries the rate transition. meterenroll (default) is the only one a handle_meter_enrollment=true target applies "
                        "(meterEnrollmentHandled=true: USERENROLL skips rate plans), but it publishes no TOU_ONBOARDING; "
                        "userenroll is what LaunchpadUserCreator would publish from, on pilots without METERENROLL")
    p.add_argument("--transition-wait", type=int, default=600,
                   help="seconds to wait for the ingester to apply the transition before the home-API fallback")
    p.add_argument("--peak-kw", type=float, default=0.0, help="extra evening (17-22h) load, kW at the summer peak")
    p.add_argument("--hold-ms", action="store_true",
                   help="pre-mark the MS cycle as sent right after user creation so the automatic Monthly Summary "
                        "from the aggregation pass is not sent (TOU runs send the coaching MS themselves)")
    p.add_argument("--budget-billing", action="store_true",
                   help="make the user a Budget Billing user: BB_AMOUNT invoice rows + a USERPROGRAM file (mds_bb.py)")
    p.add_argument("--bb-amount", type=float, default=0.0, help="monthly budget-bill amount (0 = 110%% of the average cycle bill)")
    p.add_argument("--bb-trueup-period", type=int, default=12, help="BB_TrueUp_Period (cycles) in the program attributes")
    p.add_argument("--bb-elapsed-cycles", type=int, default=6,
                   help="completed cycles already inside the BB period (program start = start of the N-th last cycle)")
    p.add_argument("--bb-date-format", default="%Y-%m-%d", help="strftime format of the USERPROGRAM start date")
    p.add_argument("--raw-end-days-ago", type=float, default=0.0,
                   help="end RAW data this many days before now (>= 3 days into the open cycle; keep < 10): mid-cycle notifications")
    p.add_argument("--bill-cycle-code", default=DEFAULT_BILL_CYCLE_CODE)
    p.add_argument("--bucket")
    p.add_argument("--raw-unit", choices=("Wh", "kWh"))
    p.add_argument("--cycles", type=int, default=13, help="completed monthly cycles; the last one is the MS cycle")
    p.add_argument("--solar-kw", type=float, default=0.0,
                   help="rooftop solar kW of the generated profile; 0 = non-solar (RAW_R all zero, FEED_IN $0)")
    p.add_argument("--base-kw", type=float, default=0.45)
    p.add_argument("--gas-scale", type=float, default=1.0)
    p.add_argument("--fixed-charge", type=float, default=10.0)
    p.add_argument("--gas-fixed-charge", type=float, default=8.0)
    p.add_argument("--ensure-gas-stream", action="store_true", help="PILOT write: add GAS to supported_streams if missing")
    p.add_argument("--ensure-ms-config", action="store_true", help="kept for CLI compatibility; non-solar never writes pilot event lists")
    p.add_argument("--no-user-config", action="store_true")
    p.add_argument("--poll", type=int, default=20)
    p.add_argument("--bc-wait", type=int, default=600)
    p.add_argument("--user-wait", type=int, default=1200)
    p.add_argument("--meter-wait", type=int, default=900)
    p.add_argument("--raw-wait", type=int, default=1200)
    p.add_argument("--invoice-wait", type=int, default=1200)
    p.add_argument("--settle", type=int, default=120)
    p.add_argument("--disagg-resend-after", type=int, default=420)
    p.add_argument("--disagg-wait", type=int, default=900)
    p.add_argument("--agg-wait", type=int, default=900)
    p.add_argument("--attempts", type=int, default=2)
    p.add_argument("--ms-wait", type=int, default=600)
    p.add_argument("--ms-mode", choices=("combined", "separate"), default="combined",
                   help="dual fuel only: ONE combined dual-fuel Monthly Summary (default; user-scope configs) or one "
                        "email per fuel (the GAS one is blocked by the EmailDataProvider:980 NPE on DCX solar homes)")
    p.add_argument("--ms-resend", action="store_true",
                   help="rewind the MS sent-state (billStartTs -> previous cycle, sentCount 0) and trigger again")
    p.add_argument("--bill-analysis-wait", type=int, default=BILL_ANALYSIS_WAIT)
    p.add_argument("--adopt-unrecorded-files", action="store_true",
                   help="OPERATOR ONLY, with --manifest of a run that predates enrollment integrity records: record the "
                        "stored files at their CURRENT bytes (after you have checked them) instead of refusing")
    a = p.parse_args(argv)
    a.phase_list = list(PHASES) if a.phases == "all" else [x.strip() for x in a.phases.split(",") if x.strip()]
    bad = [x for x in a.phase_list if x not in PHASES]
    if bad:
        p.error(f"unknown phases {bad}")
    if a.manifest and "generate" in a.phase_list and a.phases == "all":
        a.phase_list.remove("generate")
    return a


def main(doc: str, fuels: tuple[str, ...], default_out: str, argv: list[str] | None = None) -> int:
    global _TOKEN, _LOG_FH
    argv = mds_target.bootstrap(argv)          # PROFILE_REQUIRED before anything touches a target
    a = parse_args(doc, default_out, fuels, argv)
    if a.manifest:
        mp = Path(a.manifest)
        run = Run(mp.parent, json.loads(mp.read_text()))
        if run.m.get("uuid"):
            set_uuid_tag(run.m["uuid"])
    else:
        out = Path(a.output_dir).expanduser() / f"run_{datetime.now():%Y%m%d_%H%M%S}_{secrets.token_hex(3)}"
        out.mkdir(parents=True, exist_ok=False)
        run = Run(out, {"invocation": sys.argv})
    _LOG_FH = open(run.dir("logs") / "run.log", "a")
    global LAST_RUN_DIR
    LAST_RUN_DIR = run.out                      # the caller's handle on THIS run (concurrent runs share out_dir)
    run.bill_analysis_wait = a.bill_analysis_wait
    if "GAS" in fuels and ("msMode" not in run.m or "--ms-mode" in (argv or sys.argv)):
        run.m["msMode"] = a.ms_mode
        if run.m.get("identity"):
            run.save()
    log(f"run dir {run.out}  fuels {fuels}  phases {a.phase_list}")
    checks = None
    try:
        if a.manifest and run.m.get("files"):
            # a resumed phase never uses (or re-uploads) an original whose bytes differ from the manifest record
            n = len(verify_recorded_files(run, adopt_unrecorded=a.adopt_unrecorded_files))
            log(f"enrollment integrity: {n} recorded original(s) verified against the manifest sha256")
        if a.dry_run:
            ctx = {"tz": ZoneInfo(DEFAULT_TZ), "units": {"ELECTRIC": a.raw_unit or "Wh", "GAS": "ccf"},
                   "rates": {"ELECTRIC": RateBook([], {"DELIVERED": 0.15, "RECEIVED": 0.05}),
                             "GAS": RateBook([], {"DELIVERED": 0.15, "RECEIVED": 0.05})},
                   "planNumbers": {"ELECTRIC": "DRYRUN", "GAS": "DRYRUN"}, "schedule": {}}
            generate(run, a, fuels, ctx)
            log("dry run complete: files generated, nothing uploaded")
            return 0
        _TOKEN = get_token(a.token_file)
        ctx: dict[str, Any] = {}
        if any(x in a.phase_list for x in ("preflight", "generate", "upload")):
            ctx = preflight(run, a, fuels)
        elif any(x in a.phase_list for x in ("aggregate", "notify")):
            sess = aws(a.aws_profile)
            ctx = {"s3": sess.client("s3"), "sqs": sess.client("sqs")}
        if a.phase_list == ["preflight"]:
            log("preflight OK")
            return 0
        if "generate" in a.phase_list:
            generate(run, a, fuels, ctx)
        if not run.m.get("identity"):
            raise SetupError("no generated files -- run generate or pass --manifest")
        if "upload" in a.phase_list:
            do_upload(run, a, ctx, fuels, with_configure="configure" in a.phase_list and not a.no_user_config)
        elif "configure" in a.phase_list and not a.no_user_config:
            configure(run, fuels)
        if "aggregate" in a.phase_list:
            aggregate(run, a, ctx, fuels)
        if "transition" in a.phase_list and run.m.get("transition") and not run.m.get("transitionApplied"):
            # flat-plan history is ingested, disaggregated and aggregated: the customer now switches plans
            step("RATE TRANSITION (customer switches plans; the system publishes TOU_ONBOARDING itself)")
            apply_transition(run, a, ctx["s3"], ctx["bucket"], home_api_fallback=a.transition_home_api_fallback)
            if AFTER_TRANSITION:
                AFTER_TRANSITION(run)
            if getattr(a, "reaggregate_after_transition", False):
                step("ROUND 2: disagg + aggregation again on the new plan")
                aggregate(run, a, ctx, fuels)
        if "notify" in a.phase_list:
            notify(run, a, ctx, fuels)
        if "validate" in a.phase_list:
            checks = validate(run, fuels)
        if "dashboard" in a.phase_list and run.m.get("uuid") and "validate" not in a.phase_list:
            dashboard_link(run, "end of run")          # fresh link at the end: the hash is time-stamped
        write_summary(run, checks)
        return 1 if checks and any(c.status == "FAIL" for c in checks) else 0
    except SetupError as exc:
        log(str(exc), "ERROR")
        run.m.setdefault("errors", []).append(str(exc))
        run.save()
        if run.m.get("identity"):
            write_summary(run, checks)
        return 2
    except KeyboardInterrupt:
        log("interrupted", "ERROR")
        return 130
    finally:
        if _LOG_FH:
            _LOG_FH.close()
            _LOG_FH = None
