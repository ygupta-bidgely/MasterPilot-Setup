#!/usr/bin/env python3
"""Fill the shared config's USERS list from the pilot's ported users.

The email scripts act on *destination* UUIDs, which only exist once
`cdg_user_setup/create_users.py` has ported a user. This reads the pilot's
ported users from DETO, matches them to the email types they serve by persona
name, and writes the result into `Persona_setup/config.json`.

Nothing is hardcoded to a pilot: the pilot comes from the shared config, and
the DETO connection from `cdg_user_setup/config.json`.

  uv run python sync_users_from_pilot.py            # update config.json
  uv run python sync_users_from_pilot.py --dry-run  # just show what it found
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import SetupError, load  # noqa: E402

PERSONA_DIR = Path(__file__).resolve().parent
CDG_CONFIG = PERSONA_DIR.parent / "cdg_user_setup" / "config.json"

# persona_name on the ported user -> the scripts that user drives, and any
# per-script settings. Persona names come from what create_users.py wrote, so
# they match the sources.csv PERSONA column.
PERSONA_ROLES: dict[str, dict] = {
    "Dual Fuel BB": {"scripts": ["WelcomeEmail", "BillProjection"], "FUEL": "ELECTRIC"},
    "SHC": {"scripts": ["MonthlySummary", "HER"], "VARIANT": "STANDARD"},
    "Monthly Summary - Solar Elements": {
        "scripts": ["MonthlySummary"],
        "VARIANT": "SOLAR",
    },
    "TOU PROMOTION": {"scripts": ["MonthlySummary"], "VARIANT": "TOU_PROMOTION"},
    "Monthly Summary - TOU Coaching": {
        "scripts": ["MonthlySummary"],
        "VARIANT": "TOU_COACHING",
    },
    "TOU Promotion": {"scripts": ["BillProjection"], "VARIANT": "TOU_PROMOTION"},
    "TOU Coaching": {"scripts": ["BillProjection"], "VARIANT": "TOU_COACHING"},
    "Best Rate Plan Alert": {"scripts": ["BestRateEmail"]},
    "TOU Rate Onboarding Email": {"scripts": ["TOUOnboarding"]},
    "Seasonal HER": {"scripts": ["HER"]},
    "GAS Monthly summary": {
        "scripts": ["WelcomeEmail", "MonthlySummary", "BillProjection"],
        "FUEL": "GAS",
    },
    "BB": {"scripts": ["BudgetAlert"], "THRESHOLD_PERCENT": 75},
}


def bearer(token: str) -> str:
    token = str(token).strip()
    if token.lower().startswith("bearer "):
        token = token[len("bearer ") :].strip()
    return f"Bearer {token}"


def fetch_ported(pilot_id) -> list[dict]:
    if not CDG_CONFIG.exists():
        raise SetupError(
            f"{CDG_CONFIG} not found - it holds the DETO connection this needs."
        )
    cdg = json.loads(CDG_CONFIG.read_text())
    for key in ("DETO_BASE_URL", "DETO_ACCESS_TOKEN"):
        if not cdg.get(key):
            raise SetupError(f"{CDG_CONFIG} is missing {key}")
    response = requests.get(
        f"{cdg['DETO_BASE_URL'].rstrip('/')}/v1/user-metadata",
        params={"destination_pilot_id": pilot_id},
        headers={
            "Accept": "application/json",
            "Authorization": bearer(cdg["DETO_ACCESS_TOKEN"]),
        },
        timeout=float(cdg.get("TIMEOUT", 60)),
    )
    response.raise_for_status()
    return response.json().get("data") or []


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fill the shared config's USERS from the pilot's ported users."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what was found, change nothing."
    )
    args = parser.parse_args()

    try:
        config = load()
        pilot_id = config.pilot_id
        ported = fetch_ported(pilot_id)
    except (SetupError, requests.exceptions.RequestException) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    users: list[dict] = []
    unmatched: list[str] = []
    for record in ported:
        persona = (record.get("persona_name") or "").strip()
        role = PERSONA_ROLES.get(persona)
        if not role:
            if persona:
                unmatched.append(persona)
            continue
        # Only a completed port has a usable destination user.
        if str(record.get("status") or "").lower() != "success":
            print(
                f"  skipping {persona}: status={record.get('status')} (port unfinished)"
            )
            continue
        entry = {
            "UUID": record.get("uuid"),
            "label": f"{persona} (pilot {pilot_id})",
            **role,
        }
        users.append(entry)

    print(f"Pilot {pilot_id}: {len(ported)} ported users, {len(users)} mapped to scripts")
    for entry in users:
        print(f"  {entry['label']:<48} {entry['UUID']}  -> {','.join(entry['scripts'])}")
    if unmatched:
        unique = sorted(set(unmatched))
        print(f"\n{len(unique)} persona(s) with no script mapping (ignored):")
        for name in unique[:15]:
            print(f"  {name}")

    if args.dry_run:
        print("\n--dry-run: config.json unchanged")
        return 0

    path = PERSONA_DIR / "config.json"
    raw = json.loads(path.read_text())
    raw["USERS"] = users
    path.write_text(json.dumps(raw, indent=2) + "\n")
    print(f"\nWrote {len(users)} users to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
