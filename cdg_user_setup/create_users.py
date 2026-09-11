#!/usr/bin/env python3
"""
Create QA users via CDG (Customer Data Generator).

Reads constant destination/DETO settings from config.json and one row per user
from sources.csv, then runs the full CDG porting flow for each row: it pulls the
production user's data, writes user metadata (persona, rate plan, email, billing
cycle), syncs weather, generates the enroll/raw/billing files, and finally reads
back the destination UUID.

A user whose port fails is retried a few times with a growing pause, since the
usual causes (slow prod fetch, gateway timeout, S3 hiccup) clear on their own.
Anything still unported is written to `failed_users.csv` in the same format as
`sources.csv`, so a follow-up run is just `cp failed_users.csv sources.csv`.

Self-contained: depends only on the `requests` library. The DETO endpoints and
payload shapes mirror the qa-utils `cdg` package.
"""

import argparse
import csv
import io
import json
import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
SOURCES_CSV_PATH = SCRIPT_DIR / "sources.csv"
RESULTS_PATH = SCRIPT_DIR / "create_users_results.json"
FAILED_CSV_PATH = SCRIPT_DIR / "failed_users.csv"

# Column order of sources.csv, reused when writing the failures file so it can
# be fed straight back in as a sources.csv.
SOURCES_CSV_COLUMNS = (
    "PERSONA",
    "PERSONA_DESCRIPTION",
    "SOURCE_UUID",
    "SOURCE_ENVIRONMENT",
    "RATE_PLAN",
    "PRIMARY_EMAIL",
    "BILLING_CYCLE",
    "METER_FUEL",
    "BB_DURATION",
)

DEFAULT_RETRIES = 2
DEFAULT_RETRY_DELAY = 20

# Constant settings that don't vary per user.
REQUIRED_CONFIG_KEYS = (
    "DETO_BASE_URL",
    "DETO_ACCESS_TOKEN",
    "UTILITY_NAME",
    "DESTINATION_PILOT_ID",
    "DESTINATION_ENVIRONMENT",
    "FILE_UPLOAD_BUCKET",
)

# One row per user. Only SOURCE_UUID is strictly required; the rest have
# sensible fallbacks applied in load_sources().
REQUIRED_CSV_COLUMNS = {
    "PERSONA",
    "PERSONA_DESCRIPTION",
    "SOURCE_UUID",
    "SOURCE_ENVIRONMENT",
    "RATE_PLAN",
    "PRIMARY_EMAIL",
    "BILLING_CYCLE",
    "METER_FUEL",
    "BB_DURATION",
}

DEFAULT_SOURCE_ENVIRONMENT = "NA"
DEFAULT_METER_FUEL = "AMI-ELECTRIC"
DEFAULT_BILLING_CYCLE = "CDG_01"
DEFAULT_BB_DURATION = "0 Month"
DEFAULT_TIMEOUT = 60.0


# --------------------------------------------------------------------------- #
# Config + input loading
# --------------------------------------------------------------------------- #


def load_config():
    if not CONFIG_PATH.exists():
        raise ValueError(
            f"{CONFIG_PATH} not found. Copy config.json.example to config.json and fill it in."
        )
    with open(CONFIG_PATH) as f:
        config = json.load(f)

    missing = [k for k in REQUIRED_CONFIG_KEYS if not config.get(k)]
    if missing:
        raise ValueError(f"Missing required config key(s) in {CONFIG_PATH}: {', '.join(missing)}")

    config.setdefault("SOURCE_COUNTRY", "US")
    config.setdefault("TIMEOUT", DEFAULT_TIMEOUT)
    return config


def _parse_meter_fuel(raw):
    """A meter-fuel cell may list several fuels for dual/multi-fuel users.
    Accept ',', ';' or '|' as separators so a comma-list works whether or not
    the CSV cell was quoted."""
    if not raw or not raw.strip():
        return [DEFAULT_METER_FUEL]
    normalized = raw.replace(";", ",").replace("|", ",")
    fuels = [f.strip() for f in normalized.split(",") if f.strip()]
    return fuels or [DEFAULT_METER_FUEL]


def _format_bb_duration(raw):
    """The BB_DURATION cell holds just a number of months (e.g. 0, 3, 12); the
    API expects the string "<n> Month". Tolerate an already-formatted value or
    a trailing 'month(s)' too. Blank -> "0 Month"."""
    if not raw or not raw.strip():
        return DEFAULT_BB_DURATION
    match = re.match(r"^\s*(\d+)", raw)
    if match:
        return f"{int(match.group(1))} Month"
    return raw.strip()


def load_sources():
    if not SOURCES_CSV_PATH.exists():
        raise ValueError(
            f"{SOURCES_CSV_PATH} not found. Copy sources.csv.example to sources.csv and fill it in."
        )
    # Tolerate a UTF-8 BOM and any leading blank lines before the header row.
    with open(SOURCES_CSV_PATH, newline="", encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    header = set(reader.fieldnames or [])
    missing_cols = REQUIRED_CSV_COLUMNS - header
    if missing_cols:
        raise ValueError(
            f"{SOURCES_CSV_PATH} missing column(s): {', '.join(sorted(missing_cols))}"
        )
    raw_rows = list(reader)

    users = []
    for i, row in enumerate(raw_rows, start=2):  # row 1 is the header
        uuid = (row.get("SOURCE_UUID") or "").strip()
        # Skip blank/comment lines so trailing newlines don't blow up.
        if not uuid or uuid.startswith("#"):
            continue

        users.append(
            {
                "persona": (row.get("PERSONA") or "").strip(),
                "persona_description": (row.get("PERSONA_DESCRIPTION") or "").strip(),
                "source_uuid": uuid,
                "source_environment": (row.get("SOURCE_ENVIRONMENT") or "").strip()
                or DEFAULT_SOURCE_ENVIRONMENT,
                # Blank rate plan -> fall back to the plan on the prod user's meter.
                "rate_plan": (row.get("RATE_PLAN") or "").strip() or None,
                # Blank email -> deterministic QA address keyed on the source UUID.
                "primary_email": (row.get("PRIMARY_EMAIL") or "").strip()
                or f"bidgelyqa_{uuid}@bidgely.com",
                "billing_cycle": (row.get("BILLING_CYCLE") or "").strip() or DEFAULT_BILLING_CYCLE,
                "meter_fuel": _parse_meter_fuel(row.get("METER_FUEL")),
                "bb_duration": _format_bb_duration(row.get("BB_DURATION")),
                "_row": i,
            }
        )
    return users


# --------------------------------------------------------------------------- #
# DETO / CDG API client
# --------------------------------------------------------------------------- #


class DetoClient:
    """Thin client over the DETO CDG endpoints used by the porting flow."""

    FETCH_PROD_USER_DATA = "/v1/fetchProdUserData/userDetails"
    USER_METADATA = "/v1/user-metadata"
    SYNC_WEATHER_DATA = "/v1/fetchProdUserData/syncWeatherData"
    CREATE_BC_SCHEDULE = "/v1/fetchProdUserData/createBCSchedule"
    CREATE_USER_ENROLL_FILE = "/v1/fetchProdUserData/userDetails"
    CREATE_RAW_FILE = "/v1/fetchProdUserData/rawData"
    CREATE_BILLING_FILE = "/v1/fetchProdUserData/billingData"
    FETCH_USER_UUID = "/fetchUserUUID"
    ASSIGN_PHYSICAL_CLUSTER = "/v1/assign-physical-cluster"
    VALID_METER_FUEL = "/v1/valid-meter-fuel"

    def __init__(self, base_url, access_token, timeout=DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Accept the token with or without a leading "Bearer " so the config
        # value can be pasted straight from a captured request.
        token = access_token.strip()
        if token.lower().startswith("bearer "):
            token = token[len("bearer ") :].strip()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "Authorization": f"Bearer {token}",
            }
        )

    def _post(self, endpoint, payload):
        r = self.session.post(f"{self.base_url}{endpoint}", json=payload, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _get(self, endpoint, params=None):
        r = self.session.get(f"{self.base_url}{endpoint}", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _get_raw_url(self, url):
        # Steps that pass a pre-encoded userMeterMapping build the query string
        # by hand to avoid double-encoding it.
        r = self.session.get(url, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _meter_mapping_query(
        self, uuid, meter_fuel, dest_env, dest_pilot_id, utility_name, source_env, extra=""
    ):
        mapping = json.dumps({uuid: meter_fuel})
        encoded = urllib.parse.quote(mapping)
        params = [
            f"userMeterMapping={encoded}",
            f"utilityName={utility_name}",
            f"sourceEnvironment={source_env}",
            "sourceUsername=",
            "sourcePassword=",
            f"destinationPilotId={dest_pilot_id}",
            f"destinationEnv={dest_env}",
        ]
        if extra:
            params.append(extra)
        return "&".join(params)

    # Step 1
    def fetch_prod_user_data(
        self, uuid, utility_name, source_env, file_upload_bucket, meter_fuel, dest_pilot_id
    ):
        payload = {
            "userMeterFuelMap": {uuid: meter_fuel},
            "utilityName": utility_name,
            "sourceEnvironment": source_env,
            "sourceUsername": "",
            "sourcePassword": "",
            "houseType": "",
            "subPilot": "",
            "ingestionBucket": "",
            "fileUploadBucket": file_upload_bucket,
            "destinationPilotId": dest_pilot_id,
        }
        return self._post(self.FETCH_PROD_USER_DATA, payload)

    # Pre-check: which meter fuels does the source user actually have?
    def fetch_valid_meter_fuels(self, source_env, uuid):
        """Return the list of meter fuels available on the source user (e.g.
        ["AMI-ELECTRIC"] or ["AMI-ELECTRIC", "AMI-GAS"]) via the
        valid-meter-fuel API. Empty list if the user/fuels can't be determined."""
        data = self._post(f"{self.VALID_METER_FUEL}/{source_env}", {"source_uuids": [uuid]})
        results = data.get("results") or []
        if not results:
            return []
        return results[0].get("valid_meter_fuels") or []

    # Step 2
    def update_user_metadata(self, metadata):
        return self._post(self.USER_METADATA, metadata)

    # Step 3
    def sync_weather_data(self, uuid, source_env, dest_env, dest_pilot_id, source_country):
        payload = {
            "source_uuid": [uuid],
            "source_environment": source_env,
            "destinationEnvironment": dest_env,
            "destination_pilot_id": dest_pilot_id,
            "sourceCountry": source_country,
        }
        return self._post(self.SYNC_WEATHER_DATA, payload)

    # Step 4
    def create_billing_cycle_schedule(
        self, dest_pilot_id, dest_env, uuid, source_env, utility_name
    ):
        return self._get(
            self.CREATE_BC_SCHEDULE,
            {
                "destinationPilotId": dest_pilot_id,
                "destinationEnv": dest_env,
                "sourceUUID": uuid,
                "sourceEnv": source_env,
                "utilityName": utility_name,
            },
        )

    # Step 5
    def create_user_enroll_file(
        self, uuid, meter_fuel, utility_name, source_env, dest_env, dest_pilot_id
    ):
        q = self._meter_mapping_query(
            uuid, meter_fuel, dest_env, dest_pilot_id, utility_name, source_env
        )
        return self._get_raw_url(f"{self.base_url}{self.CREATE_USER_ENROLL_FILE}?{q}")

    # Step 6
    def create_raw_file(self, uuid, meter_fuel, utility_name, source_env, dest_env, dest_pilot_id):
        q = self._meter_mapping_query(
            uuid, meter_fuel, dest_env, dest_pilot_id, utility_name, source_env
        )
        return self._get_raw_url(f"{self.base_url}{self.CREATE_RAW_FILE}?{q}")

    # Step 7
    def create_billing_file(
        self, uuid, meter_fuel, utility_name, source_env, dest_env, dest_pilot_id
    ):
        q = self._meter_mapping_query(
            uuid,
            meter_fuel,
            dest_env,
            dest_pilot_id,
            utility_name,
            source_env,
            extra="hasBBUserProgram=false",
        )
        return self._get_raw_url(f"{self.base_url}{self.CREATE_BILLING_FILE}?{q}")

    # Step 8
    def fetch_user_uuid(self, dest_pilot_id, dest_env, source_env, uuid):
        return self._get(
            self.FETCH_USER_UUID,
            {
                "destinationPilotId": dest_pilot_id,
                "destinationEnv": dest_env,
                "sourceEnv": source_env,
                "sourceUUID": uuid,
            },
        )

    # Step 9
    def assign_physical_cluster(self, dest_uuid, dest_pilot_id):
        endpoint = (
            f"{self.ASSIGN_PHYSICAL_CLUSTER}?uuid={dest_uuid}&destination_pilot_id={dest_pilot_id}"
        )
        return self._post(endpoint, {})

    # Pre-check helpers
    def get_ported_users(self, dest_pilot_id):
        return self._get(
            self.USER_METADATA, {"destination_pilot_id": dest_pilot_id, "status": "Success"}
        )

    def delete_user_metadata(self, metadata_id):
        r = self.session.delete(
            f"{self.base_url}{self.USER_METADATA}/{metadata_id}", timeout=self.timeout
        )
        r.raise_for_status()
        return r.json()


# --------------------------------------------------------------------------- #
# Flow
# --------------------------------------------------------------------------- #


class FuelValidationError(Exception):
    """Raised when a requested meter fuel isn't available on the source user."""


class AlreadyPortedError(Exception):
    """Raised when the persona already exists on the destination pilot.

    Not a failure: the user is present, so there is nothing to create and
    nothing to retry.
    """


def validate_meter_fuels(client, uuid, source_env, requested_fuels):
    """Confirm every requested fuel is actually present on the source user.

    Raises FuelValidationError (so the caller skips the user without porting) if
    a requested fuel is missing, or if the available fuels can't be determined.
    """
    valid = client.fetch_valid_meter_fuels(source_env, uuid)
    if not valid:
        raise FuelValidationError(
            f"could not determine the source user's meter fuels "
            f"(valid-meter-fuel returned none); requested {requested_fuels}"
        )
    valid_norm = {f.strip().upper() for f in valid}
    missing = [f for f in requested_fuels if f.strip().upper() not in valid_norm]
    if missing:
        raise FuelValidationError(
            f"source user only has {valid}; requested {requested_fuels} "
            f"(missing: {missing})"
        )
    return valid


def _build_metadata(user, prod_data, config):
    """Merge the prod user's fetched details with the per-row overrides into the
    user-metadata payload."""
    detailed = (prod_data.get("detailedUserData") or [{}])[0]
    meter_details = detailed.get("meterDetails", {}) or {}
    meter_fuel = user["meter_fuel"]

    # Rate plan / meter type: use the CSV override if given, else the plan found
    # on the prod user's first fuel that carries one.
    if user["rate_plan"]:
        rate_plan = user["rate_plan"]
        meter_type = meter_fuel[0] if meter_fuel else None
    else:
        rate_plan = None
        meter_type = None
        for fuel in meter_fuel:
            rate_plan = (meter_details.get(fuel, {}) or {}).get("ratePlanId")
            if rate_plan:
                meter_type = fuel
                break

    address = detailed.get("address") or ""
    if detailed.get("city"):
        address += f", {detailed['city'].strip()}"
    if detailed.get("state"):
        address += f", {detailed['state']}"

    # Field set mirrors the DeliveryConsole's own POST /v1/user-metadata request.
    return {
        "first_name": detailed.get("firstName"),
        "last_name": detailed.get("lastName"),
        "rate_plan": rate_plan,
        "billing_cycle": user["billing_cycle"],
        "primary_email": user["primary_email"],
        "secondary_email": "",
        "address": address,
        "meter_type": meter_type,
        "dcx_clusters": "",
        "zipcode": detailed.get("zipCode"),
        "persona_name": user["persona"],
        "persona_description": user["persona_description"],
        "source_uuid": user["source_uuid"],
        "source_pilot_id": str(detailed.get("source_pilot_id", "")),
        "destination_pilot_id": config["DESTINATION_PILOT_ID"],
        "source_env": user["source_environment"],
        "bb_duration": user["bb_duration"],
    }


def _pre_check_delete(client, uuid, dest_pilot_id):
    """Delete any existing Success-status metadata for this source UUID so the
    port can be re-run cleanly."""
    existing = client.get_ported_users(dest_pilot_id).get("data", []) or []
    for u in existing:
        if u.get("source_uuid") == uuid and u.get("id") is not None:
            print(f"    Pre-check: deleting existing ported user metadata id={u['id']}")
            client.delete_user_metadata(u["id"])


def port_user(client, user, config, delete_existing, skip_weather, skip_cluster, check_fuel=True):
    """Run the full CDG flow for one user. Returns the destination UUID."""
    uuid = user["source_uuid"]
    meter_fuel = user["meter_fuel"]
    utility_name = config["UTILITY_NAME"]
    dest_pilot_id = config["DESTINATION_PILOT_ID"]
    dest_env = config["DESTINATION_ENVIRONMENT"]
    source_env = user["source_environment"]

    # Pre-check meter fuels before any mutation (raises FuelValidationError to
    # skip the user if a requested fuel isn't on the source account).
    if check_fuel:
        print("    [pre] validate meter fuels")
        valid = validate_meter_fuels(client, uuid, source_env, meter_fuel)
        print(f"          available: {valid}  requested: {meter_fuel}  ✓")

    if delete_existing:
        _pre_check_delete(client, uuid, dest_pilot_id)

    print("    [1/9] fetchProdUserData")
    prod_data = client.fetch_prod_user_data(
        uuid, utility_name, source_env, config["FILE_UPLOAD_BUCKET"], meter_fuel, dest_pilot_id
    )

    # When the persona already exists on the destination pilot, this step
    # short-circuits: it reports the uuid under `existing_users` and returns no
    # `detailedUserData`. Metadata built from that would be missing the fields
    # the API requires (first_name, address), so the POST would 400 every time
    # - there is nothing to retry. Treat it as already-done instead.
    if not prod_data.get("detailedUserData") and prod_data.get("existing_users"):
        raise AlreadyPortedError(
            prod_data.get("message") or "user persona already exists on this pilot"
        )

    print("    [2/9] updateUserMetadata")
    client.update_user_metadata(_build_metadata(user, prod_data, config))

    if skip_weather:
        print("    [3/9] syncWeatherData (skipped)")
    else:
        print("    [3/9] syncWeatherData")
        try:
            client.sync_weather_data(
                uuid, source_env, dest_env, dest_pilot_id, config["SOURCE_COUNTRY"]
            )
        except requests.exceptions.RequestException as e:
            # Non-blocking: weather sync failure must not abort the port.
            print(f"          weather sync failed (continuing): {e}")

    print("    [4/9] createBCSchedule")
    client.create_billing_cycle_schedule(dest_pilot_id, dest_env, uuid, source_env, utility_name)

    print("    [5/9] createUserEnrollFile")
    client.create_user_enroll_file(
        uuid, meter_fuel, utility_name, source_env, dest_env, dest_pilot_id
    )

    print("    [6/9] createRawFile")
    client.create_raw_file(uuid, meter_fuel, utility_name, source_env, dest_env, dest_pilot_id)

    print("    [7/9] createBillingFile")
    client.create_billing_file(uuid, meter_fuel, utility_name, source_env, dest_env, dest_pilot_id)

    print("    [8/9] fetchUserUUID")
    uuid_resp = client.fetch_user_uuid(dest_pilot_id, dest_env, source_env, uuid)
    dest_uuid = uuid_resp.get("uuid") if uuid_resp.get("status") == "success" else None
    if not dest_uuid:
        raise RuntimeError(
            f"fetchUserUUID did not return a destination UUID (response: {uuid_resp})"
        )

    if skip_cluster:
        print("    [9/9] assignPhysicalCluster (skipped)")
    else:
        print("    [9/9] assignPhysicalCluster")
        client.assign_physical_cluster(dest_uuid, dest_pilot_id)

    return dest_uuid


def process_user(client, user, config, args):
    """Port one user, retrying the whole flow on failure.

    A port can fail part-way through for reasons that clear on their own - a
    slow prod fetch, a gateway timeout, an S3 hiccup - so each user gets
    `args.retries` extra attempts with a growing pause between them. Re-running
    the flow is safe: every step either overwrites or re-posts the same data
    for the same source UUID.

    A meter-fuel failure is *not* retried: the source account simply lacks the
    requested fuel, so another attempt would fail identically.
    """
    label = user["persona"] or user["source_uuid"]
    print(f"\n[row {user['_row']}] {label}")
    print(
        f"    source_uuid={user['source_uuid']}  env={user['source_environment']}  "
        f"rate_plan={user['rate_plan'] or '(from prod)'}  fuel={','.join(user['meter_fuel'])}"
    )
    base = {
        "persona": user["persona"],
        "source_uuid": user["source_uuid"],
        "primary_email": user["primary_email"],
    }

    attempts = max(1, int(args.retries) + 1)
    last_error = None
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            delay = int(args.retry_delay) * (attempt - 1)
            print(f"    ↻ retry {attempt - 1}/{args.retries} in {delay}s")
            time.sleep(delay)
        try:
            dest_uuid = port_user(
                client,
                user,
                config,
                delete_existing=args.delete_existing,
                skip_weather=args.skip_weather,
                skip_cluster=args.skip_cluster,
                check_fuel=not args.skip_fuel_check,
            )
            print(f"    ✓ SUCCESS  destination_uuid={dest_uuid}")
            result = {**base, "destination_uuid": dest_uuid, "status": "success"}
            if attempt > 1:
                result["attempts"] = attempt
            return result
        except AlreadyPortedError as e:
            # Already present on the pilot - nothing to do, nothing to retry.
            print(f"    = ALREADY PORTED  {e}")
            return {**base, "status": "already_ported", "error": str(e)}
        except FuelValidationError as e:
            # Deterministic - the fuel isn't on the source account.
            print(f"    ⚠ SKIPPED (meter-fuel check)  {e}")
            return {**base, "status": "skipped", "error": str(e)}
        except Exception as e:
            last_error = e
            remaining = attempts - attempt
            suffix = f" ({remaining} attempt(s) left)" if remaining else ""
            print(f"    ✗ attempt {attempt}/{attempts} failed: {e}{suffix}")

    print(f"    ✗ FAILED after {attempts} attempt(s)")
    return {
        **base,
        "status": "failed",
        "error": str(last_error),
        "attempts": attempts,
    }


def write_failed_csv(path, results, users):
    """Write the users that did not port, in sources.csv format.

    The output is a drop-in `sources.csv`, so a follow-up run is just
    `cp <this file> sources.csv && uv run python create_users.py` - no hand
    editing. Skipped (meter-fuel) users are included too: they still need
    attention, and the file is the single list of what is left to do.
    """
    unresolved = {
        r["source_uuid"] for r in results if r["status"] in ("failed", "skipped")
    }
    if not unresolved:
        return None

    by_uuid = {u["source_uuid"]: u for u in users}
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(SOURCES_CSV_COLUMNS))
        writer.writeheader()
        for source_uuid in unresolved:
            user = by_uuid.get(source_uuid)
            if not user:
                continue
            writer.writerow(
                {
                    "PERSONA": user["persona"],
                    "PERSONA_DESCRIPTION": user["persona_description"],
                    "SOURCE_UUID": user["source_uuid"],
                    "SOURCE_ENVIRONMENT": user["source_environment"],
                    "RATE_PLAN": user["rate_plan"] or "",
                    "PRIMARY_EMAIL": user["primary_email"],
                    "BILLING_CYCLE": user["billing_cycle"],
                    # Re-join on "|" so a dual-fuel cell survives a round trip.
                    "METER_FUEL": "|".join(user["meter_fuel"]),
                    # Stored as "<n> Month"; the column wants just the number.
                    "BB_DURATION": user["bb_duration"].split()[0],
                }
            )
    return path


def main():
    parser = argparse.ArgumentParser(
        description="Create QA users via the CDG flow from config.json + sources.csv."
    )
    parser.add_argument(
        "--delete-existing",
        action="store_true",
        help="Delete any existing ported user with the same source UUID before porting "
        "(idempotent re-runs).",
    )
    parser.add_argument("--skip-weather", action="store_true", help="Skip the weather-sync step.")
    parser.add_argument(
        "--skip-cluster", action="store_true", help="Skip the physical-cluster assignment step."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N users (handy for a single test user).",
    )
    parser.add_argument(
        "--skip-fuel-check",
        action="store_true",
        help="Skip the meter-fuel pre-check. By default a user is skipped (not ported) if "
        "a requested fuel (e.g. AMI-GAS) isn't available on the source account.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"Extra attempts per user after the first failure (default {DEFAULT_RETRIES}). "
        "A meter-fuel failure is never retried - the source account lacks the fuel. "
        "Use 0 to disable retrying.",
    )
    parser.add_argument(
        "--retry-delay",
        type=int,
        default=DEFAULT_RETRY_DELAY,
        help=f"Seconds to wait before a retry, multiplied by the attempt number "
        f"(default {DEFAULT_RETRY_DELAY}, so 20s then 40s).",
    )
    parser.add_argument(
        "--failed-csv",
        default=str(FAILED_CSV_PATH),
        help="Where to write the users that still did not port, in sources.csv "
        f"format (default {FAILED_CSV_PATH.name}).",
    )
    args = parser.parse_args()

    config = load_config()
    users = load_sources()
    if args.limit is not None:
        users = users[: args.limit]

    if not users:
        print(f"No users found in {SOURCES_CSV_PATH}")
        sys.exit(1)

    print("=" * 70)
    print("CDG User Creation")
    print("=" * 70)
    print(f"DETO base URL : {config['DETO_BASE_URL']}")
    print(
        f"Destination   : pilot {config['DESTINATION_PILOT_ID']} / "
        f"{config['DESTINATION_ENVIRONMENT']} ({config['UTILITY_NAME']})"
    )
    print(f"Users to port : {len(users)}")
    print(f"Delete existing: {args.delete_existing}")
    print(f"Meter-fuel check: {'off' if args.skip_fuel_check else 'on'}")
    print(
        f"Retries       : {args.retries} per user"
        + (f", {args.retry_delay}s backoff" if args.retries else " (disabled)")
    )
    print("=" * 70)

    client = DetoClient(
        config["DETO_BASE_URL"], config["DETO_ACCESS_TOKEN"], timeout=float(config["TIMEOUT"])
    )

    results = [process_user(client, user, config, args) for user in users]

    successful = [r for r in results if r["status"] == "success"]
    already = [r for r in results if r["status"] == "already_ported"]
    skipped = [r for r in results if r["status"] == "skipped"]
    failed = [r for r in results if r["status"] == "failed"]

    report = {
        "timestamp": datetime.now().isoformat(),
        "total": len(results),
        "successful": len(successful),
        "already_ported": len(already),
        "skipped": len(skipped),
        "failed": len(failed),
        "users": results,
    }
    with open(RESULTS_PATH, "w") as f:
        json.dump(report, f, indent=2)

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Total : {len(results)}")
    print(f"✓ Successful          : {len(successful)}")
    print(f"= Already ported      : {len(already)}")
    print(f"⚠ Skipped (fuel check): {len(skipped)}")
    print(f"✗ Failed              : {len(failed)}")

    retried = [r for r in successful if r.get("attempts", 1) > 1]
    if retried:
        print(f"\n{len(retried)} succeeded only after a retry:")
        for r in retried:
            print(f"  - {r['persona'] or r['source_uuid']}: {r['attempts']} attempts")
    if failed:
        print("\nFailed users:")
        for r in failed:
            print(f"  - {r['source_uuid']}: {r['error']}")
    if skipped:
        print("\nSkipped users:")
        for r in skipped:
            print(f"  - {r['source_uuid']}: {r['error']}")

    failed_csv = write_failed_csv(Path(args.failed_csv), results, users)
    print(f"\nResults written to {RESULTS_PATH}")
    if failed_csv:
        print(f"Users still to port : {failed_csv}")
        print(
            f"  Retry just those with: cp {Path(failed_csv).name} sources.csv "
            f"&& uv run python create_users.py"
        )
    else:
        print("Every user ported - no failures file written.")
    print("=" * 70)

    if failed or skipped:
        sys.exit(1)


if __name__ == "__main__":
    main()
