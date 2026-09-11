#!/usr/bin/env python3
"""Post-CDG step: mark each user modified, then run aggregations.

Run this after `cdg_user_setup/create_users.py` and before the email scripts.
Freshly ported users have no established billing cycle - `billStartTs` is 0 -
so the cycle-derived notifications (Monthly Summary, Bill Projection, Budget
Alert, ...) have nothing to generate from. This flags each user as modified and
kicks off aggregation, which is what builds those cycles.

Per user, in order:

  1. GET  /meta/users/<uuid>/homes/<home>/modified
  2. wait MODIFIED_WAIT_SECONDS (default 120)
  3. POST /billingdata/users/<uuid>/homes/<home>/run/aggregations
  4. wait AGGREGATION_WAIT_SECONDS (default 120)

The waits are deliberate: each step hands off to an asynchronous pipeline, and
the next step needs the previous one to have landed. With the defaults this is
about four minutes per user, so a full run over the configured users takes a
while - it is meant to be left alone.

Reads the shared `Persona_setup/config.json`. Nothing about the pilot is
hardcoded. The token is never printed.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (  # noqa: E402
    ApiClient,
    SetupError,
    detail,
    fetch_user,
    load,
    log,
    require_pilot,
)

SCRIPT_NAME = "PostCdgAggregation"

MODIFIED_PATH = "/meta/users/{user_id}/homes/{home_ordinal}/modified"
AGGREGATIONS_PATH = "/billingdata/users/{user_id}/homes/{home_ordinal}/run/aggregations"

DEFAULT_MODIFIED_WAIT = 120
DEFAULT_AGGREGATION_WAIT = 120


def mark_modified(client: ApiClient, user_id: str, home_ordinal: int) -> None:
    """Flag the user's home as modified so the pipeline reprocesses it."""
    client.request(
        "GET",
        MODIFIED_PATH.format(user_id=user_id, home_ordinal=home_ordinal),
        expected=(200, 201, 202, 204),
    )


def run_aggregations(client: ApiClient, user_id: str, home_ordinal: int) -> None:
    client.request(
        "POST",
        AGGREGATIONS_PATH.format(user_id=user_id, home_ordinal=home_ordinal),
        body={},
        expected=(200, 201, 202, 204),
    )


def wait(seconds: int, why: str) -> None:
    if seconds <= 0:
        detail(f"not waiting ({why})")
        return
    detail(f"waiting {seconds}s - {why}")
    time.sleep(seconds)


def process_user(
    client: ApiClient,
    config,
    user: dict,
    index: int,
    total: int,
    modified_wait: int,
    aggregation_wait: int,
    last: bool,
) -> None:
    user_id = user["UUID"]
    home_ordinal = config.home_ordinal
    label = user.get("label") or user_id

    log(f"[{index}/{total}] {label}")
    detail(f"uuid {user_id}")

    record = fetch_user(client, user_id)
    require_pilot(record, config.pilot_id)

    detail("GET .../modified")
    mark_modified(client, user_id, home_ordinal)
    wait(modified_wait, "letting the modified flag propagate")

    detail("POST .../run/aggregations")
    run_aggregations(client, user_id, home_ordinal)
    # The trailing wait is per user, so the next user's aggregation does not
    # start while this one is still being processed. Pointless after the last.
    if last:
        detail("aggregation requested (last user, no trailing wait)")
    else:
        wait(aggregation_wait, "letting aggregation complete")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mark users modified and run aggregations, after a CDG port."
    )
    parser.add_argument(
        "--uuid",
        action="append",
        help="Run for this UUID instead of the shared config's users. Repeatable.",
    )
    parser.add_argument(
        "--all-configured",
        action="store_true",
        help="Run for every user in USERS, regardless of which scripts they list "
        "(the default, since this step is not tied to one email type).",
    )
    parser.add_argument(
        "--modified-wait",
        type=int,
        default=None,
        help=f"Seconds to wait after the modified call (default {DEFAULT_MODIFIED_WAIT}).",
    )
    parser.add_argument(
        "--aggregation-wait",
        type=int,
        default=None,
        help=f"Seconds to wait after aggregation (default {DEFAULT_AGGREGATION_WAIT}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the users and timing, then exit without calling anything.",
    )
    args = parser.parse_args()

    try:
        config = load(SCRIPT_NAME)

        if args.uuid:
            users = [{"UUID": u} for u in args.uuid]
        else:
            # This step applies to every configured user, not a per-script subset.
            users = config.users()
        if not users:
            raise SetupError(
                "No users configured. Populate USERS in the shared config "
                "(sync_users_from_pilot.py does this), or pass --uuid."
            )

        modified_wait = (
            args.modified_wait
            if args.modified_wait is not None
            else int(config.get("MODIFIED_WAIT_SECONDS", DEFAULT_MODIFIED_WAIT))
        )
        aggregation_wait = (
            args.aggregation_wait
            if args.aggregation_wait is not None
            else int(config.get("AGGREGATION_WAIT_SECONDS", DEFAULT_AGGREGATION_WAIT))
        )

        total = len(users)
        # Last user skips the trailing wait.
        estimate = total * modified_wait + max(0, total - 1) * aggregation_wait
        print("=" * 70)
        print("Post-CDG aggregation")
        print("=" * 70)
        print(f"Pilot            : {config.pilot_id}")
        print(f"Users            : {total}")
        print(f"Wait after modified   : {modified_wait}s")
        print(f"Wait after aggregation: {aggregation_wait}s")
        print(f"Rough runtime    : ~{estimate // 60} min (waits only)")
        print("=" * 70)

        if args.dry_run:
            for i, user in enumerate(users, start=1):
                print(f"  {i}. {user.get('label') or user['UUID']}  {user['UUID']}")
            print("\n--dry-run: nothing called")
            return 0

        client = ApiClient(config.base_url, config.token, config.http_timeout)

        started = datetime.now()
        failures: list[tuple[str, str]] = []
        for index, user in enumerate(users, start=1):
            try:
                process_user(
                    client,
                    config,
                    user,
                    index,
                    total,
                    modified_wait,
                    aggregation_wait,
                    last=(index == total),
                )
            except SetupError as exc:
                print(f"    FAILED {user['UUID']}: {exc}", file=sys.stderr)
                failures.append((user["UUID"], str(exc)))

        elapsed = (datetime.now() - started).total_seconds()
        print()
        print("=" * 70)
        print("SUMMARY")
        print("=" * 70)
        print(f"Processed : {total - len(failures)}/{total}")
        print(f"Elapsed   : {int(elapsed // 60)}m {int(elapsed % 60)}s")
        if failures:
            print("\nFailed:")
            for user_id, error in failures:
                print(f"  {user_id}: {error}")
        print("=" * 70)
        if failures:
            return 1
        print(
            "\nAggregation has been requested for every user. Billing cycles are "
            "built asynchronously, so give the pipeline time before running the "
            "email scripts - check progress with:\n"
            "  uv run python check_billing_cycles.py"
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
