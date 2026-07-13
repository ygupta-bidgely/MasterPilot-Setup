#!/usr/bin/env python3
"""
Fetch rate plan structures from one or more source environments (listed in a
CSV) and push each into a single target environment (configured via JSON).
"""

import csv
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
SOURCES_CSV_PATH = SCRIPT_DIR / "sources.csv"

TIMEOUT = 30.0
# Number of source rows to migrate concurrently.
MAX_WORKERS = 5


def _emit(log, msg):
    """Append to a per-row log buffer if given, else print immediately."""
    if log is None:
        print(msg)
    else:
        log.append(msg)


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


def _get_json_payload(source_base_url, source_token, path):
    url = f"{source_base_url}{path}"
    headers = {"Authorization": f"Bearer {source_token}"}

    response = requests.get(url, headers=headers, timeout=TIMEOUT)
    if response.status_code != 200:
        raise Exception(f"GET {path} failed: HTTP {response.status_code} - {response.text[:200]}")

    body = response.json()

    # The API wraps the payload as {"requestId": ..., "payload": ...}; some
    # responses instead use {"data": ...}. Unwrap either.
    if isinstance(body, dict):
        if "payload" in body:
            body = body["payload"]
        elif "data" in body:
            body = body["data"]

    return body


def fetch_rate_structure(source_base_url, source_token, source_pilot_id, plan_id):
    return _get_json_payload(
        source_base_url, source_token,
        f"/v3.0/rates/utilities/{source_pilot_id}/plans/{plan_id}/structure"
    )


def fetch_plan_info(source_base_url, source_token, source_pilot_id, plan_id):
    return _get_json_payload(
        source_base_url, source_token,
        f"/v3.0/rates/utilities/{source_pilot_id}/plans/{plan_id}/info"
    )


def _build_rate_dto(row):
    valid_low = row.get("validLow")
    valid_high = row.get("validHigh")
    return {
        "monthLow": row.get("monthLow"),
        "monthHigh": row.get("monthHigh"),
        "dayLow": row.get("dayLow"),
        "dayHigh": row.get("dayHigh"),
        "consumptionLow": row.get("consumptionLow"),
        "consumptionHigh": row.get("consumptionHigh"),
        "timeOfDayLow": row.get("timeOfDayLow"),
        "timeOfDayHigh": row.get("timeOfDayHigh"),
        "weekLow": row.get("weekLow"),
        "weekHigh": row.get("weekHigh"),
        # Source gives epoch seconds; the configuration endpoint expects epoch milliseconds.
        "validLow": valid_low * 1000 if valid_low is not None else None,
        "validHigh": valid_high * 1000 if valid_high is not None else None,
        "rate": row.get("rate"),
        "touName": row.get("touName"),
        "tierName": row.get("tierName"),
        "isHoliday": row.get("isHoliday"),
        "groupName": row.get("groupName"),
        "chargeType": row.get("chargeType"),
    }


def build_rate_plan_configuration(rate_structure, plan_info, target_pilot_id, target_plan_id, log=None):
    """Convert the flat GET /structure response (RateInfo rows) into the nested
    RatePlanConfigurationDTO shape the POST /configuration endpoint expects,
    mirroring pingpong's RatesConfigurationService.getComponents grouping.

    When several source rows cover the same window (same component + identical
    interval, differing only in rate and/or erateId) we keep the row with the
    highest erateId - the newest insert. This matches how pingpong's calc engine
    de-facto resolves such collisions (last write wins, which in practice is the
    latest-inserted row) and avoids the target's overlap validation. erateId is
    used only for this tie-break; it is not sent (the target DB regenerates it)."""
    components_by_key = {}
    # (component key, window fingerprint) -> {erateId, rate_dto, comp_key}
    winner_by_window = {}
    for row in rate_structure:
        comp_key = (int(row["rateBandId"]), bool(row["isHoliday"]))
        if comp_key not in components_by_key:
            components_by_key[comp_key] = {
                "rateBandId": comp_key[0],
                "rateBandName": row.get("rateBandName"),
                "holidaysEnabled": comp_key[1],
                "rates": [],
            }

        rate_dto = _build_rate_dto(row)
        # Window = the rate line without its price; two rows sharing this but
        # with different rates are a same-interval conflict.
        window = json.dumps({k: v for k, v in rate_dto.items() if k != "rate"}, sort_keys=True)
        window_key = (comp_key, window)
        erate_id = row.get("erateId") if row.get("erateId") is not None else -1

        winner = winner_by_window.get(window_key)
        if winner is None or erate_id > winner["erateId"]:
            winner_by_window[window_key] = {"erateId": erate_id, "rate_dto": rate_dto, "comp_key": comp_key}

    kept = len(winner_by_window)
    dropped = len(rate_structure) - kept
    for winner in winner_by_window.values():
        components_by_key[winner["comp_key"]]["rates"].append(winner["rate_dto"])

    if dropped:
        _emit(log, f"Collapsed {len(rate_structure)} row(s) -> {kept} "
                   f"(dropped {dropped} same-window row(s), kept highest erateId per window).")

    return {
        "utilityId": int(target_pilot_id),
        "mappedUtility": plan_info.get("mappedUtility"),
        "planNumber": int(target_plan_id),
        "planName": plan_info.get("planName"),
        "sourceFile": plan_info.get("sourceFile"),
        "planDescription": plan_info.get("description"),
        "residential": plan_info.get("planType") == "RESIDENTIAL",
        "components": list(components_by_key.values()),
    }


def push_rate_structure(target_base_url, target_token, target_pilot_id, rate_plan_configuration):
    url = f"{target_base_url}/v3.0/rates/configuration/utilityId/{target_pilot_id}"
    headers = {
        "Authorization": f"Bearer {target_token}",
        "Content-Type": "application/json"
    }

    return requests.post(url, headers=headers, data=json.dumps([rate_plan_configuration]), timeout=TIMEOUT)


def process_source(row, target):
    """Migrate one CSV row. Returns (ok, log_text). All progress is collected
    into a per-row buffer so concurrent runs don't interleave output."""
    source_base_url = row["SOURCE_BASE_URL"]
    source_token = row["ENV_TOKEN"]
    source_pilot_id = row["SOURCE_PILOT_ID"]
    plan_id = row["PLAN_ID"]
    # Optional: push under a different plan number on the target. Blank/missing
    # keeps the source plan number.
    target_plan_id = row.get("TARGET_PLAN_ID", "").strip() or plan_id

    tag = f"[plan {plan_id}" + (f"->{target_plan_id}] " if target_plan_id != plan_id else "] ")
    log = [f"{tag}Fetching rate structure for utility {source_pilot_id} from {source_base_url}..."]

    try:
        rate_structure = fetch_rate_structure(source_base_url, source_token, source_pilot_id, plan_id)
        plan_info = fetch_plan_info(source_base_url, source_token, source_pilot_id, plan_id)
    except Exception as e:
        log.append(f"{tag}Failed: {e}")
        return False, "\n".join(log)
    log.append(f"{tag}Fetched successfully ({len(rate_structure)} row(s)).")

    rate_plan_configuration = build_rate_plan_configuration(
        rate_structure, plan_info, target["TARGET_PILOT_ID"], target_plan_id, log=log
    )

    log.append(f"{tag}Pushing to {target['TARGET_BASE_URL']} for utility {target['TARGET_PILOT_ID']}, "
               f"plan {target_plan_id}...")
    try:
        response = push_rate_structure(
            target["TARGET_BASE_URL"], target["TARGET_TOKEN"], target["TARGET_PILOT_ID"], rate_plan_configuration
        )
    except Exception as e:
        log.append(f"{tag}Failed: {e}")
        return False, "\n".join(log)

    if response.status_code in (200, 201):
        log.append(f"{tag}Success: HTTP {response.status_code} {response.text}")
        return True, "\n".join(log)

    log.append(f"{tag}Failed: HTTP {response.status_code} {response.text}")
    return False, "\n".join(log)


def main():
    target = load_target_config()
    sources = load_sources()

    if not sources:
        print(f"No rows found in {SOURCES_CSV_PATH}")
        sys.exit(1)

    workers = min(MAX_WORKERS, len(sources))
    print(f"Migrating {len(sources)} plan(s) with {workers} worker(s)...")

    # Preserve CSV order in the output even though rows finish out of order.
    results = [None] * len(sources)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process_source, row, target): i for i, row in enumerate(sources)}
        for future in as_completed(futures):
            results[futures[future]] = future.result()

    failures = 0
    for ok, text in results:
        print(f"\n{text}")
        if not ok:
            failures += 1

    if failures:
        print(f"\n{failures} of {len(sources)} row(s) failed.")
        sys.exit(1)

    print(f"\nAll {len(sources)} row(s) processed successfully.")


if __name__ == "__main__":
    main()
