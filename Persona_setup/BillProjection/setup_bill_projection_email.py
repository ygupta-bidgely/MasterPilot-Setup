#!/usr/bin/env python3
"""Trigger the Bill Projection email for existing users.

Reads the shared `Persona_setup/config.json`. Give it the UUID of a user that
already exists on the configured pilot - typically one created by
`cdg_user_setup/create_users.py` - and it makes the bill projection send.

`VARIANT` selects which flavour of the email is set up:

  STANDARD      - plain bill projection
  TOU_PROMOTION - TOU rate promotion elements
  TOU_COACHING  - TOU coaching elements

The projection is computed from the user's mid-cycle usage, so the user needs
consumption in the *current* billing cycle for the email to have anything to
project. That data comes from the CDG port; this script does not ingest any.

This script only conditions the email and fires it. It does not set the user's
rate plan - that is the persona scripts' job (`Standard/`, `TOU/`), so run
those first if the user is not already on the right plan. It never writes
pilot-level configuration.

Nothing about the pilot is hardcoded; the notification queue is discovered from
the pilot itself. The token is never printed.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (  # noqa: E402
    MEASUREMENT_TYPE_ELECTRIC,
    MEASUREMENT_TYPE_GAS,
    ApiClient,
    NotificationEvent,
    PilotContext,
    SetupError,
    detail,
    fetch_user,
    load,
    log,
    payload_of,
    require_pilot,
    subscribe_email_delivery,
    trigger_notification,
)

SCRIPT_NAME = "BillProjection"
MANAGED_BY = "setup_bill_projection_email.py"

EVENT_NAME = "BILL_PROJECTION"

# Where the projection is read from. Verified against the pilot: the response
# carries `projectionPrice` (the projected spend) alongside `currentPrice`.
PROJECTION_PATH = "/2.1/users/{user_id}/homes/{home_ordinal}/billprojections"
PROJECTION_PRICE_KEY = "projectionPrice"

# Informational only - the subject interpolates the spend so far, so only the
# stable tail is quoted.
SUBJECT_TAIL = "... so far. See how much you're expected to pay!"

# VARIANT -> a note about what the user must already be, since this script does
# not change rate plans.
VARIANTS: dict[str, str | None] = {
    "STANDARD": None,
    "TOU_PROMOTION": "expects a TOU-promotion user (run TOU/ first)",
    "TOU_COACHING": "expects a TOU-coaching user (run TOU/ first)",
}

FUEL_MEASUREMENT_TYPES = {
    "ELECTRIC": MEASUREMENT_TYPE_ELECTRIC,
    "GAS": MEASUREMENT_TYPE_GAS,
}


def resolve_variant(name: str) -> tuple[str, str | None]:
    key = str(name).strip().upper()
    if key not in VARIANTS:
        raise SetupError(
            f"Unknown VARIANT {name!r}. Valid values: {', '.join(sorted(VARIANTS))}"
        )
    return key, VARIANTS[key]


def build_event(fuel: str) -> NotificationEvent:
    normalized = str(fuel).strip().upper()
    if normalized not in FUEL_MEASUREMENT_TYPES:
        raise SetupError(
            f"FUEL must be one of {sorted(FUEL_MEASUREMENT_TYPES)}, got {fuel!r}"
        )
    return NotificationEvent(
        event_name=EVENT_NAME,
        measurement_type=FUEL_MEASUREMENT_TYPES[normalized],
        subject=SUBJECT_TAIL,
    )


def read_projection(
    client: ApiClient, user_id: str, home_ordinal: int, measurement_type: str
) -> dict | None:
    """The user's current bill projection, or None if the pilot has none."""
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
    return payload if isinstance(payload, dict) else None


def report_projection(
    client: ApiClient, user_id: str, home_ordinal: int, measurement_type: str
) -> None:
    """Print the user's current projection.

    Purely informational, but worth surfacing before the event is published: a
    missing or zero projection is the usual reason a bill-projection email does
    not render, and seeing it here beats waiting for the poll to time out.
    """
    projection = read_projection(client, user_id, home_ordinal, measurement_type)
    if not projection:
        detail("projection: not available from this pilot (continuing)")
        return
    price = projection.get(PROJECTION_PRICE_KEY)
    current = projection.get("currentPrice")
    if price is None:
        detail("projection: none reported yet (the email may not render)")
        return
    detail(f"projection: {float(price):.2f} so far {float(current or 0):.2f}")
    days_left = projection.get("daysLeft")
    if days_left is not None:
        detail(f"  days left in cycle: {days_left}")


def run_for_user(
    client: ApiClient, pilot: PilotContext, config, user: dict
) -> None:
    user_id = user["UUID"]
    fuel = user.get("FUEL") or config.get("FUEL") or "ELECTRIC"
    variant_name = user.get("VARIANT") or config.get("VARIANT") or "STANDARD"
    variant_key, note = resolve_variant(variant_name)
    event = build_event(fuel)

    label = user.get("label")
    log(f"User {user_id}" + (f" ({label})" if label else ""))
    record = fetch_user(client, user_id)
    require_pilot(record, pilot.pilot_id)
    detail(f"variant {variant_key}, fuel {event.measurement_type}")
    if note:
        detail(f"note: {note}")
    if record.get("email"):
        detail(f"recipient: {record['email']}")
    report_projection(client, user_id, config.home_ordinal, event.measurement_type)

    log(f"Subscribing user to {event.event_name} over {event.delivery_mode}")
    subscribe_email_delivery(client, event, user_id, managed_by=MANAGED_BY)
    detail('delivery_modes = ["Email"] (plain + OPT_OUT)')

    # The projection is derived from aggregated mid-cycle data, so the
    # aggregation rerun is wanted here by default.
    skip_aggregation = bool(config.get("SKIP_AGGREGATION", False))
    trigger_notification(
        client, event, pilot, config, user_id, skip_aggregation=skip_aggregation
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Trigger the Bill Projection email for the configured user(s)."
    )
    parser.add_argument(
        "--uuid", help="Run for this UUID instead of the users in the shared config."
    )
    parser.add_argument(
        "--variant", help=f"Override the variant ({', '.join(sorted(VARIANTS))})."
    )
    args = parser.parse_args()

    try:
        if shutil.which("aws") is None:
            raise SetupError("AWS CLI is required to publish the notification")

        config = load(SCRIPT_NAME)
        if args.uuid:
            users = [{"UUID": args.uuid}]
        else:
            users = config.users_for(SCRIPT_NAME)
        if args.variant:
            users = [{**u, "VARIANT": args.variant} for u in users]
        if not users:
            raise SetupError(
                f"No users configured for {SCRIPT_NAME}. Add one to USERS in the "
                f"shared config with \"scripts\": [\"{SCRIPT_NAME}\"], or pass --uuid."
            )

        client = ApiClient(config.base_url, config.token, config.http_timeout)
        log(f"Reading pilot {config.pilot_id} configuration")
        pilot = PilotContext.load(
            client, config.pilot_id, config.region, config.queue_url
        )

        failures = []
        for user in users:
            try:
                run_for_user(client, pilot, config, user)
            except SetupError as exc:
                print(f"  FAILED {user['UUID']}: {exc}", file=sys.stderr)
                failures.append(user["UUID"])

        print()
        print(f"{len(users) - len(failures)}/{len(users)} user(s) succeeded")
        if failures:
            print(f"failed: {', '.join(failures)}", file=sys.stderr)
            return 1
        return 0
    except SetupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
