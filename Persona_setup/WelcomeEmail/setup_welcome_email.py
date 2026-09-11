#!/usr/bin/env python3
"""Trigger the User Welcome email for an existing user.

Reads the shared `Persona_setup/config.json`. Give it the UUID of a user that
already exists on the configured pilot - typically one created by
`cdg_user_setup/create_users.py` - and it makes the welcome email send.

It:
  1. Reads the user record and confirms it is on the configured pilot.
  2. Subscribes the user to USER_WELCOME over email (plain + OPT_OUT).
  3. Resets the USER_WELCOME sent count so the email fires again.
  4. Publishes the notification event and polls until it sends.

No data ingestion: the user's consumption/billing data already exists from the
CDG port, so nothing is uploaded here. Every config write is scoped to the
single user - no pilot-level configuration is touched. Nothing about the pilot
is hardcoded; the notification queue is discovered from the pilot itself.

The token is never printed. AWS credentials are resolved by the AWS CLI.
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
    require_pilot,
    subscribe_email_delivery,
    trigger_notification,
)

SCRIPT_NAME = "WelcomeEmail"
MANAGED_BY = "setup_welcome_email.py"

EVENT_NAME = "USER_WELCOME"
# Informational only - printed so a run says which email to look for.
WELCOME_SUBJECT = "Welcome to your new alerts from Energy Co!"

FUEL_MEASUREMENT_TYPES = {
    "ELECTRIC": MEASUREMENT_TYPE_ELECTRIC,
    "GAS": MEASUREMENT_TYPE_GAS,
}


def build_event(fuel: str) -> NotificationEvent:
    normalized = str(fuel).strip().upper()
    if normalized not in FUEL_MEASUREMENT_TYPES:
        raise SetupError(
            f"FUEL must be one of {sorted(FUEL_MEASUREMENT_TYPES)}, got {fuel!r}"
        )
    return NotificationEvent(
        event_name=EVENT_NAME,
        measurement_type=FUEL_MEASUREMENT_TYPES[normalized],
        subject=WELCOME_SUBJECT,
    )


def run_for_user(
    client: ApiClient,
    pilot: PilotContext,
    config,
    user: dict,
) -> None:
    """Condition one user and trigger the welcome email."""
    user_id = user["UUID"]
    fuel = user.get("FUEL") or config.get("FUEL") or "ELECTRIC"
    event = build_event(fuel)

    label = user.get("label")
    log(f"User {user_id}" + (f" ({label})" if label else ""))
    record = fetch_user(client, user_id)
    require_pilot(record, pilot.pilot_id)
    detail(f"pilot {pilot.pilot_id}, fuel {event.measurement_type}")
    if record.get("email"):
        detail(f"recipient: {record['email']}")

    log(f"Subscribing user to {event.event_name} over {event.delivery_mode}")
    subscribe_email_delivery(client, event, user_id, managed_by=MANAGED_BY)
    detail('delivery_modes = ["Email"] (plain + OPT_OUT)')

    # The welcome email is not derived from an aggregation run, so the rerun is
    # skipped unless RERUN_AGGREGATION is set.
    skip_aggregation = not bool(config.get("RERUN_AGGREGATION", False))
    trigger_notification(
        client, event, pilot, config, user_id, skip_aggregation=skip_aggregation
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Trigger the User Welcome email for the configured user(s)."
    )
    parser.add_argument(
        "--uuid",
        help="Run for this UUID instead of the users in the shared config.",
    )
    args = parser.parse_args()

    try:
        if shutil.which("aws") is None:
            raise SetupError("AWS CLI is required to publish the notification")

        config = load(SCRIPT_NAME)
        users = (
            [{"UUID": args.uuid}] if args.uuid else config.users_for(SCRIPT_NAME)
        )
        if not users:
            raise SetupError(
                f"No users configured for {SCRIPT_NAME}. Add one to USERS in the "
                f"shared config, or pass --uuid."
            )

        client = ApiClient(config.base_url, config.token, config.http_timeout)
        log(f"Reading pilot {config.pilot_id} configuration")
        pilot = PilotContext.load(
            client, config.pilot_id, config.region, config.queue_url
        )
        detail(f"ingestion bucket: {pilot.ingestion_bucket}")

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
        print(
            "The recipient address comes from each user's profile; it is not "
            "part of the published event payload."
        )
        return 0
    except SetupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
