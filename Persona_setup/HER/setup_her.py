#!/usr/bin/env python3
"""
Set up a HER (Home Energy Report) persona for one user.

Run with no arguments for an interactive prompt that asks which HER to set up
(monthly, seasonal, or both) and then runs the matching flow. Any flag or
subcommand skips the prompts.

`setup` - the monthly flow, end to end:

  1. Checks the pilot's her_moderation config (her_base_url/her_vendor_bucket)
     and relaxes neighbourhood_comparison.data_point_threshold.
  2. Renders the nbi_asset_data SQL for the DB (you run it yourself).
  3. Fills in the user's last completed billing cycle and uploads the
     interactions payload to S3.
  4. Pushes the NBI string resources (title/shortText/longText).
  5. SHC 2.0 mock pipeline (Confluence PM/1455620097): fetches the pilot's
     full user list (ported + ingested), splits it into clusters, uploads the
     mock diff/total/cluster-info files to S3, and renders the
     NeighbourhoodPostProcessingRunner command (run it yourself, on the
     nhoodServices machine) - this is what backs the HER report's SHC
     (social/home-comparison) insights.

Other subcommands:
  - `seasonal --batch N`: derives the Summer/Winter seasonal NBIs from the
    user's monthly NBI (or the bundled templates) and uploads them.
  - `verify`: checks the cluster API and (optionally) a user's defNhoodId.
  - `set-threshold VALUE`: sets neighbourhood_comparison.data_point_threshold
    (e.g. 0 for relaxed testing; remember to revert to 20 for prod).

Reads BASE_URL / AUTH_TOKEN / PILOT_ID / ENVIRONMENT / FUEL_TYPE / UUID /
DETO_BASE_URL / DETO_TOKEN from the shared `Persona_setup/config.json` (plus
optional HOME_ID, NHOOD_JAR_PATH and FEATURE_METADATA_S3_PATH). The HER-only
keys may live at the top level or under `scripts.HER`. Everything else is
derived at runtime rather than configured:

  - HER payload `batch=`  -> the oldest billing-cycle start for the user.
  - SHC `batch_id=`       -> the highest existing batch id in S3, plus one
                             (111 when the pilot has none yet).
  - cluster count         -> one cluster per 20 users, minimum 1.
  - queue suffix          -> the project name inside BASE_URL's host.
  - java -Dmy.env         -> ENVIRONMENT.

Templates live in templates/ and are rendered per-run into output/
(git-ignored).

Self-contained: only needs the `requests` library plus the `aws` CLI on PATH
(already configured on this machine) for S3 reads/uploads.
"""

import argparse
import contextlib
import csv
import json
import re
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import requests

try:
    import pymysql
except ImportError:  # the DB step is optional; everything else still runs
    pymysql = None

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import SetupError as SharedSetupError  # noqa: E402
from _common import load as load_shared_config  # noqa: E402
from _common.config import CONFIG_PATH as SHARED_CONFIG_PATH  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPT_NAME = "HER"
TEMPLATES_DIR = SCRIPT_DIR / "templates"
OUTPUT_DIR = SCRIPT_DIR / "output"

INTERACTIONS_TEMPLATE = TEMPLATES_DIR / "interactions.json"
SQL_TEMPLATE = TEMPLATES_DIR / "nbi_asset_data.sql.template"
STRINGS_TEMPLATE = TEMPLATES_DIR / "set_string_resources.sh.template"
# The SHC feature-metadata yaml, uploaded to FEATURE_METADATA_S3_PATH and passed
# to the runner as -featureMetaDataFilePath.
FEATURE_CONFIG_TEMPLATE = TEMPLATES_DIR / "demo_shc.yaml"

DEFAULT_TIMEOUT = 30.0

# Fixed segments of the HER interaction-profile S3 layout; only env/uuid/
# deliveryType/fuelType/batch vary.
S3_PATH_TEMPLATE = (
    "s3://bidgely-profile-data-{env}/home/mode=batch/profile=interaction/"
    "uuid={uuid}/source=DISAGG/nbiRun=HER/deliveryMode=Paper/"
    "deliveryType={delivery_type}/fuelType={fuel_type}/batch={batch}/"
)

MONTHLY_DELIVERY_TYPE = "HER_MONTHLY_REPORT"
# season -> (deliveryType path segment, the nbiType every interaction gets)
SEASONS = {
    "summer": ("HER_SEASONAL_SUMMER", "SummerSeasonal"),
    "winter": ("HER_SEASONAL_WINTER", "WinterSeasonal"),
}
REPORT_TYPE_MAPPING_KEY = "report_type_nbi_run_mapping"
# What report_type_nbi_run_mapping must contain for seasonal HER to aggregate.
EXPECTED_REPORT_TYPE_MAPPINGS = (
    "HER_MONTHLY_REPORT|HER",
    "HER_SEASONAL_SUMMER|HER",
    "HER_SEASONAL_WINTER|HER",
)

BILLING_CYCLES_PATH = "/billingdata/users/{uuid}/homes/{home_id}/billingcycles"

# Pilot config GET/POST use different paths (GET has a "pilot" segment, POST doesn't).
PILOT_CONFIGS_GET_PATH = "/entities/pilot/{pilot_id}/configs"
PILOT_CONFIGS_POST_PATH = "/entities/{pilot_id}/configs"
HER_MODERATION_CONFIG_TYPE = "her_moderation"
S3_PULL_CONFIG_TYPE = "s3_pull"

# Fixed metadata the update API expects alongside each configKey/configVal.
HER_MODERATION_CONFIG_META = {
    "her_base_url": {
        "configDocumentation": "Her Base Url",
        "configRegex": ".*",
        "configDataType": "TEXT",
    },
    "her_vendor_bucket": {
        "configDocumentation": "Her Vendor Bucket",
        "configRegex": ".*",
        "configDataType": "TEXT",
    },
    REPORT_TYPE_MAPPING_KEY: {
        "configDocumentation": "Report Type NBI Run Mapping",
        "configRegex": ".*",
        "configDataType": "TEXT",
    },
}

# --- SHC 2.0 mock pipeline (Confluence PM/1455620097) ---------------------- #

FETCH_USER_ATTRIBUTE_PATH = "/v1/fetch-user-attribute"
INGESTED_USERS_PATH = "/v1/ingested-users"

# SHC-2 data lives in the data warehouse for the pilot's *own* env. The
# Confluence doc hardcodes the uat bucket because that's the env it was written
# against; a productqa pilot reads/writes bidgely-data-warehouse-productqa.
SHC_BUCKET_TEMPLATE = "bidgely-data-warehouse-{env}"


def shc_base_prefix(environment):
    """S3 prefix for this env's SHC-2 tree, e.g. productqa ->
    s3://bidgely-data-warehouse-productqa/shc-2."""
    return f"s3://{SHC_BUCKET_TEMPLATE.format(env=environment)}/shc-2"


NC_CONFIG_TYPE = "neighbourhood_comparison"
DATA_POINT_THRESHOLD_KEY = "data_point_threshold"
# 0 relaxes SHC generation entirely (for mock/test pilots); prod wants 20.
TEST_DATA_POINT_THRESHOLD = "0"
PROD_DATA_POINT_THRESHOLD = "20"

# One cluster per full block of this many users (min 1 cluster overall).
USERS_PER_CLUSTER = 20
# Used when the pilot has no batch_id folder in S3 yet.
FALLBACK_SHC_BATCH_ID = "111"
DEFAULT_NHOOD_JAR_PATH = "/opt/bidgely/nhoodServices/onelib/OneJar-core-nhoods-4.0-SNAPSHOT.jar"
# Where the manually-uploaded demo_shc.yaml lives; passed as
# -featureMetaDataFilePath. Overrides the Confluence doc's shared
# shc-2/configs/demo_shc.yaml so runs use our own copy.
DEFAULT_FEATURE_METADATA_S3_PATH = "s3://bidgely-artifacts2/yash/demo_shc.yaml"

# --- DB (nbi_asset_data) --------------------------------------------------- #

# The RDS instances sit on private VPC addresses, so every connection goes
# through the env's jumphost - the same tunnel DBeaver opens.
DEFAULT_DB_SSH_HOST_TEMPLATE = "jumphost-{env}.bidgely.com"
DEFAULT_DB_SSH_KEY = "~/.ssh/id_ed25519"
DEFAULT_DB_PORT = 3306
NBI_ASSET_TABLE = "nbi_asset_data"
DB_CONFIG_KEYS = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_SSH_USER")
# Pulls the six values out of each INSERT in the rendered SQL. None of the
# values contain embedded double quotes, so this stays unambiguous.
SQL_VALUES_RE = re.compile(
    r'VALUES\s*\(\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,'
    r'\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"\s*\)'
)


def load_config():
    shared = load_shared_config(SCRIPT_NAME)
    users = shared.users_for(SCRIPT_NAME)
    if not users:
        raise ValueError(
            f"No users configured for {SCRIPT_NAME}. Add one to USERS in "
            f'{SHARED_CONFIG_PATH} with "scripts": ["{SCRIPT_NAME}"].'
        )
    if len(users) > 1:
        raise ValueError(
            f"{len(users)} users configured for {SCRIPT_NAME}; this script runs "
            f"one user at a time. Use run_personas.py or narrow the config."
        )

    config = {
        "UUID": users[0]["UUID"],
        "AUTH_TOKEN": shared.token,
        "BASE_URL": shared.base_url,
        "PILOT_ID": shared.pilot_id,
    }

    # HER-only settings. They may sit at the top level of the shared config or
    # under scripts.HER; `shared.get` resolves the per-script override first.
    for key in ("ENVIRONMENT", "FUEL_TYPE", "DETO_BASE_URL", "DETO_TOKEN"):
        value = shared.get(key)
        if not value or not str(value).strip():
            raise ValueError(
                f"Missing required setting {key!r} in {SHARED_CONFIG_PATH} "
                f"(set scripts.{SCRIPT_NAME}.{key} or the top-level {key})"
            )
        config[key] = value

    config["HOME_ID"] = shared.get("HOME_ID", "1")
    config["NHOOD_JAR_PATH"] = shared.get("NHOOD_JAR_PATH", DEFAULT_NHOOD_JAR_PATH)
    config["FEATURE_METADATA_S3_PATH"] = shared.get(
        "FEATURE_METADATA_S3_PATH", DEFAULT_FEATURE_METADATA_S3_PATH
    )
    # 0 = fully relaxed SHC generation, which is what a mock/test pilot wants.
    # Push PROD_DATA_POINT_THRESHOLD before a pilot goes to prod.
    config["DATA_POINT_THRESHOLD"] = shared.get(
        "DATA_POINT_THRESHOLD", TEST_DATA_POINT_THRESHOLD
    )

    # DB access is optional - the step is skipped when these aren't filled in.
    for key in DB_CONFIG_KEYS:
        config[key] = shared.get(key)
    config["DB_PORT"] = shared.get("DB_PORT", DEFAULT_DB_PORT)
    config["DB_SSH_KEY"] = shared.get("DB_SSH_KEY", DEFAULT_DB_SSH_KEY)
    config["DB_SSH_HOST"] = shared.get(
        "DB_SSH_HOST", DEFAULT_DB_SSH_HOST_TEMPLATE.format(env=config["ENVIRONMENT"])
    )
    return config


def db_config_missing(config):
    """Which DB keys still need filling in. Empty list = good to go."""
    return [k for k in DB_CONFIG_KEYS if not config.get(k)]


def derive_queue_suffix(base_url, environment):
    """The queue suffix is the project name embedded in the API host: the part
    between the "api-server-" prefix and the "-<env>" suffix, e.g.
    https://api-server-masterpilot-productqa.bidgely.com -> masterpilot."""
    host = urllib.parse.urlparse(base_url).netloc or base_url
    host = host.split(".")[0]
    if host.startswith("api-server-"):
        host = host[len("api-server-") :]
    env = str(environment).strip()
    if env and host.endswith(f"-{env}"):
        host = host[: -len(f"-{env}")]
    return host


# --------------------------------------------------------------------------- #
# her_moderation config sync
# --------------------------------------------------------------------------- #


def render_sql(pilot_id):
    """Render nbi_asset_data.sql.template with this pilot's entity_id."""
    text = SQL_TEMPLATE.read_text().replace("{{PILOT_ID}}", str(pilot_id))
    out_path = OUTPUT_DIR / f"nbi_asset_data_{pilot_id}.sql"
    out_path.write_text(text)
    return out_path


def render_strings_script(pilot_id):
    """Render set_string_resources.sh.template with this pilot's id."""
    text = STRINGS_TEMPLATE.read_text().replace("{{PILOT_ID}}", str(pilot_id))
    out_path = OUTPUT_DIR / f"set_string_resources_{pilot_id}.sh"
    out_path.write_text(text)
    out_path.chmod(0o755)
    return out_path


def fetch_billing_cycles(base_url, auth_token, uuid, home_id, fuel_type):
    """All of the user's billing cycles as [{"key": start, "value": end}, ...]
    in epoch seconds. GAS reads pass the `measurementType: GAS` header;
    everything else (electric) omits it."""
    now = int(time.time())
    # t1 just needs to comfortably cover the user's most recent cycle.
    t1 = now + 6 * 365 * 24 * 3600
    url = f"{base_url.rstrip('/')}{BILLING_CYCLES_PATH.format(uuid=uuid, home_id=home_id)}"
    headers = {"Authorization": f"bearer {auth_token}"}
    if fuel_type.strip().upper() == "GAS":
        headers["measurementType"] = "GAS"

    response = requests.get(
        url, headers=headers, params={"t0": 1, "t1": t1}, timeout=DEFAULT_TIMEOUT
    )
    response.raise_for_status()
    cycles = response.json()
    if not cycles:
        raise ValueError(f"No billing cycles returned for uuid={uuid} home={home_id}")
    return cycles


def pick_last_completed_cycle(cycles):
    """The most recently *completed* cycle - i.e. the one with the latest end
    time that is already in the past."""
    now = int(time.time())
    completed = [c for c in cycles if c["value"] <= now]
    if completed:
        return max(completed, key=lambda c: c["value"])
    # No cycle has finished yet - fall back to the most recent one available.
    return max(cycles, key=lambda c: c["value"])


def oldest_cycle_start(cycles):
    """The earliest billing-cycle start across every cycle - used as the HER
    payload's S3 `batch=` segment."""
    return min(c["key"] for c in cycles)


def fetch_pilot_configs(base_url, auth_token, pilot_id):
    """GET /entities/pilot/{pilot_id}/configs. Returns the raw response dict,
    keyed by configType, each value a JSON-encoded string.

    Must be a GET with no body: POSTing an empty body to this path makes the
    server's ConfigFilter blow up with a 500 ("entityConfig is null")."""
    url = f"{base_url.rstrip('/')}{PILOT_CONFIGS_GET_PATH.format(pilot_id=pilot_id)}"
    headers = {"Authorization": f"Bearer {auth_token}"}
    response = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _config_kvs(pilot_configs, config_type):
    """Each configType value in the pilot configs response is itself a
    JSON-encoded string of {"kvs": [{"key":..., "val":...}, ...]}. Returns it
    as a plain {key: val} dict."""
    raw = pilot_configs.get(config_type)
    if raw is None:
        raise ValueError(f"'{config_type}' not present in the pilot configs response")
    parsed = json.loads(raw)
    return {kv["key"]: kv.get("val") for kv in parsed.get("kvs", [])}


def expected_her_base_url(base_url):
    """The HER base URL mirrors the API base URL with the "api-server-" prefix
    dropped, e.g. https://api-server-masterpilot-productqa.bidgely.com ->
    https://masterpilot-productqa.bidgely.com."""
    return base_url.replace("api-server-", "", 1)


def sync_her_moderation_config(base_url, auth_token, pilot_id, dry_run):
    """Compare the pilot's her_moderation her_base_url/her_vendor_bucket against
    what they should be (her_base_url derived from BASE_URL; her_vendor_bucket
    from the pilot's own s3_pull.s3DestinationBucket) and push only the ones
    that differ."""
    print(f"\n[pilot config] checking her_base_url / her_vendor_bucket for pilot {pilot_id}")
    configs = fetch_pilot_configs(base_url, auth_token, pilot_id)
    her_moderation = _config_kvs(configs, HER_MODERATION_CONFIG_TYPE)
    s3_pull = _config_kvs(configs, S3_PULL_CONFIG_TYPE)

    wanted_base_url = expected_her_base_url(base_url)
    wanted_vendor_bucket = s3_pull.get("s3DestinationBucket")
    if wanted_vendor_bucket is None:
        raise ValueError("'s3DestinationBucket' not found in the pilot's 's3_pull' config")

    current_base_url = her_moderation.get("her_base_url")
    current_vendor_bucket = her_moderation.get("her_vendor_bucket")

    updates = []
    if current_base_url != wanted_base_url:
        print(f"  her_base_url mismatch: {current_base_url!r} -> {wanted_base_url!r}")
        updates.append(("her_base_url", wanted_base_url))
    else:
        print(f"  her_base_url OK ({current_base_url})")

    if current_vendor_bucket != wanted_vendor_bucket:
        print(
            f"  her_vendor_bucket mismatch: {current_vendor_bucket!r} -> {wanted_vendor_bucket!r}"
        )
        updates.append(("her_vendor_bucket", wanted_vendor_bucket))
    else:
        print(f"  her_vendor_bucket OK ({current_vendor_bucket})")

    if not updates:
        print("  no changes needed")
        return True

    print(f"  updating: {[key for key, _ in updates]}")
    if dry_run:
        print("  (dry-run, not executed)")
        return True

    config_kvs = [
        {"configKey": key, "configVal": val, **HER_MODERATION_CONFIG_META[key]}
        for key, val in updates
    ]
    url = f"{base_url.rstrip('/')}{PILOT_CONFIGS_POST_PATH.format(pilot_id=pilot_id)}"
    headers = {"Content-Type": "application/json", "Authorization": f"bearer {auth_token}"}
    response = requests.post(
        url,
        headers=headers,
        json={"configType": HER_MODERATION_CONFIG_TYPE, "configKVs": config_kvs},
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code not in (200, 201):
        print(f"  ✗ update failed: HTTP {response.status_code} - {response.text[:300]}")
        return False
    print("  ✓ updated")
    return True


def apply_billing_info(payload, cycles, fuel_type):
    """Fill the billing block matching fuel_type with the user's last completed
    cycle; the other block is reset to -1/-1. Mutates and returns payload."""
    billing_info = payload.setdefault("nbi_delivery_helper_dict", {}).setdefault("billing_info", {})
    billing_info["last_electric_billing_cycle_info"] = {
        "last_billing_start": -1,
        "last_billing_end": -1,
    }
    billing_info["last_gas_billing_cycle_info"] = {"last_billing_start": -1, "last_billing_end": -1}

    is_gas = fuel_type.strip().upper() == "GAS"
    target_key = "last_gas_billing_cycle_info" if is_gas else "last_electric_billing_cycle_info"

    cycle = pick_last_completed_cycle(cycles)
    billing_info[target_key] = {
        "last_billing_start": cycle["key"],
        "last_billing_end": cycle["value"],
    }
    print(f"[billing cycle] {target_key}: start={cycle['key']} end={cycle['value']}")
    return payload


def render_interactions_payload(uuid, cycles, fuel_type):
    """Write the interactions payload, named for the destination user's uuid,
    with the last completed billing cycle filled into the block matching this
    run's fuel type (the other block stays at -1/-1)."""
    payload = apply_billing_info(
        json.loads(INTERACTIONS_TEMPLATE.read_text()), cycles, fuel_type
    )
    out_path = OUTPUT_DIR / f"{uuid}.json"
    out_path.write_text(json.dumps(payload, indent=2))
    return out_path


def run_strings_script(script_path, base_url, auth_token, dry_run):
    print(f"\n[string resources] {script_path.name} -> {base_url}")
    if dry_run:
        print("  (dry-run, not executed)")
        return True
    result = subprocess.run(["bash", str(script_path), base_url, auth_token])
    if result.returncode != 0:
        print(f"  ✗ script exited with status {result.returncode}")
        return False
    print("  ✓ done")
    return True


def s3_object_exists(dest):
    """dest is a full s3://bucket/key path. Uses head-object (exact key match)
    rather than `s3 ls` (which does a prefix match)."""
    bucket, _, key = dest[len("s3://") :].partition("/")
    result = subprocess.run(
        ["aws", "s3api", "head-object", "--bucket", bucket, "--key", key],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def upload_file_to_s3(local_path, dest, dry_run, content_type=None, force=False):
    print(f"\n[s3 upload] {local_path} -> {dest}")
    if not force and s3_object_exists(dest):
        print("  already exists at destination, skipping upload")
        return True
    if dry_run:
        print("  (dry-run, not executed)")
        return True
    cmd = ["aws", "s3", "cp", str(local_path), dest]
    if content_type:
        cmd += ["--content-type", content_type]
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"  ✗ aws s3 cp exited with status {result.returncode}")
        return False
    print("  ✓ uploaded")
    return True


def download_from_s3(src, local_path):
    """Pull one object down. Returns True on success."""
    print(f"[s3 download] {src}")
    result = subprocess.run(
        ["aws", "s3", "cp", src, str(local_path), "--quiet"], capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  ✗ aws s3 cp failed: {result.stderr.strip()[:300]}")
        return False
    print(f"  ✓ saved to {local_path}")
    return True


def upload_persona_payload(local_path, env, uuid, fuel_type, batch, dry_run):
    prefix = S3_PATH_TEMPLATE.format(
        env=env,
        uuid=uuid,
        delivery_type=MONTHLY_DELIVERY_TYPE,
        fuel_type=fuel_type,
        batch=batch,
    )
    return upload_file_to_s3(local_path, f"{prefix}{local_path.name}", dry_run)


# --------------------------------------------------------------------------- #
# SHC 2.0 mock pipeline
# --------------------------------------------------------------------------- #


def fetch_ported_uuids(deto_base_url, deto_token, pilot_id):
    """DETO fetch-user-attribute - CDG/ported users. Returns only uuids whose
    status is "success"."""
    url = f"{deto_base_url.rstrip('/')}{FETCH_USER_ATTRIBUTE_PATH}"
    headers = {"Authorization": f"Bearer {deto_token}", "Accept": "application/json"}
    response = requests.get(
        url, headers=headers, params={"destination_pilot_id": pilot_id}, timeout=DEFAULT_TIMEOUT
    )
    response.raise_for_status()
    results = response.json().get("results", []) or []
    return [r["uuid"] for r in results if r.get("status") == "success" and r.get("uuid")]


def fetch_ingested_uuids(deto_base_url, deto_token, pilot_id, page_size=100):
    """DETO ingested-users - organically-ingested users. Paginates until
    pagination.hasNext is false."""
    url = f"{deto_base_url.rstrip('/')}{INGESTED_USERS_PATH}"
    headers = {"Authorization": f"Bearer {deto_token}", "Accept": "application/json"}
    uuids = []
    page = 1
    while True:
        response = requests.get(
            url,
            headers=headers,
            params={"projectId": pilot_id, "limit": page_size, "page": page},
            timeout=DEFAULT_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        uuids.extend(u["uuid"] for u in data.get("users", []) or [] if u.get("uuid"))
        pagination = data.get("pagination") or {}
        if not pagination.get("hasNext"):
            break
        page += 1
    return uuids


def fetch_all_pilot_uuids(deto_base_url, deto_token, pilot_id):
    ported = fetch_ported_uuids(deto_base_url, deto_token, pilot_id)
    ingested = fetch_ingested_uuids(deto_base_url, deto_token, pilot_id)
    print(f"[shc users] ported (fetch-user-attribute, success): {len(ported)}")
    print(f"[shc users] ingested (ingested-users, all pages): {len(ingested)}")
    combined = sorted(set(ported) | set(ingested))
    print(f"[shc users] combined unique total: {len(combined)}")
    return combined


def _list_s3_batch_ids(prefix):
    """Numeric batch ids of any `batch_id=<n>/` folders directly under prefix.
    Empty list if the prefix doesn't exist yet or aws can't read it."""
    result = subprocess.run(["aws", "s3", "ls", prefix], capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return [int(m.group(1)) for m in re.finditer(r"batch_id=(\d+)/", result.stdout)]


def next_shc_batch_id(pilot_id, environment):
    """One past the highest batch id already in S3 for this pilot, or 111 if it
    has none. Completed runs get moved into a `processed/` subfolder, so both
    levels are checked - otherwise a pilot that has already been run once would
    look empty and reuse an old id."""
    prefix = shc_base_prefix(environment)
    base = f"{prefix}/algo-outputs/outputs/diff/pilot_id={pilot_id}/ntype={pilot_id}/"
    existing = _list_s3_batch_ids(base) + _list_s3_batch_ids(f"{base}processed/")
    if not existing:
        print(
            f"[shc] no existing batch_id in S3 for pilot {pilot_id} "
            f"- using {FALLBACK_SHC_BATCH_ID}"
        )
        return FALLBACK_SHC_BATCH_ID
    latest = max(existing)
    next_id = str(latest + 1)
    print(f"[shc] latest existing batch_id={latest} -> using {next_id}")
    return next_id


def cluster_count_for(total):
    """One cluster per full block of USERS_PER_CLUSTER users, minimum 1: 38
    users -> 1 cluster, 40 -> 2, 52 -> 2. Users are then spread evenly across
    those clusters, so no cluster ends up under USERS_PER_CLUSTER."""
    return max(1, total // USERS_PER_CLUSTER)


def assign_clusters(uuids, pilot_id, num_clusters):
    """Split the (already sorted) uuid list into num_clusters chunks, as
    evenly as possible - the first (total % num_clusters) chunks get one
    extra user. Cluster ids follow the pilot's own "<pilot_id><3-digit index>"
    convention seen in the existing mock data (e.g. 88001 -> 88001001)."""
    total = len(uuids)
    base, remainder = divmod(total, num_clusters)
    assignments = []  # (uuid, cluster_id) in original order
    cluster_counts = {}
    start = 0
    for i in range(num_clusters):
        size = base + (1 if i < remainder else 0)
        cluster_id = f"{pilot_id}{i + 1:03d}"
        for uuid in uuids[start : start + size]:
            assignments.append((uuid, cluster_id))
        cluster_counts[cluster_id] = size
        start += size
    return assignments, cluster_counts


def write_dataframe_csv(path, assignments):
    """Matches the pandas-style default to_csv(index=True) shape used by the
    existing mock files: a blank header cell for the index column."""
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["", "uuid", "cluster_id"])
        for i, (uuid, cluster_id) in enumerate(assignments):
            writer.writerow([i, uuid, cluster_id])


def write_cluster_info_json(path, cluster_counts, fuel_type):
    mapping = {
        cluster_id: {
            "name": f"fuel_type_key:{fuel_type}",
            "fuelTypeTag": fuel_type,
            "user_count": count,
            "cluster_id": cluster_id,
        }
        for cluster_id, count in cluster_counts.items()
    }
    path.write_text(json.dumps(mapping, indent=2))


def upload_feature_metadata_file(feature_path, dry_run):
    """Push the SHC feature-metadata yaml to S3 for -featureMetaDataFilePath.
    The nhood runner dies with `yamlJsonNode is null` if this object isn't
    there, so it has to exist before the command is run. Skipped when the
    object already exists, so a hand-tuned S3 copy is never clobbered."""
    if not FEATURE_CONFIG_TEMPLATE.exists():
        print(f"\n[shc] ⚠ {FEATURE_CONFIG_TEMPLATE} is missing - cannot upload the feature yaml.")
        print(f"       The nhood command will fail without {feature_path}")
        return False
    return upload_file_to_s3(FEATURE_CONFIG_TEMPLATE, feature_path, dry_run)


def render_nhood_command(
    pilot_id, shc_batch_id, env_label, queue_suffix, jar_path, feature_metadata_path
):
    prefix = shc_base_prefix(env_label)
    outputs = f"{prefix}/algo-outputs/outputs"
    dataprep = f"{prefix}/dataprep"
    args = [
        f"java -Dmy.env={env_label} -Dqueue.suffix={queue_suffix} -cp {jar_path}",
        "com.bidgely.cloud.core.nhoods.NeighbourhoodPostProcessingRunner",
        f"-ntype {pilot_id}",
        f"-shcDiffPath {outputs}/diff/pilot_id={pilot_id}/ntype={pilot_id}/",
        f"-totalOutputPath {outputs}/total/pilot_id={pilot_id}/ntype={pilot_id}/",
        f"-shcClusterInfoPath {outputs}/cluster_info/pilot_id={pilot_id}/ntype={pilot_id}/",
        f"-intermediateCombinedDataprepOutputPath {dataprep}/intermediate-combined-outputs/"
        f"pilot_id={pilot_id}/ntype={pilot_id}/batch_id={shc_batch_id}",
        f"-finalCombinedDataprepOutputPath {dataprep}/final-combined-output/"
        f"pilot_id={pilot_id}/ntype={pilot_id}",
        f"-featureMetaDataFilePath {feature_metadata_path}",
        f"-isExperimental false -experimentVersion {shc_batch_id}",
    ]
    return " \\\n  ".join(args) + "\n"


def run_shc_setup(config, dry_run):
    """Fetch the pilot's users, build the mock diff/total/cluster-info files,
    upload them to S3, and render the nhood command. Returns (ok, warning)."""
    pilot_id = str(config["PILOT_ID"])
    environment = config["ENVIRONMENT"]

    print("\n" + "-" * 70)
    print("[shc] SHC 2.0 mock pipeline")
    print("-" * 70)

    uuids = fetch_all_pilot_uuids(config["DETO_BASE_URL"], config["DETO_TOKEN"], pilot_id)
    if not uuids:
        print("[shc] no users found for this pilot - skipping SHC file generation.")
        return True

    shc_batch_id = next_shc_batch_id(pilot_id, environment)
    num_clusters = cluster_count_for(len(uuids))
    print(
        f"[shc] {len(uuids)} users / {USERS_PER_CLUSTER} per cluster "
        f"-> {num_clusters} cluster(s)"
    )

    assignments, cluster_counts = assign_clusters(uuids, pilot_id, num_clusters)
    print(f"[shc] clusters: {cluster_counts}")

    fuel_type_title = config["FUEL_TYPE"].strip().capitalize()
    diff_path = OUTPUT_DIR / "diff_dataframe.csv"
    total_path = OUTPUT_DIR / "output_dataframe.csv"
    cluster_info_path = OUTPUT_DIR / "cluster_id_name_mapping.json"
    write_dataframe_csv(diff_path, assignments)
    write_dataframe_csv(total_path, assignments)
    write_cluster_info_json(cluster_info_path, cluster_counts, fuel_type_title)
    print(f"[shc] wrote {diff_path}")
    print(f"[shc] wrote {total_path}")
    print(f"[shc] wrote {cluster_info_path}")

    ok = True
    base = f"{shc_base_prefix(environment)}/algo-outputs/outputs"
    ok = upload_file_to_s3(
        diff_path,
        f"{base}/diff/pilot_id={pilot_id}/ntype={pilot_id}/batch_id={shc_batch_id}/diff_dataframe.csv",
        dry_run,
    ) and ok
    ok = upload_file_to_s3(
        total_path,
        f"{base}/total/pilot_id={pilot_id}/ntype={pilot_id}/batch_id={shc_batch_id}/output_dataframe.csv",
        dry_run,
    ) and ok
    ok = upload_file_to_s3(
        cluster_info_path,
        f"{base}/cluster_info/pilot_id={pilot_id}/ntype={pilot_id}/batch_id={shc_batch_id}/"
        "cluster_id_name_mapping.json",
        dry_run,
    ) and ok

    feature_metadata_path = config["FEATURE_METADATA_S3_PATH"]
    ok = upload_feature_metadata_file(feature_metadata_path, dry_run) and ok

    queue_suffix = derive_queue_suffix(config["BASE_URL"], config["ENVIRONMENT"])
    print(f"[shc] queue suffix (from BASE_URL): {queue_suffix}")
    nhood_command = render_nhood_command(
        pilot_id,
        shc_batch_id,
        config["ENVIRONMENT"],
        queue_suffix,
        config["NHOOD_JAR_PATH"],
        feature_metadata_path,
    )

    # The command goes to a file as well as the log, so it can be copied to the
    # nhoodServices machine without scraping it out of the console output.
    command_path = OUTPUT_DIR / f"run_nhood_postprocessing_{pilot_id}_{shc_batch_id}.sh"
    header = [
        "#!/usr/bin/env bash",
        "# NeighbourhoodPostProcessingRunner for the SHC 2.0 mock run.",
        f"# pilot_id={pilot_id}  batch_id={shc_batch_id}  env={config['ENVIRONMENT']}",
        "# Run this ON the machine where nhoodServices is installed.",
        "set -euo pipefail",
        "",
    ]
    command_path.write_text("\n".join(header) + "\n" + nhood_command)
    command_path.chmod(0o755)

    print(f"\n[shc] nhood command written to {command_path}")
    print("  Run it on the machine where nhoodServices is installed (not executed here):")
    print(f"\n{nhood_command}")
    print(
        "  Note: if re-running, make sure the batch_id folder sits outside any "
        "'processed/' subfolder under the diff/total/cluster_info paths above."
    )
    print(
        "  After running it: `setup_her.py verify --uuid <uuid>`, then trigger ncRunner on the "
        "datacubejobs machine, then re-verify."
    )
    return ok


# --------------------------------------------------------------------------- #
# Seasonal HER (derive SummerSeasonal / WinterSeasonal from the monthly NBI)
# --------------------------------------------------------------------------- #


def seasonal_template_path(season):
    """Pre-built seasonal payload shipped in templates/ - the same shape as the
    monthly interactions.json but with every nbiType already set for that
    season. Used by --from-template when there's no monthly NBI in S3."""
    return TEMPLATES_DIR / f"interactions_{season}_seasonal.json"


def latest_monthly_batch(env, uuid, fuel_type):
    """Newest `batch=<n>/` folder under the user's HER_MONTHLY_REPORT prefix -
    the monthly NBI the seasonal files get derived from."""
    prefix = S3_PATH_TEMPLATE.format(
        env=env, uuid=uuid, delivery_type=MONTHLY_DELIVERY_TYPE, fuel_type=fuel_type, batch=""
    ).rsplit("batch=", 1)[0]
    result = subprocess.run(["aws", "s3", "ls", prefix], capture_output=True, text=True)
    # `aws s3 ls` exits 1 on a prefix that doesn't exist, which is the same
    # situation as "no batches yet" - only treat real stderr output as an error.
    stderr = result.stderr.strip()
    if result.returncode != 0 and stderr:
        raise ValueError(f"could not list monthly batches under {prefix}: {stderr[:300]}")
    batches = [int(m.group(1)) for m in re.finditer(r"batch=(\d+)/", result.stdout)]
    if not batches:
        raise ValueError(
            f"no HER_MONTHLY_REPORT batches found under {prefix}\n"
            "       Run `setup_her.py setup` first so a monthly NBI exists to derive from, "
            "or pass --source-batch explicitly."
        )
    return str(max(batches))


def build_seasonal_payload(monthly, nbi_type):
    """The monthly NBI with every interaction's nbiType rewritten. Nothing else
    changes - scores, hashes, insight/action blocks and the billing helper dict
    are carried over as-is."""
    payload = json.loads(json.dumps(monthly))  # deep copy
    for interaction in payload.get("interactions", []):
        interaction["nbiType"] = nbi_type
    return payload


def validate_seasonal_payload(payload, nbi_type):
    """The two jq -e assertions from the runbook: interactions must be non-empty
    and every nbiType must be exactly nbi_type."""
    interactions = payload.get("interactions") or []
    if not interactions:
        print(f"  ✗ validation failed: no interactions in the {nbi_type} payload")
        return False
    types = sorted({i.get("nbiType") for i in interactions})
    if types != [nbi_type]:
        print(f"  ✗ validation failed: expected only ['{nbi_type}'], found {types}")
        return False
    print(f"  ✓ validated: {len(interactions)} interactions, all nbiType={nbi_type}")
    return True


def sync_report_type_mapping(base_url, auth_token, pilot_id, dry_run):
    """report_type_nbi_run_mapping has to list the seasonal report types, or
    aggregation never picks the uploaded files up. Missing entries are appended,
    keeping whatever is already configured."""
    print(f"\n[pilot config] checking {REPORT_TYPE_MAPPING_KEY} for pilot {pilot_id}")
    configs = fetch_pilot_configs(base_url, auth_token, pilot_id)
    her_moderation = _config_kvs(configs, HER_MODERATION_CONFIG_TYPE)
    current = (her_moderation.get(REPORT_TYPE_MAPPING_KEY) or "").strip()
    present = [m.strip() for m in current.split(",") if m.strip()]

    missing = [m for m in EXPECTED_REPORT_TYPE_MAPPINGS if m not in present]
    if not missing:
        print(f"  OK ({current})")
        return True

    wanted = ",".join(present + missing)
    print(f"  current : {current or '(unset)'}")
    print(f"  missing : {missing}")
    print(f"  -> setting: {wanted}")
    if dry_run:
        print("  (dry-run, not executed)")
        return True

    url = f"{base_url.rstrip('/')}{PILOT_CONFIGS_POST_PATH.format(pilot_id=pilot_id)}"
    headers = {"Content-Type": "application/json", "Authorization": f"bearer {auth_token}"}
    response = requests.post(
        url,
        headers=headers,
        json={
            "configType": HER_MODERATION_CONFIG_TYPE,
            "configKVs": [
                {
                    "configKey": REPORT_TYPE_MAPPING_KEY,
                    "configVal": wanted,
                    **HER_MODERATION_CONFIG_META[REPORT_TYPE_MAPPING_KEY],
                }
            ],
        },
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code not in (200, 201):
        print(f"  ✗ update failed: HTTP {response.status_code} - {response.text[:300]}")
        return False
    print("  ✓ updated")
    return True


def verify_cluster_api(base_url, auth_token, pilot_id):
    url = f"{base_url.rstrip('/')}/nhs/{pilot_id}/ids"
    headers = {"Authorization": f"Bearer {auth_token}"}
    response = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
    print(f"[verify] GET {url} -> HTTP {response.status_code}")
    print(f"  {response.text[:500]}")
    return response.ok


def verify_user_home(base_url, auth_token, uuid):
    url = f"{base_url.rstrip('/')}/meta/users/{uuid}/homes/1"
    headers = {"Authorization": f"Bearer {auth_token}"}
    response = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
    print(f"[verify] GET {url} -> HTTP {response.status_code}")
    if response.ok:
        try:
            body = response.json()
        except ValueError:
            body = {}
        print(f"  defNhoodId: {body.get('payload', body).get('defNhoodId')}")
    else:
        print(f"  {response.text[:500]}")
    return response.ok


def set_data_point_threshold(base_url, auth_token, pilot_id, value, dry_run):
    url = f"{base_url.rstrip('/')}{PILOT_CONFIGS_POST_PATH.format(pilot_id=pilot_id)}"
    headers = {"Content-Type": "application/json", "Authorization": f"bearer {auth_token}"}
    payload = {
        "configType": NC_CONFIG_TYPE,
        "configKVs": [
            {
                "configKey": DATA_POINT_THRESHOLD_KEY,
                "configVal": str(value),
                "configDocumentation": "doc",
                "configRegex": ".*",
                "configDataType": "TEXT",
            }
        ],
    }
    print(f"\n[config] setting {DATA_POINT_THRESHOLD_KEY}={value} for pilot {pilot_id}")
    if dry_run:
        print("  (dry-run, not executed)")
        return True
    response = requests.post(url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT)
    if response.status_code not in (200, 201):
        print(f"  ✗ update failed: HTTP {response.status_code} - {response.text[:300]}")
        return False
    print("  ✓ updated")
    return True


# --------------------------------------------------------------------------- #
# DB: insert the nbi_asset_data rows that aren't there yet
# --------------------------------------------------------------------------- #


def _free_local_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def ssh_tunnel(ssh_host, ssh_user, ssh_key, remote_host, remote_port, timeout=25):
    """Forward a free local port to remote_host:remote_port over ssh, the same
    way DBeaver's per-connection tunnel does. Yields the local port."""
    local_port = _free_local_port()
    cmd = [
        "ssh",
        "-i",
        str(Path(ssh_key).expanduser()),
        "-N",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-L",
        f"{local_port}:{remote_host}:{remote_port}",
        f"{ssh_user}@{ssh_host}",
    ]
    print(f"  [tunnel] 127.0.0.1:{local_port} -> {remote_host}:{remote_port} "
          f"via {ssh_user}@{ssh_host}")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.time() + timeout
        while True:
            if proc.poll() is not None:
                err = proc.stderr.read().decode(errors="replace").strip()
                raise ValueError(f"ssh tunnel failed: {err[:300]}")
            try:
                with socket.create_connection(("127.0.0.1", local_port), timeout=1):
                    break
            except OSError:
                if time.time() > deadline:
                    raise ValueError(
                        f"ssh tunnel to {ssh_host} did not open within {timeout}s"
                    ) from None
                time.sleep(0.3)
        yield local_port
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


def parse_sql_rows(sql_text):
    """Every INSERT in the rendered SQL as
    (entity_id, asset_id, asset_key, asset_value, asset_value_type, asset_type)."""
    return SQL_VALUES_RE.findall(sql_text)


def sync_nbi_asset_data(config, sql_path, dry_run):
    """Insert the nbi_asset_data rows this pilot is missing.

    (entity_id, asset_id, asset_key) is the table's PRIMARY KEY, so that triple
    decides whether a row already exists. Existing rows are left exactly as they
    are - even when asset_value differs from the template - and only counted.
    """
    if pymysql is None:
        print("\n[db] ✗ pymysql isn't installed - run `uv sync` (or `uv add pymysql`)")
        return False

    missing_keys = db_config_missing(config)
    if missing_keys:
        print(f"\n[db] skipped - the shared config is missing: {', '.join(missing_keys)}")
        print("     Fill those in to have the nbi_asset_data rows inserted automatically.")
        return True

    rows = parse_sql_rows(sql_path.read_text())
    if not rows:
        print(f"\n[db] ✗ no INSERT statements parsed out of {sql_path}")
        return False

    pilot_id = str(config["PILOT_ID"])
    print(f"\n[db] {config['DB_NAME']}.{NBI_ASSET_TABLE} on {config['DB_HOST']}")
    print(f"     {len(rows)} row(s) in the rendered SQL for entity_id={pilot_id}")

    try:
        with ssh_tunnel(
            config["DB_SSH_HOST"],
            config["DB_SSH_USER"],
            config["DB_SSH_KEY"],
            config["DB_HOST"],
            int(config["DB_PORT"]),
        ) as local_port:
            conn = pymysql.connect(
                host="127.0.0.1",
                port=local_port,
                user=config["DB_USER"],
                password=config["DB_PASSWORD"],
                database=config["DB_NAME"],
                connect_timeout=int(DEFAULT_TIMEOUT),
            )
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT asset_id, asset_key, asset_value FROM {NBI_ASSET_TABLE} "
                        "WHERE entity_id = %s",
                        (pilot_id,),
                    )
                    existing = {(r[0], r[1]): r[2] for r in cur.fetchall()}

                to_insert = [r for r in rows if (r[1], r[2]) not in existing]
                # Rows already there whose value differs from the template. Left
                # untouched by design; surfaced so they're not a silent surprise.
                mismatched = [
                    r for r in rows if (r[1], r[2]) in existing and existing[(r[1], r[2])] != r[3]
                ]

                print(f"     already present : {len(rows) - len(to_insert)}")
                print(f"     to insert       : {len(to_insert)}")
                if mismatched:
                    print(f"     value mismatches: {len(mismatched)} (left as-is)")
                    for r in mismatched[:3]:
                        was = existing[(r[1], r[2])]
                        print(f"       {r[1]}.{r[2]}: db={was!r} template={r[3]!r}")
                    if len(mismatched) > 3:
                        print(f"       ... and {len(mismatched) - 3} more")

                if not to_insert:
                    print("     nothing to do ✓")
                    return True

                for r in to_insert[:5]:
                    print(f"       + {r[1]}.{r[2]}")
                if len(to_insert) > 5:
                    print(f"       + ... and {len(to_insert) - 5} more")

                if dry_run:
                    print("     (dry-run, nothing inserted)")
                    return True

                with conn.cursor() as cur:
                    cur.executemany(
                        f"INSERT INTO {NBI_ASSET_TABLE} (entity_id, asset_id, asset_key, "
                        "asset_value, asset_value_type, asset_type) VALUES (%s,%s,%s,%s,%s,%s)",
                        to_insert,
                    )
                conn.commit()
                print(f"     ✓ inserted {len(to_insert)} row(s)")
                return True
            finally:
                conn.close()
    except (ValueError, OSError) as e:
        print(f"[db] ✗ {e}")
        return False
    except pymysql.MySQLError as e:
        print(f"[db] ✗ MySQL error: {e}")
        return False


# --------------------------------------------------------------------------- #
# Interactive prompts
# --------------------------------------------------------------------------- #


def _ask(question, default=None):
    suffix = f" [{default}]" if default is not None else ""
    while True:
        try:
            answer = input(f"{question}{suffix}: ").strip()
        except EOFError:
            raise SystemExit(
                "\nInteractive mode needs a terminal. Run an explicit subcommand instead, "
                "e.g. `setup_her.py setup` or `setup_her.py seasonal --batch <n>`."
            ) from None
        if answer:
            return answer
        if default is not None:
            return default
        print("  (required)")


def ask_choice(question, options):
    """options: [(key, label), ...]. Returns the chosen key; first is default."""
    print(f"\n{question}")
    for i, (_, label) in enumerate(options, 1):
        print(f"  {i}) {label}")
    keys = [k for k, _ in options]
    while True:
        raw = _ask("Choice", "1")
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return keys[int(raw) - 1]
        for key in keys:
            if raw.lower() == key.lower():
                return key
        print(f"  enter 1-{len(options)} (or the name)")


def ask_yes_no(question, default=True):
    while True:
        raw = _ask(f"{question} ({'Y/n' if default else 'y/N'})", "y" if default else "n").lower()
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  enter y or n")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_interactive(config):
    """Ask which HER to set up, then run the same code paths the explicit
    subcommands use."""
    print("=" * 70)
    print("HER Setup")
    print("=" * 70)
    print(f"  Pilot       : {config['PILOT_ID']}")
    print(f"  Environment : {config['ENVIRONMENT']}")
    print(f"  Fuel type   : {config['FUEL_TYPE']}")
    print(f"  UUID        : {config['UUID']}")
    print(f"  (these come from {SHARED_CONFIG_PATH} - edit it to change them)")
    if not ask_yes_no("\nUse this configuration?", True):
        print("Aborted - nothing was changed.")
        return True

    what = ask_choice(
        "Which HER do you want to set up?",
        [
            ("monthly", "Monthly HER  - pilot config, DB SQL, payload -> S3, strings, SHC"),
            ("seasonal", "Seasonal HER - derive + upload the Summer/Winter NBIs"),
            ("both", "Both         - monthly first, then seasonal"),
        ],
    )
    dry_run = ask_yes_no("\nDry run first (build and print, write nothing)?", False)

    ok = True

    if what in ("monthly", "both"):
        print("\n--- Monthly HER options ---")
        setup_args = argparse.Namespace(
            dry_run=dry_run,
            skip_config_sync=not ask_yes_no(
                "  Sync pilot config (her_base_url / her_vendor_bucket / threshold)?", True
            ),
            skip_db=not ask_yes_no("  Insert the nbi_asset_data rows into the DB?", True),
            skip_s3=not ask_yes_no("  Upload the interactions payload to S3?", True),
            skip_strings=not ask_yes_no("  Push the NBI string resources?", True),
            skip_shc=not ask_yes_no("  Run the SHC 2.0 mock pipeline?", True),
        )
        ok = cmd_setup(config, setup_args) and ok

    if what in ("seasonal", "both"):
        print("\n--- Seasonal HER options ---")
        if ask_yes_no(
            "  Derive the batch like monthly does (oldest billing cycle)?", True
        ):
            batch = None
        else:
            batch = _ask("  Target S3 batch")
            if not batch.isdigit():
                print(f"  note: '{batch}' isn't numeric - batches are normally epoch seconds")
        season = ask_choice(
            "  Summer or Winter?",
            [
                ("summer", "Summer  (HER_SEASONAL_SUMMER / nbiType=SummerSeasonal)"),
                ("winter", "Winter  (HER_SEASONAL_WINTER / nbiType=WinterSeasonal)"),
                ("both", "Both    (upload both seasonal files)"),
            ],
        )
        source = ask_choice(
            "  Build the seasonal payload from?",
            [
                ("monthly", "the user's existing monthly NBI in S3 (default)"),
                ("template", "the bundled seasonal templates (no monthly NBI needed)"),
            ],
        )
        seasonal_args = argparse.Namespace(
            dry_run=dry_run,
            batch=batch,
            source_batch=None,
            season=season,
            from_template=(source == "template"),
            force=ask_yes_no("  Overwrite if the object already exists in S3?", False),
            # Seasonal HER can't aggregate without the report-type mapping, so
            # this check always runs.
            skip_config_sync=False,
        )
        ok = cmd_seasonal(config, seasonal_args) and ok

    if dry_run and ok:
        print("\nThat was a dry run - nothing was written. Re-run and answer 'n' to apply.")
    return ok


def cmd_setup(config, args):
    OUTPUT_DIR.mkdir(exist_ok=True)
    pilot_id = str(config["PILOT_ID"])
    uuid = config["UUID"]

    print("=" * 70)
    print("HER Persona Setup")
    print("=" * 70)
    print(f"Pilot        : {pilot_id}")
    print(f"Environment  : {config['ENVIRONMENT']}")
    print(f"Fuel type    : {config['FUEL_TYPE']}")
    print(f"UUID         : {uuid}")
    print("=" * 70)

    ok = True

    # 1. Pilot config: fix her_base_url / her_vendor_bucket if they drifted, and
    # relax neighbourhood_comparison.data_point_threshold so SHC will generate.
    if args.skip_config_sync:
        print("\n[pilot config] skipped (--skip-config-sync)")
    else:
        try:
            ok = sync_her_moderation_config(
                config["BASE_URL"], config["AUTH_TOKEN"], pilot_id, args.dry_run
            ) and ok
        except (requests.exceptions.RequestException, ValueError) as e:
            print(f"  ✗ {e}")
            ok = False
        try:
            ok = set_data_point_threshold(
                config["BASE_URL"],
                config["AUTH_TOKEN"],
                pilot_id,
                config["DATA_POINT_THRESHOLD"],
                args.dry_run,
            ) and ok
        except requests.exceptions.RequestException as e:
            print(f"  ✗ {e}")
            ok = False

    # 2. Render the DB insert statements, then apply the missing ones.
    sql_path = render_sql(pilot_id)
    print(f"\n[sql] rendered {sql_path}")
    if args.skip_db:
        print("\n[db] skipped (--skip-db) - run the SQL file above yourself")
    else:
        ok = sync_nbi_asset_data(config, sql_path, args.dry_run) and ok

    # 3. Render the interactions payload for this user (last completed billing
    # cycle filled in), then upload it under batch=<oldest cycle start>.
    # The billing-cycle read runs even in dry-run: it's read-only, and both the
    # payload contents and the upload path depend on it.
    try:
        cycles = fetch_billing_cycles(
            config["BASE_URL"],
            config["AUTH_TOKEN"],
            uuid,
            config["HOME_ID"],
            config["FUEL_TYPE"],
        )
    except (requests.exceptions.RequestException, ValueError) as e:
        print(f"\n[payload] ✗ billing-cycle lookup failed: {e}")
        print("  skipping the payload render + upload (batch= can't be determined).")
        cycles = None
        ok = False

    if cycles:
        batch = str(oldest_cycle_start(cycles))
        print(f"[batch] oldest billing-cycle start -> batch={batch}")
        payload_path = render_interactions_payload(uuid, cycles, config["FUEL_TYPE"])
        print(f"[payload] rendered {payload_path}")
        if args.skip_s3:
            print("[s3 upload] skipped (--skip-s3)")
        else:
            ok = upload_persona_payload(
                payload_path,
                config["ENVIRONMENT"],
                uuid,
                config["FUEL_TYPE"],
                batch,
                args.dry_run,
            ) and ok

    # 4. Push the NBI string resources (title/shortText/longText) for this pilot.
    if args.skip_strings:
        print("\n[string resources] skipped (--skip-strings)")
    else:
        strings_path = render_strings_script(pilot_id)
        ok = run_strings_script(
            strings_path, config["BASE_URL"], config["AUTH_TOKEN"], args.dry_run
        ) and ok

    # 5. SHC 2.0 mock pipeline - backs the HER report's SHC insights.
    if args.skip_shc:
        print("\n[shc] skipped (--skip-shc)")
    else:
        try:
            ok = run_shc_setup(config, args.dry_run) and ok
        except (requests.exceptions.RequestException, ValueError) as e:
            print(f"  ✗ {e}")
            ok = False

    print("\n" + "=" * 70)
    if ok:
        print("Done.")
        if args.skip_db or db_config_missing(config):
            print("Remember to run the SQL file above against the DB yourself.")
    else:
        print("Completed with errors - see above.")
    print("=" * 70)
    return ok


def cmd_seasonal(config, args):
    """Derive the Summer/Winter seasonal NBI files from the user's existing
    monthly NBI and upload them. Mirrors the ProductQA recovery runbook."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    env = config["ENVIRONMENT"]
    uuid = config["UUID"]
    fuel_type = config["FUEL_TYPE"]
    pilot_id = str(config["PILOT_ID"])
    seasons = list(SEASONS) if args.season == "both" else [args.season]

    print("=" * 70)
    print("HER Seasonal Setup")
    print("=" * 70)
    print(f"Pilot        : {pilot_id}")
    print(f"Environment  : {env}")
    print(f"Fuel type    : {fuel_type}")
    print(f"UUID         : {uuid}")
    print(f"Seasons      : {', '.join(seasons)}")
    print("=" * 70)

    # Billing cycles are needed to derive the batch (same rule as the monthly
    # flow: the oldest cycle start) and to refresh a template's billing values.
    cycles = None
    if args.batch is None or args.from_template:
        try:
            cycles = fetch_billing_cycles(
                config["BASE_URL"], config["AUTH_TOKEN"], uuid, config["HOME_ID"], fuel_type
            )
        except (requests.exceptions.RequestException, ValueError) as e:
            print(f"\n[batch] ✗ billing-cycle lookup failed: {e}")
            return False

    if args.batch is None:
        batch = str(oldest_cycle_start(cycles))
        print(f"\n[batch] oldest billing-cycle start -> batch={batch} (same rule as monthly)")
    else:
        batch = args.batch
        print(f"\n[batch] using the batch you passed: {batch}")

    # 1. Get the source payload: either the local seasonal template, or the
    # monthly NBI already in S3 for this user.
    monthly = None
    if args.from_template:
        missing = [
            str(seasonal_template_path(s))
            for s in seasons
            if not seasonal_template_path(s).exists()
        ]
        if missing:
            print(f"\n[source] ✗ missing template(s): {missing}")
            return False
        # Templates ship with the billing values from whenever they were
        # captured; `cycles` (fetched above) is used to refresh them per user.
        print("\n[source] local seasonal templates in templates/")
    else:
        try:
            source_batch = args.source_batch or latest_monthly_batch(env, uuid, fuel_type)
        except ValueError as e:
            print(f"\n[source] ✗ {e}")
            return False
        if not args.source_batch:
            print(f"\n[source] latest monthly batch for this user: {source_batch}")
        monthly_src = (
            S3_PATH_TEMPLATE.format(
                env=env,
                uuid=uuid,
                delivery_type=MONTHLY_DELIVERY_TYPE,
                fuel_type=fuel_type,
                batch=source_batch,
            )
            + f"{uuid}.json"
        )
        monthly_local = OUTPUT_DIR / "monthly-nbi.json"
        if not download_from_s3(monthly_src, monthly_local):
            return False
        monthly = json.loads(monthly_local.read_text())
        print(f"  {len(monthly.get('interactions') or [])} interactions in the monthly NBI")

    # 2-4. Rewrite nbiType per season, validate, upload.
    ok = True
    for season in seasons:
        delivery_type, nbi_type = SEASONS[season]
        print(f"\n--- {season} ({delivery_type} / nbiType={nbi_type}) ---")
        if args.from_template:
            template = seasonal_template_path(season)
            print(f"  source: {template.name}")
            source = apply_billing_info(json.loads(template.read_text()), cycles, fuel_type)
        else:
            source = monthly
        # Runs for both sources: harmless for a template that already has the
        # right nbiType, and the whole point when deriving from the monthly NBI.
        payload = build_seasonal_payload(source, nbi_type)
        if not validate_seasonal_payload(payload, nbi_type):
            ok = False
            continue

        local = OUTPUT_DIR / f"seasonal-{season}-nbi.json"
        local.write_text(json.dumps(payload, indent=2))
        print(f"  wrote {local}")

        dest = (
            S3_PATH_TEMPLATE.format(
                env=env,
                uuid=uuid,
                delivery_type=delivery_type,
                fuel_type=fuel_type,
                batch=batch,
            )
            + f"{uuid}.json"
        )
        ok = upload_file_to_s3(
            local,
            dest,
            args.dry_run,
            content_type="application/json",
            force=args.force,
        ) and ok

    # 5. The report-type mapping, without which aggregation ignores the uploads.
    if args.skip_config_sync:
        print("\n[pilot config] skipped (--skip-config-sync)")
    else:
        try:
            ok = sync_report_type_mapping(
                config["BASE_URL"], config["AUTH_TOKEN"], pilot_id, args.dry_run
            ) and ok
        except (requests.exceptions.RequestException, ValueError) as e:
            print(f"  ✗ {e}")
            ok = False

    print("\n" + "=" * 70)
    if ok:
        print("Done. Now rerun aggregation and confirm the logs contain:")
        print("  Added [...] EE NBIs to the layout")
        for season in seasons:
            print(f"  report-type={SEASONS[season][0]}")
    else:
        print("Completed with errors - see above.")
    print("=" * 70)
    return ok


def cmd_verify(config, args):
    ok = True
    ok = verify_cluster_api(config["BASE_URL"], config["AUTH_TOKEN"], config["PILOT_ID"]) and ok
    if args.uuid:
        ok = verify_user_home(config["BASE_URL"], config["AUTH_TOKEN"], args.uuid) and ok
    else:
        print("[verify] no --uuid given, skipping the user-home/defNhoodId check")
    return ok


def cmd_set_threshold(config, args):
    return set_data_point_threshold(
        config["BASE_URL"], config["AUTH_TOKEN"], config["PILOT_ID"], args.value, args.dry_run
    )


def main():
    parser = argparse.ArgumentParser(
        description="Set up a HER persona (monthly and/or seasonal). Run with no arguments "
        "for an interactive prompt."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "interactive",
        help="Ask which HER to set up, then run it (the default when no arguments are given).",
    )

    setup_parser = sub.add_parser("setup", help="Run the full monthly HER setup flow.")
    setup_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render/build files and print planned actions without calling S3 or the API.",
    )
    setup_parser.add_argument("--skip-s3", action="store_true", help="Skip the S3 upload step.")
    setup_parser.add_argument(
        "--skip-db",
        action="store_true",
        help="Skip inserting the nbi_asset_data rows (just write the .sql file).",
    )
    setup_parser.add_argument(
        "--skip-strings", action="store_true", help="Skip pushing NBI string resources."
    )
    setup_parser.add_argument(
        "--skip-config-sync",
        action="store_true",
        help="Skip checking/updating the pilot's her_base_url / her_vendor_bucket config.",
    )
    setup_parser.add_argument(
        "--skip-shc",
        action="store_true",
        help="Skip the SHC 2.0 mock pipeline step (user fetch, cluster files, nhood command).",
    )

    seasonal_parser = sub.add_parser(
        "seasonal",
        help="Derive + upload the Summer/Winter seasonal NBI files from the monthly one.",
    )
    seasonal_parser.add_argument(
        "--batch",
        help="Target S3 batch= segment. Default: derived the same way the monthly flow does "
        "it - the oldest billing-cycle start for this user. Pass a value to override, e.g. "
        "when the aggregation request expects a specific batch.",
    )
    seasonal_parser.add_argument(
        "--source-batch",
        help="Monthly NBI batch to derive from (default: the newest one for this user).",
    )
    seasonal_parser.add_argument(
        "--from-template",
        action="store_true",
        help="Build from templates/interactions_<season>_seasonal.json instead of downloading "
        "the monthly NBI from S3 (billing values are still refreshed for this user).",
    )
    seasonal_parser.add_argument(
        "--season",
        choices=[*SEASONS, "both"],
        default="both",
        help="Which seasonal file(s) to produce (default: both).",
    )
    seasonal_parser.add_argument(
        "--force", action="store_true", help="Overwrite the seasonal object if it already exists."
    )
    seasonal_parser.add_argument(
        "--skip-config-sync",
        action="store_true",
        help=f"Skip the {REPORT_TYPE_MAPPING_KEY} check/update.",
    )
    seasonal_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build + validate the files and print planned actions without uploading.",
    )

    verify_parser = sub.add_parser(
        "verify", help="Check the SHC cluster API and (optionally) one user's defNhoodId."
    )
    verify_parser.add_argument("--uuid", help="A pilot user's uuid to check defNhoodId for.")

    threshold_parser = sub.add_parser(
        "set-threshold", help="Set neighbourhood_comparison.data_point_threshold for the pilot."
    )
    threshold_parser.add_argument(
        "value", help=f"New threshold value (use {PROD_DATA_POINT_THRESHOLD} to revert to prod)."
    )
    threshold_parser.add_argument(
        "--dry-run", action="store_true", help="Print the planned update without calling the API."
    )

    # No arguments at all -> ask. Starting straight into setup's own flags (e.g.
    # `--skip-strings`) still implies the non-interactive setup command.
    argv = sys.argv[1:]
    if not argv:
        argv = ["interactive"]
    elif argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv = ["setup"] + argv

    args = parser.parse_args(argv)
    try:
        config = load_config()
    except (ValueError, SharedSetupError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.command == "interactive":
        ok = cmd_interactive(config)
    elif args.command == "setup":
        ok = cmd_setup(config, args)
    elif args.command == "seasonal":
        ok = cmd_seasonal(config, args)
    elif args.command == "verify":
        ok = cmd_verify(config, args)
    elif args.command == "set-threshold":
        ok = cmd_set_threshold(config, args)
    else:
        parser.error(f"unknown command {args.command!r}")

    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
