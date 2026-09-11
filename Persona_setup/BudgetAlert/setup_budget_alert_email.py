#!/usr/bin/env python3
"""Trigger the Budget Alert email for existing users.

Reads the shared `Persona_setup/config.json`. Give it the UUID of a user that
already exists on the configured pilot - typically one created by
`cdg_user_setup/create_users.py` - and it sets a budget the user has already
crossed, then makes the alert send.

A budget alert only fires once the user's projected spend crosses a percentage
of their budget, so the budget amount has to be set relative to that
projection. `THRESHOLD_PERCENT` says which crossing to provoke:

  75   - the "reached 75% of your budget" alert
  100  - the "reached 100% of your budget" alert

The budget is computed from the user's own projection
(`budget = projection / (percent / 100)`), so a 75% run sets a budget the
projection is 75% of. Set `BUDGET_AMOUNT` to skip the computation and write an
exact figure instead.

This script never ingests data and never writes pilot-level configuration; the
budget it writes is on the user's own home record. Nothing about the pilot is
hardcoded. The token is never printed.
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
    reset_sent_count,
    subscribe_email_delivery,
    trigger_notification,
)

SCRIPT_NAME = "BudgetAlert"
MANAGED_BY = "setup_budget_alert_email.py"

EVENT_NAME = "BUDGET_ALERT"

# Informational only.
SUBJECT_TAIL = "... You've reached {percent}% of your budget"

# Where the projection is read from. Verified against the pilot: the response
# carries `projectionPrice`, and echoes back `budgetThresholdAmount` - which
# confirms this is the figure the alert compares against.
PROJECTION_PATH = "/2.1/users/{user_id}/homes/{home_ordinal}/billprojections"
PROJECTION_PRICE_KEY = "projectionPrice"

# The user-home meta record is where the budget lives. Confirmed against the
# platform's own contract: the payload carries an absolute currency amount,
# not a percentage.
META_HOME_PATH = "/meta/users/{user_id}/homes/{home_ordinal}"
BUDGET_KEY = "budgetThresholdAmount"
BUDGET_KEY_ELECTRIC = "electricBudgetThresholdAmount"
BUDGET_KEY_GAS = "gasBudgetThresholdAmount"

# The percentages the alert fires on, on the notification status record. A
# working user has e.g. "75.0" here; an unset one reads "null" and stays silent.
THRESHOLDS_CSV_KEY = "budgetThresholdsCSV"

VALID_PERCENTS = (75, 100)

FUEL_MEASUREMENT_TYPES = {
    "ELECTRIC": MEASUREMENT_TYPE_ELECTRIC,
    "GAS": MEASUREMENT_TYPE_GAS,
}


def resolve_percent(value) -> int:
    try:
        percent = int(float(value))
    except (TypeError, ValueError) as exc:
        raise SetupError(f"THRESHOLD_PERCENT must be a number, got {value!r}") from exc
    if percent not in VALID_PERCENTS:
        raise SetupError(
            f"THRESHOLD_PERCENT must be one of {list(VALID_PERCENTS)}, got {percent}"
        )
    return percent


def build_event(fuel: str, percent: int) -> NotificationEvent:
    normalized = str(fuel).strip().upper()
    if normalized not in FUEL_MEASUREMENT_TYPES:
        raise SetupError(
            f"FUEL must be one of {sorted(FUEL_MEASUREMENT_TYPES)}, got {fuel!r}"
        )
    return NotificationEvent(
        event_name=EVENT_NAME,
        measurement_type=FUEL_MEASUREMENT_TYPES[normalized],
        subject=SUBJECT_TAIL.format(percent=percent),
    )


def read_projection(
    client: ApiClient, user_id: str, home_ordinal: int, measurement_type: str
) -> float | None:
    """The projected spend this cycle, which the budget is derived from."""
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
    value = payload.get(PROJECTION_PRICE_KEY)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def write_budget(
    client: ApiClient,
    user_id: str,
    home_ordinal: int,
    amount: float,
    measurement_type: str,
) -> None:
    """Write the budget onto the user's home record.

    Both the generic and the fuel-specific key are written, because which one
    the alert reads depends on how the pilot is set up.
    """
    rounded = round(float(amount), 2)
    body = {BUDGET_KEY: rounded}
    if measurement_type == MEASUREMENT_TYPE_GAS:
        body[BUDGET_KEY_GAS] = rounded
    else:
        body[BUDGET_KEY_ELECTRIC] = rounded
    client.request(
        "POST",
        META_HOME_PATH.format(user_id=user_id, home_ordinal=home_ordinal),
        body=body,
        expected=(200, 201, 204),
    )


def write_alert_thresholds(
    client: ApiClient,
    event: NotificationEvent,
    user_id: str,
    home_ordinal: int,
    percent: int,
) -> None:
    """Set the percentages the alert actually fires on.

    The budget *amount* on the home record is only half of it: the alert
    triggers when projected spend crosses a percentage listed in the
    notification's own `budgetThresholdsCSV`. A user whose CSV is unset has no
    threshold to cross, so no alert is generated no matter what the amount is -
    which is exactly how a working user differs from a silent one.
    """
    client.request(
        "POST",
        event.status_path(user_id, home_ordinal),
        body={THRESHOLDS_CSV_KEY: f"{float(percent):.1f}"},
        expected=(200, 201, 204),
    )


def run_for_user(
    client: ApiClient, pilot: PilotContext, config, user: dict
) -> None:
    user_id = user["UUID"]
    home_ordinal = config.home_ordinal
    fuel = user.get("FUEL") or config.get("FUEL") or "ELECTRIC"
    percent = resolve_percent(
        user.get("THRESHOLD_PERCENT") or config.get("THRESHOLD_PERCENT") or 75
    )
    event = build_event(fuel, percent)

    label = user.get("label")
    log(f"User {user_id}" + (f" ({label})" if label else ""))
    record = fetch_user(client, user_id)
    require_pilot(record, pilot.pilot_id)
    detail(f"target {percent}% of budget, fuel {event.measurement_type}")
    if record.get("email"):
        detail(f"recipient: {record['email']}")

    explicit = user.get("BUDGET_AMOUNT") or config.get("BUDGET_AMOUNT")
    if explicit:
        amount = float(explicit)
        detail(f"using configured BUDGET_AMOUNT {amount}")
    else:
        projection = read_projection(
            client, user_id, home_ordinal, event.measurement_type
        )
        if projection is None or projection <= 0:
            raise SetupError(
                "Could not read a positive bill projection for this user, so the "
                "budget cannot be derived. The user needs usage in the current "
                "billing cycle, or set BUDGET_AMOUNT explicitly."
            )
        # A budget the projection is `percent` of, so the crossing has happened.
        amount = projection / (percent / 100.0)
        detail(f"projection {projection:.2f} -> budget {amount:.2f} ({percent}%)")

    log("Writing the budget onto the user's home record")
    write_budget(client, user_id, home_ordinal, amount, event.measurement_type)
    detail(f"{BUDGET_KEY} = {round(amount, 2)}")

    log(f"Subscribing user to {event.event_name} over {event.delivery_mode}")
    subscribe_email_delivery(client, event, user_id, managed_by=MANAGED_BY)
    detail('delivery_modes = ["Email"] (plain + OPT_OUT)')

    # Reset the sent count first, then set the thresholds. Both live on the same
    # notification status record and the reset clears the CSV, so setting the
    # thresholds earlier would be silently undone.
    log(f"Resetting {event.event_name} sentCount to 0")
    reset_sent_count(client, event, user_id, home_ordinal)

    log(f"Setting the alert threshold to {percent}%")
    write_alert_thresholds(client, event, user_id, home_ordinal, percent)
    detail(f"{THRESHOLDS_CSV_KEY} = {float(percent):.1f}")

    # The alert is evaluated against aggregated data, so rerun by default.
    # `reset=False`: the reset already happened above, and repeating it here
    # would wipe the thresholds just set.
    skip_aggregation = bool(config.get("SKIP_AGGREGATION", False))
    trigger_notification(
        client,
        event,
        pilot,
        config,
        user_id,
        skip_aggregation=skip_aggregation,
        reset=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Trigger the Budget Alert email for the configured user(s)."
    )
    parser.add_argument(
        "--uuid", help="Run for this UUID instead of the users in the shared config."
    )
    parser.add_argument(
        "--percent",
        help=f"Override the threshold ({', '.join(str(p) for p in VALID_PERCENTS)}).",
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
        if args.percent:
            users = [{**u, "THRESHOLD_PERCENT": args.percent} for u in users]
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
