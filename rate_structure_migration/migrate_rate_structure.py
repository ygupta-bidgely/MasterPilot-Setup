#!/usr/bin/env python3
"""
Fetch rate plan structures from one or more source environments (listed in a
CSV) and push each into a single target environment (configured via JSON).
"""

import csv
import json
import sys
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
SOURCES_CSV_PATH = SCRIPT_DIR / "sources.csv"

TIMEOUT = 30.0


def load_target_config():
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    for key in ("TARGET_BASE_URL", "TARGET_TOKEN", "TARGET_PILOT_ID"):
        if not config.get(key):
            raise ValueError(f"Missing '{key}' in {CONFIG_PATH}")
    return config


def load_sources():
    with open(SOURCES_CSV_PATH, newline="") as f:
        rows = list(csv.DictReader(f))

    required = {"SOURCE_BASE_URL", "ENV_TOKEN", "SOURCE_PILOT_ID", "PLAN_ID"}
    last = {"SOURCE_BASE_URL": None, "ENV_TOKEN": None}
    for i, row in enumerate(rows, start=2):
        missing = required - row.keys()
        if missing:
            raise ValueError(f"{SOURCES_CSV_PATH} row {i} missing columns: {missing}")

        # Allow SOURCE_BASE_URL/ENV_TOKEN to be left blank on rows that share
        # the same source env as the row above them.
        for key in ("SOURCE_BASE_URL", "ENV_TOKEN"):
            if row[key].strip():
                last[key] = row[key].strip()
            elif last[key] is not None:
                row[key] = last[key]
            else:
                raise ValueError(f"{SOURCES_CSV_PATH} row {i}: '{key}' is blank with no prior value to fill down from")
    return rows


def fetch_rate_structure(source_base_url, source_token, source_pilot_id, plan_id):
    url = f"{source_base_url}/v3.0/rates/utilities/{source_pilot_id}/plans/{plan_id}/structure"
    headers = {"Authorization": f"Bearer {source_token}"}

    response = requests.get(url, headers=headers, timeout=TIMEOUT)
    if response.status_code != 200:
        raise Exception(f"Failed to fetch rate structure: HTTP {response.status_code} - {response.text[:200]}")

    body = response.json()

    # The API wraps the payload as {"requestId": ..., "payload": [...]};
    # some responses instead use {"data": [...]}. Unwrap either to match the
    # plain list the configuration endpoint expects.
    if isinstance(body, dict):
        if "payload" in body:
            body = body["payload"]
        elif "data" in body:
            body = body["data"]

    return body


def push_rate_structure(target_base_url, target_token, target_pilot_id, rate_structure):
    url = f"{target_base_url}/v3.0/rates/configuration/utilityId/{target_pilot_id}"
    headers = {
        "Authorization": f"Bearer {target_token}",
        "Content-Type": "application/json"
    }

    return requests.post(url, headers=headers, data=json.dumps(rate_structure), timeout=TIMEOUT)


def main():
    target = load_target_config()
    sources = load_sources()

    if not sources:
        print(f"No rows found in {SOURCES_CSV_PATH}")
        sys.exit(1)

    failures = 0
    for row in sources:
        source_base_url = row["SOURCE_BASE_URL"]
        source_token = row["ENV_TOKEN"]
        source_pilot_id = row["SOURCE_PILOT_ID"]
        plan_id = row["PLAN_ID"]

        print(f"\nFetching rate structure for utility {source_pilot_id}, plan {plan_id} from {source_base_url}...")
        try:
            rate_structure = fetch_rate_structure(source_base_url, source_token, source_pilot_id, plan_id)
        except Exception as e:
            print(f"Failed: {e}")
            failures += 1
            continue
        print("Fetched rate structure successfully.")

        print(f"Pushing rate structure to {target['TARGET_BASE_URL']} for utility {target['TARGET_PILOT_ID']}...")
        response = push_rate_structure(
            target["TARGET_BASE_URL"], target["TARGET_TOKEN"], target["TARGET_PILOT_ID"], rate_structure
        )

        if response.status_code in (200, 201):
            print(f"Success: HTTP {response.status_code}")
            print(response.text)
        else:
            print(f"Failed: HTTP {response.status_code}")
            print(response.text)
            failures += 1

    if failures:
        print(f"\n{failures} of {len(sources)} row(s) failed.")
        sys.exit(1)

    print(f"\nAll {len(sources)} row(s) processed successfully.")


if __name__ == "__main__":
    main()
