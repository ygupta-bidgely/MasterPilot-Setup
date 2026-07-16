#!/usr/bin/env python3
"""
Create QA users via CDG (Customer Data Generator).

Reads constant destination/DETO settings from config.json and one row per user
from sources.csv, then runs the full CDG porting flow for each row: it pulls the
production user's data, writes user metadata (persona, rate plan, email, billing
cycle), syncs weather, generates the enroll/raw/billing files, and finally reads
back the destination UUID.

Self-contained: depends only on the `requests` library. The DETO endpoints and
payload shapes mirror the qa-utils `cdg` package.
"""

import argparse
import csv
import json
import re
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
SOURCES_CSV_PATH = SCRIPT_DIR / "sources.csv"
RESULTS_PATH = SCRIPT_DIR / "create_users_results.json"

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
    with open(SOURCES_CSV_PATH, newline="") as f:
        reader = csv.DictReader(f)
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


def port_user(client, user, config, delete_existing, skip_weather, skip_cluster):
    """Run the full CDG flow for one user. Returns the destination UUID."""
    uuid = user["source_uuid"]
    meter_fuel = user["meter_fuel"]
    utility_name = config["UTILITY_NAME"]
    dest_pilot_id = config["DESTINATION_PILOT_ID"]
    dest_env = config["DESTINATION_ENVIRONMENT"]
    source_env = user["source_environment"]

    if delete_existing:
        _pre_check_delete(client, uuid, dest_pilot_id)

    print("    [1/9] fetchProdUserData")
    prod_data = client.fetch_prod_user_data(
        uuid, utility_name, source_env, config["FILE_UPLOAD_BUCKET"], meter_fuel, dest_pilot_id
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
    """Port one user, print result, return a result dict."""
    label = user["persona"] or user["source_uuid"]
    print(f"\n[row {user['_row']}] {label}")
    print(
        f"    source_uuid={user['source_uuid']}  env={user['source_environment']}  "
        f"rate_plan={user['rate_plan'] or '(from prod)'}  fuel={','.join(user['meter_fuel'])}"
    )
    try:
        dest_uuid = port_user(
            client,
            user,
            config,
            delete_existing=args.delete_existing,
            skip_weather=args.skip_weather,
            skip_cluster=args.skip_cluster,
        )
        print(f"    ✓ SUCCESS  destination_uuid={dest_uuid}")
        return {
            "persona": user["persona"],
            "source_uuid": user["source_uuid"],
            "primary_email": user["primary_email"],
            "destination_uuid": dest_uuid,
            "status": "success",
        }
    except Exception as e:
        print(f"    ✗ FAILED  {e}")
        return {
            "persona": user["persona"],
            "source_uuid": user["source_uuid"],
            "primary_email": user["primary_email"],
            "status": "failed",
            "error": str(e),
        }


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
    print("=" * 70)

    client = DetoClient(
        config["DETO_BASE_URL"], config["DETO_ACCESS_TOKEN"], timeout=float(config["TIMEOUT"])
    )

    results = [process_user(client, user, config, args) for user in users]

    successful = [r for r in results if r["status"] == "success"]
    failed = [r for r in results if r["status"] != "success"]

    report = {
        "timestamp": datetime.now().isoformat(),
        "total": len(results),
        "successful": len(successful),
        "failed": len(failed),
        "users": results,
    }
    with open(RESULTS_PATH, "w") as f:
        json.dump(report, f, indent=2)

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Total : {len(results)}")
    print(f"✓ Successful : {len(successful)}")
    print(f"✗ Failed     : {len(failed)}")
    print(f"Results written to {RESULTS_PATH}")
    print("=" * 70)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
