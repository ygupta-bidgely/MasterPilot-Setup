#!/usr/bin/env python3
"""
Fetch the list of ported destination UUIDs for a pilot.

Calls the DETO `fetch-user-attribute` API for the destination pilot in
config.json and writes the ported users — destination UUID, source UUID, status
and attributes — to `ported_uuids.csv`, plus a plain `ported_uuids.txt` of just
the destination UUIDs (one per line, handy for piping into other tools).

Reuses the same config.json as create_users.py (DETO base URL, token, and
destination pilot); no extra configuration needed. Self-contained: only needs
the `requests` library.
"""

import argparse
import csv
import json
import sys
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

FETCH_USER_ATTRIBUTE = "/v1/fetch-user-attribute"
DEFAULT_TIMEOUT = 60.0

REQUIRED_CONFIG_KEYS = ("DETO_BASE_URL", "DETO_ACCESS_TOKEN", "DESTINATION_PILOT_ID")


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

    config.setdefault("TIMEOUT", DEFAULT_TIMEOUT)
    return config


def _bearer(access_token):
    """Accept the token with or without a leading "Bearer "."""
    token = access_token.strip()
    if token.lower().startswith("bearer "):
        token = token[len("bearer "):].strip()
    return f"Bearer {token}"


def fetch_user_attributes(base_url, access_token, pilot_id, timeout):
    """GET /v1/fetch-user-attribute — returns all ported users for the pilot in
    a single call."""
    url = f"{base_url.rstrip('/')}{FETCH_USER_ATTRIBUTE}"
    headers = {"Accept": "application/json", "Authorization": _bearer(access_token)}
    response = requests.get(
        url, params={"destination_pilot_id": pilot_id}, headers=headers, timeout=timeout
    )
    response.raise_for_status()
    return response.json()


def extract_rows(data, include_failed):
    """Pull the ported users out of the API response. Each result carries a
    destination `uuid`, its `source_uuid`, a `status`, and `attributes`."""
    rows = []
    for result in data.get("results", []) or []:
        status = result.get("status")
        if not include_failed and status != "success":
            continue
        rows.append({
            "destination_uuid": result.get("uuid", ""),
            "source_uuid": result.get("source_uuid", ""),
            "status": status or "",
            "attributes": "|".join(result.get("attributes", []) or []),
        })
    return rows


def main():
    parser = argparse.ArgumentParser(
        description="Fetch ported destination UUIDs for a pilot via the DETO "
        "fetch-user-attribute API."
    )
    parser.add_argument(
        "--pilot",
        help="Destination pilot id (default: DESTINATION_PILOT_ID from config.json).",
    )
    parser.add_argument(
        "--include-failed",
        action="store_true",
        help="Also include users whose port did not succeed (default: ported/success only).",
    )
    parser.add_argument(
        "--suffix",
        help="Label for the output filenames (default: DESTINATION_ENVIRONMENT from config, "
        "e.g. dev/uat). Files are ported_uuids_<suffix>.csv / .txt, so different "
        "environments don't overwrite each other.",
    )
    args = parser.parse_args()

    config = load_config()
    pilot_id = args.pilot or config["DESTINATION_PILOT_ID"]

    # Name outputs per environment so dev/uat/productqa runs sit side by side.
    label = (args.suffix or config.get("DESTINATION_ENVIRONMENT") or str(pilot_id)).strip().lower()
    label = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in label) or "out"
    csv_out = SCRIPT_DIR / f"ported_uuids_{label}.csv"
    txt_out = SCRIPT_DIR / f"ported_uuids_{label}.txt"

    print(f"Fetching ported users for pilot {pilot_id} from {config['DETO_BASE_URL']} ...")
    try:
        data = fetch_user_attributes(
            config["DETO_BASE_URL"], config["DETO_ACCESS_TOKEN"], pilot_id, float(config["TIMEOUT"])
        )
    except requests.exceptions.RequestException as e:
        print(f"✗ API request failed: {e}")
        sys.exit(1)

    rows = extract_rows(data, args.include_failed)

    with open(csv_out, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["destination_uuid", "source_uuid", "status", "attributes"]
        )
        writer.writeheader()
        writer.writerows(rows)

    with open(txt_out, "w") as f:
        for row in rows:
            if row["destination_uuid"]:
                f.write(row["destination_uuid"] + "\n")

    print("=" * 70)
    print(f"Pilot            : {pilot_id}")
    print(f"Environment      : {label}")
    # These top-level counts are reported by the API when present.
    if "total_users" in data:
        print(f"Total users      : {data.get('total_users')}")
        print(f"Successful       : {data.get('successful_users')}")
        print(f"Failed           : {data.get('failed_users')}")
    scope = "all" if args.include_failed else "ported/success only"
    print(f"Rows written     : {len(rows)} ({scope})")
    print(f"CSV              : {csv_out}")
    print(f"Destination UUIDs: {txt_out}")
    print("=" * 70)


if __name__ == "__main__":
    main()
