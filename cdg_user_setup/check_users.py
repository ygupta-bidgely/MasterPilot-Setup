#!/usr/bin/env python3
"""Report which of the expected personas are missing from a destination pilot.

Read-only. Compares the personas listed in `sources.csv` (or
`sources.csv.example`) against what the DETO `user-metadata` API says is
actually ported into the destination pilot, and prints what is present, what is
missing, and what is ported but unexpected.

Matching is by `source_uuid`, with the persona name/description reported
alongside so a mismatch is obvious. Use this before `create_users.py` to see
what a new pilot still needs, and after it to confirm the port worked.

  uv run python check_users.py                    # pilot from config.json
  uv run python check_users.py --pilot 88002       # override the pilot
  uv run python check_users.py --sources sources.csv.example
  uv run python check_users.py --missing-csv to_create.csv

`--missing-csv` writes the missing rows in `sources.csv` format, so the output
can be fed straight to `create_users.py` to create exactly what is absent.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

USER_METADATA = "/v1/user-metadata"
DEFAULT_TIMEOUT = 60.0

CSV_COLUMNS = (
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


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"{CONFIG_PATH} not found. Copy config.json.example to config.json "
            f"and fill it in."
        )
    with open(CONFIG_PATH) as handle:
        config = json.load(handle)
    for key in ("DETO_BASE_URL", "DETO_ACCESS_TOKEN", "DESTINATION_PILOT_ID"):
        if not config.get(key):
            raise SystemExit(f"Missing required config key {key} in {CONFIG_PATH}")
    config.setdefault("TIMEOUT", DEFAULT_TIMEOUT)
    return config


def bearer(token: str) -> str:
    token = str(token).strip()
    if token.lower().startswith("bearer "):
        token = token[len("bearer ") :].strip()
    return f"Bearer {token}"


def fetch_ported(base_url: str, token: str, pilot_id, timeout: float) -> list[dict]:
    """Every user record on the pilot, via the metadata API (it carries the
    persona name/description, which fetch-user-attribute does not).

    Deliberately unfiltered by status. A part-finished port leaves a record at
    `NotStarted`, and filtering those out reports the user as *missing* when it
    actually exists - which then hides the real problem (an incomplete port)
    behind a wrong one (an absent user).
    """
    response = requests.get(
        f"{base_url.rstrip('/')}{USER_METADATA}",
        params={"destination_pilot_id": pilot_id},
        headers={"Accept": "application/json", "Authorization": bearer(token)},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get("data") or []


def load_expected(path: Path) -> list[dict]:
    """Persona rows from a sources CSV.

    The file may hold more than one table (the example file documents its
    format with a placeholder table first), so every header row is honoured and
    rows with no source UUID are skipped.
    """
    if not path.exists():
        raise SystemExit(f"{path} not found")
    text = path.read_text(encoding="utf-8-sig")
    tables: list[list[dict]] = []
    header: list[str] | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = next(csv.reader(io.StringIO(line)))
        first = fields[0].strip()
        if first == "PERSONA":
            header = [f.strip() for f in fields]
            tables.append([])
            continue
        if first.upper().startswith("TOTAL") or header is None or not tables:
            continue
        row = dict(zip(header, [f.strip() for f in fields], strict=False))
        uuid = (row.get("SOURCE_UUID") or "").strip()
        if not uuid or uuid.startswith("#"):
            continue
        tables[-1].append(row)

    if not tables:
        return []
    # A real sources.csv has one table. The example file documents its format
    # with a made-up table first, then the real personas - so when there is
    # more than one, the last is the one that matters.
    return tables[-1]


def is_placeholder(uuid: str) -> bool:
    """True for a UUID that cannot be a real one (non-hex characters)."""
    stripped = uuid.replace("-", "")
    return len(stripped) != 32 or any(c not in "0123456789abcdefABCDEF" for c in stripped)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report which expected personas are missing from a pilot."
    )
    parser.add_argument("--pilot", help="Destination pilot id (default: from config).")
    parser.add_argument(
        "--sources",
        default=None,
        help="Sources CSV (default: sources.csv, falling back to sources.csv.example).",
    )
    parser.add_argument(
        "--missing-csv",
        help="Write the missing rows to this file in sources.csv format.",
    )
    parser.add_argument(
        "--show-extra",
        action="store_true",
        help="Also list users ported to the pilot that the sources file does not expect.",
    )
    args = parser.parse_args()

    config = load_config()
    pilot_id = args.pilot or config["DESTINATION_PILOT_ID"]

    if args.sources:
        sources_path = Path(args.sources)
        if not sources_path.is_absolute():
            sources_path = SCRIPT_DIR / sources_path
    else:
        sources_path = SCRIPT_DIR / "sources.csv"
        if not sources_path.exists():
            sources_path = SCRIPT_DIR / "sources.csv.example"

    expected = load_expected(sources_path)
    real = [r for r in expected if not is_placeholder(r["SOURCE_UUID"])]
    skipped = len(expected) - len(real)

    print("=" * 78)
    print("CDG user check")
    print("=" * 78)
    print(f"DETO base URL : {config['DETO_BASE_URL']}")
    print(f"Pilot         : {pilot_id}")
    print(f"Sources file  : {sources_path.name}")
    note = f" ({skipped} placeholder rows skipped)" if skipped else ""
    print(f"Expected      : {len(real)} personas{note}")

    try:
        ported = fetch_ported(
            config["DETO_BASE_URL"],
            config["DETO_ACCESS_TOKEN"],
            pilot_id,
            float(config["TIMEOUT"]),
        )
    except requests.exceptions.RequestException as exc:
        print(f"\n✗ DETO request failed: {exc}", file=sys.stderr)
        return 1

    by_source = {
        (u.get("source_uuid") or "").lower(): u for u in ported if u.get("source_uuid")
    }
    print(f"Ported        : {len(ported)} users on this pilot")
    print("=" * 78)

    # A record at any status other than Success means the port started but did
    # not finish - the user needs re-porting, not creating from scratch.
    present, incomplete, missing = [], [], []
    for row in real:
        uuid = row["SOURCE_UUID"].lower()
        found = by_source.get(uuid)
        if not found:
            missing.append((row, None))
        elif str(found.get("status") or "").lower() != "success":
            incomplete.append((row, found))
        else:
            present.append((row, found))

    print(f"\n✓ PRESENT ({len(present)})")
    for row, found in present:
        dest = found.get("uuid", "?")
        ingestion = found.get("ingestion_status") or "?"
        note = "" if ingestion == "SUCCESS" else f"  [ingestion: {ingestion}]"
        rate = found.get("rate_plan")
        want = (row.get("RATE_PLAN") or "").strip()
        if want and str(rate or "").strip() != want:
            note += f"  [rate_plan {rate} != {want} expected]"
        print(f"  {row['PERSONA']:<42} -> {dest}{note}")

    if incomplete:
        print(f"\n! INCOMPLETE ({len(incomplete)}) - port started but did not finish")
        for row, found in incomplete:
            print(
                f"  {row['PERSONA']:<42} status={found.get('status')} "
                f"dest={found.get('uuid')}"
            )

    if missing:
        print(f"\n✗ MISSING ({len(missing)}) - these need creating")
        for row, _ in missing:
            print(f"  {row['PERSONA']:<42} source={row['SOURCE_UUID']}")
    else:
        print("\n✓ Nothing missing - every expected persona is ported.")

    if args.show_extra:
        expected_uuids = {r["SOURCE_UUID"].lower() for r in real}
        extra = [u for s, u in by_source.items() if s not in expected_uuids]
        print(f"\n? PORTED BUT NOT IN THE SOURCES FILE ({len(extra)})")
        for user in extra:
            print(
                f"  {(user.get('persona_name') or '(no persona)'):<42} "
                f"source={user.get('source_uuid')} dest={user.get('uuid')}"
            )

    # Both missing and incomplete users need a create_users.py run, so both go
    # in the output file.
    todo = missing + incomplete
    if args.missing_csv and todo:
        out = Path(args.missing_csv)
        if not out.is_absolute():
            out = SCRIPT_DIR / out
        with open(out, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
            writer.writeheader()
            for row, _ in todo:
                writer.writerow({k: row.get(k, "") for k in CSV_COLUMNS})
        print(f"\nRows still to port written to {out}")
        print(
            f"  Port them with: cp {out.name} sources.csv && "
            f"uv run python create_users.py"
            + (" --delete-existing" if incomplete else "")
        )

    print()
    print("=" * 78)
    print(
        f"SUMMARY  present={len(present)}  incomplete={len(incomplete)}  "
        f"missing={len(missing)}  expected={len(real)}"
    )
    print("=" * 78)
    return 1 if todo else 0


if __name__ == "__main__":
    raise SystemExit(main())
