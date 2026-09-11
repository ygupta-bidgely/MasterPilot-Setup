#!/usr/bin/env python3
"""Report whether the configured users have a billing cycle yet.

The cycle-derived emails (Monthly Summary, Bill Projection, Budget Alert, ...)
are generated for a *completed* billing cycle. A freshly ported user has none -
`billStartTs` is 0 - and the notification pipeline correctly produces nothing.

This is the read-only check for that: it reports each user's `billStartTs` and
projection so you can tell whether aggregation has done its work before
spending time on the email scripts.

  uv run python check_billing_cycles.py
  uv run python check_billing_cycles.py --event BILL_PROJECTION
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (  # noqa: E402
    ApiClient,
    NotificationEvent,
    SetupError,
    load,
    payload_of,
)

SCRIPT_NAME = "PostCdgAggregation"
PROJECTION_PATH = "/2.1/users/{user_id}/homes/{home_ordinal}/billprojections"


def read_status(
    client: ApiClient, event: NotificationEvent, user_id: str, home_ordinal: int
) -> dict | None:
    response = client.request(
        "GET", event.status_path(user_id, home_ordinal), expected=(200, 404)
    )
    return response if isinstance(response, dict) else None


def read_projection(
    client: ApiClient, user_id: str, home_ordinal: int, measurement_type: str
) -> float | None:
    try:
        payload = payload_of(
            client.request(
                "GET",
                PROJECTION_PATH.format(user_id=user_id, home_ordinal=home_ordinal),
                query={"measurementType": measurement_type},
                expected=(200, 400, 404, 500),
            )
        )
    except SetupError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("projectionPrice")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def fmt_ts(value) -> str:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return "-"
    if seconds <= 0:
        return "none"
    return datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%d")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report each configured user's billing-cycle state."
    )
    parser.add_argument(
        "--event",
        default="MONTHLY_SUMMARY",
        help="Notification whose status to read (default MONTHLY_SUMMARY).",
    )
    args = parser.parse_args()

    try:
        config = load(SCRIPT_NAME)
        users = config.users()
        if not users:
            raise SetupError("No users configured in the shared config.")
        client = ApiClient(config.base_url, config.token, config.http_timeout)
        home_ordinal = config.home_ordinal

        print(f"Pilot {config.pilot_id} - {args.event} readiness")
        print("=" * 96)
        print(f"{'persona':<40}{'billStartTs':<14}{'sentCount':<11}{'projection':<12}ready")
        print("-" * 96)

        ready = 0
        for user in users:
            user_id = user["UUID"]
            fuel = str(user.get("FUEL") or config.get("FUEL") or "ELECTRIC").upper()
            event = NotificationEvent(
                event_name=args.event, measurement_type=fuel
            )
            status = read_status(client, event, user_id, home_ordinal) or {}
            projection = read_projection(client, user_id, home_ordinal, fuel)
            bill_start = status.get("billStartTs")
            sent = status.get("sentCount")
            has_cycle = bool(bill_start) and int(bill_start or 0) > 0
            ok = "YES" if has_cycle else "no"
            ready += 1 if has_cycle else 0
            label = (user.get("label") or user_id)[:38]
            print(
                f"{label:<40}{fmt_ts(bill_start):<14}"
                f"{str(sent if sent is not None else '-'):<11}"
                f"{(f'{projection:.2f}' if projection is not None else '-'):<12}{ok}"
            )

        print("=" * 96)
        print(f"{ready}/{len(users)} user(s) have a billing cycle established")
        if ready < len(users):
            print(
                "\nUsers without a cycle cannot generate cycle-derived emails yet. "
                "Run run_post_cdg_aggregation.py, then re-check - cycles are built "
                "asynchronously."
            )
        return 0
    except SetupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
