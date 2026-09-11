#!/usr/bin/env python3
"""Trigger the Monthly Summary email for existing users.

Reads the shared `Persona_setup/config.json`. Give it the UUID of a user that
already exists on the configured pilot - typically one created by
`cdg_user_setup/create_users.py` - and it makes the monthly summary send.

`VARIANT` selects which flavour of the email is set up:

  STANDARD        - plain monthly summary            (MS_STANDARD)
  SOLAR           - solar elements                   (MS_SOLAR)
  TOU_PROMOTION   - TOU rate promotion elements      (MS_TOU_PROMOTION)
  TOU_COACHING    - TOU coaching elements            (MS_TOU_COACHING)
  BUDGET_BILLING  - budget billing elements          (MS_BB)
  DEMAND_COACHING - demand coaching elements         (MS_STANDARD + peak demand)

Demand Coaching is not a separate notification: it is MONTHLY_SUMMARY with the
pilot's demand-charge display turned on for the user, which is why it lives
here as a variant rather than in its own folder.

This script only conditions the email and fires it. It does not set the user's
rate plan - that is the persona scripts' job (`Standard/`, `TOU/`, `Solar/`,
`BudgetBilling/`), so run those first if the user is not already on the right
plan. It never ingests data and never writes pilot-level configuration.

Nothing about the pilot is hardcoded; the notification queue is discovered from
the pilot itself. The token is never printed.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
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
    write_entity_config,
)

SCRIPT_NAME = "MonthlySummary"
MANAGED_BY = "setup_monthly_summary_email.py"

EVENT_NAME = "MONTHLY_SUMMARY"

# Informational only - printed so a run says which email to look for. The
# electric subject interpolates the previous bill cost, so only the stable tail
# is quoted here.
SUBJECT_ELECTRIC = "... on electricity in the last billing cycle"
SUBJECT_GAS = "... on gas in the last billing cycle"

# The config key that selects the email's variant. Unverified key name, same
# caveat as the other persona scripts in this repo (see persona_config_matrix.md).
VARIANT_CONFIG_TYPE = "email_moderation"
VARIANT_CONFIG_KEY = "monthly_summary_email_variant"

# Demand coaching additionally needs the demand-charge label shown; the value
# is the display label the email renders.
DEMAND_CONFIG_TYPE = "demand_charges_config"
DEMAND_CONFIG_KEY = "demand charge"
DEMAND_CONFIG_VALUE = "Demand Charges"


@dataclass(frozen=True)
class Variant:
    """One monthly-summary flavour."""

    ms_variant: str
    show_demand: bool = False
    note: str | None = None


VARIANTS: dict[str, Variant] = {
    "STANDARD": Variant("MS_STANDARD"),
    "SOLAR": Variant("MS_SOLAR", note="expects a solar user (run Solar/ first)"),
    "TOU_PROMOTION": Variant(
        "MS_TOU_PROMOTION", note="expects a TOU-promotion user (run TOU/ first)"
    ),
    "TOU_COACHING": Variant(
        "MS_TOU_COACHING", note="expects a TOU-coaching user (run TOU/ first)"
    ),
    "BUDGET_BILLING": Variant(
        "MS_BB", note="expects a budget-billing user (run BudgetBilling/ first)"
    ),
    "DEMAND_COACHING": Variant(
        "MS_STANDARD",
        show_demand=True,
        note="also enables the user's demand-charge display",
    ),
}

FUEL_MEASUREMENT_TYPES = {
    "ELECTRIC": MEASUREMENT_TYPE_ELECTRIC,
    "GAS": MEASUREMENT_TYPE_GAS,
}


def resolve_variant(name: str) -> tuple[str, Variant]:
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
        subject=SUBJECT_GAS if normalized == "GAS" else SUBJECT_ELECTRIC,
    )


def write_variant_config(
    client: ApiClient, user_id: str, variant_key: str, variant: Variant
) -> None:
    """Set the email variant, and the demand-charge display when needed."""
    write_entity_config(
        client,
        user_id,
        VARIANT_CONFIG_TYPE,
        [(VARIANT_CONFIG_KEY, variant.ms_variant, "TEXT")],
        managed_by=MANAGED_BY,
    )
    detail(f"{VARIANT_CONFIG_TYPE}/{VARIANT_CONFIG_KEY} = {variant.ms_variant!r}")
    detail("  (unverified key name - see persona_config_matrix.md)")

    if variant.show_demand:
        write_entity_config(
            client,
            user_id,
            DEMAND_CONFIG_TYPE,
            [(DEMAND_CONFIG_KEY, DEMAND_CONFIG_VALUE, "TEXT")],
            managed_by=MANAGED_BY,
        )
        detail(f"{DEMAND_CONFIG_TYPE}/{DEMAND_CONFIG_KEY} = {DEMAND_CONFIG_VALUE!r}")


def run_for_user(
    client: ApiClient, pilot: PilotContext, config, user: dict
) -> None:
    user_id = user["UUID"]
    fuel = user.get("FUEL") or config.get("FUEL") or "ELECTRIC"
    variant_name = user.get("VARIANT") or config.get("VARIANT") or "STANDARD"
    variant_key, variant = resolve_variant(variant_name)
    event = build_event(fuel)

    label = user.get("label")
    log(f"User {user_id}" + (f" ({label})" if label else ""))
    record = fetch_user(client, user_id)
    require_pilot(record, pilot.pilot_id)
    detail(f"variant {variant_key}, fuel {event.measurement_type}")
    if variant.note:
        detail(f"note: {variant.note}")
    if record.get("email"):
        detail(f"recipient: {record['email']}")

    log("Writing the monthly-summary variant configuration")
    write_variant_config(client, user_id, variant_key, variant)

    log(f"Subscribing user to {event.event_name} over {event.delivery_mode}")
    subscribe_email_delivery(client, event, user_id, managed_by=MANAGED_BY)
    detail('delivery_modes = ["Email"] (plain + OPT_OUT)')

    # The monthly summary is built from aggregated billing data, so the
    # aggregation rerun is wanted here by default.
    skip_aggregation = bool(config.get("SKIP_AGGREGATION", False))
    trigger_notification(
        client, event, pilot, config, user_id, skip_aggregation=skip_aggregation
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Trigger the Monthly Summary email for the configured user(s)."
    )
    parser.add_argument(
        "--uuid", help="Run for this UUID instead of the users in the shared config."
    )
    parser.add_argument(
        "--variant",
        help=f"Override the variant ({', '.join(sorted(VARIANTS))}).",
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
        detail(f"consumption unit: {pilot.consumption_unit}")

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
