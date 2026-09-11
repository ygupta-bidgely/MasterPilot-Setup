#!/usr/bin/env python3
"""Shared notification trigger core for the Persona_setup email scripts.

Every email script does the same four things once the user is configured:
reset the notification's sent count, rerun aggregation, publish the event to
the environment's notification queue, then poll until the notification records
a send. Only the event name, measurement type and delivery mode change, so the
sequence lives here.

This module deliberately does **not** ingest data. The users these scripts run
against already exist and are already ingested (`cdg_user_setup/` creates
them), so the only job is to condition the existing user and make the
notification fire.

Nothing is pilot-specific: the queue is resolved from the pilot's own
configuration via `PilotContext`, so the same code works on a new pilot.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from .api import (
    ApiClient,
    SetupError,
    detail,
    log,
    payload_of,
    run_aws_json,
    write_entity_config,
)
from .pilot_context import PilotContext

DELIVERY_MODE_EMAIL = "Email"
DELIVERY_MODE_PAPER = "Paper"
MEASUREMENT_TYPE_ELECTRIC = "ELECTRIC"
MEASUREMENT_TYPE_GAS = "GAS"


@dataclass(frozen=True)
class NotificationEvent:
    """Identifies one notification for a user.

    `event_name` is the platform's own event enum, which is what gets sent to
    the API. `subject` is informational only - printed so a run says which
    email to look for - and is never sent anywhere.
    """

    event_name: str
    measurement_type: str = MEASUREMENT_TYPE_ELECTRIC
    delivery_mode: str = DELIVERY_MODE_EMAIL
    subject: str | None = None

    def status_path(self, user_id: str, home_ordinal: int) -> str:
        return (
            f"/notification/notificationStatus/{user_id}/{home_ordinal}/"
            f"{self.event_name}/{self.measurement_type}/{self.delivery_mode}"
        )


def subscribe_email_delivery(
    client: ApiClient,
    event: NotificationEvent,
    user_id: str,
    *,
    managed_by: str,
) -> None:
    """Subscribe the user to this notification over its delivery mode.

    Both the plain and `.OPT_OUT` subscription keys are written: QA users are
    commonly provisioned as OPT_OUT, and the pipeline reads the variant that
    matches the user's own notification user type.
    """
    for suffix in ("", ".OPT_OUT"):
        write_entity_config(
            client,
            user_id,
            f"event_subscriptions.{event.event_name}.{event.measurement_type}{suffix}",
            [("delivery_modes", f'["{event.delivery_mode}"]', "TEXT")],
            managed_by=managed_by,
        )


# --------------------------------------------------------------------------- #
# Trigger core: reset -> aggregate -> publish -> poll
# --------------------------------------------------------------------------- #


def sent_count(response: Any) -> int | None:
    try:
        payload = payload_of(response)
    except SetupError:
        return None
    if isinstance(payload, dict) and "sentCount" in payload:
        return int(payload["sentCount"])
    if isinstance(response, dict) and "sentCount" in response:
        return int(response["sentCount"])
    return None


def reset_sent_count(
    client: ApiClient, event: NotificationEvent, user_id: str, home_ordinal: int
) -> None:
    """Zero the sent count so an already-sent notification will fire again."""
    client.request(
        "POST",
        event.status_path(user_id, home_ordinal),
        body={"sentCount": 0},
        expected=(200, 201, 204, 404),
    )


def rerun_aggregation(client: ApiClient, user_id: str, home_ordinal: int) -> None:
    client.request(
        "POST",
        f"/billingdata/users/{user_id}/homes/{home_ordinal}/run/aggregations",
        body={},
        expected=(200, 201, 202, 204),
    )


def send_notification(
    event: NotificationEvent,
    user_id: str,
    home_ordinal: int,
    region: str,
    queue_url: str,
) -> str:
    """Publish the notification event. The recipient address is not part of the
    payload - it comes from the user's own profile."""
    message = {
        "userId": user_id,
        "homeOrdinal": home_ordinal,
        "metaData": {
            "NbiType": event.event_name,
            "deliveryMode": event.delivery_mode,
            "EventName": event.event_name,
        },
    }
    response = run_aws_json(
        [
            "sqs",
            "send-message",
            "--region",
            region,
            "--queue-url",
            queue_url,
            "--message-body",
            json.dumps(message, separators=(",", ":")),
        ]
    )
    message_id = response.get("MessageId")
    if not message_id:
        raise SetupError("SQS did not return a MessageId")
    return str(message_id)


def poll_for_email(
    client: ApiClient,
    event: NotificationEvent,
    user_id: str,
    home_ordinal: int,
    timeout_seconds: int,
    interval_seconds: int,
) -> int:
    """Wait for the notification to record a send."""
    deadline = time.monotonic() + timeout_seconds
    last = 0
    while time.monotonic() < deadline:
        response = client.request(
            "GET", event.status_path(user_id, home_ordinal), expected=(200, 404)
        )
        count = sent_count(response)
        if count is not None:
            last = count
            if count >= 1:
                return count
        time.sleep(interval_seconds)
    raise SetupError(
        f"Notification sentCount did not reach 1 within {timeout_seconds}s "
        f"(last seen {last}). The event was published; the pipeline may still "
        f"be working, or the user may not qualify for {event.event_name}."
    )


def trigger_notification(
    client: ApiClient,
    event: NotificationEvent,
    pilot: PilotContext,
    config: Any,
    user_id: str,
    *,
    skip_aggregation: bool = False,
    reset: bool = True,
) -> int:
    """Run the shared reset -> aggregate -> publish -> poll sequence.

    Returns the observed sentCount, or raises SetupError if the notification
    does not send within the configured timeout.

    Pass `reset=False` when the caller has already reset the sent count and has
    since written other fields to the same status record - the reset clears the
    whole record, so repeating it here would undo them.
    """
    home_ordinal = config.home_ordinal

    if reset:
        log(f"Resetting {event.event_name} {event.delivery_mode} sentCount to 0")
        reset_sent_count(client, event, user_id, home_ordinal)

    if skip_aggregation:
        log("Skipping aggregation rerun")
    else:
        log("Requesting aggregation rerun")
        rerun_aggregation(client, user_id, home_ordinal)
        wait_seconds = int(config.get("AGGREGATION_WAIT_SECONDS"))
        detail(f"waiting {wait_seconds}s before publishing")
        time.sleep(wait_seconds)

    queue_url = pilot.notification_queue_url()
    log("Publishing notification event")
    detail(f"queue: {queue_url.rsplit('/', 1)[-1]}")
    message_id = send_notification(
        event, user_id, home_ordinal, config.region, queue_url
    )
    detail(f"SQS message ID: {message_id}")

    log(f"Waiting for {event.event_name} sentCount >= 1")
    count = poll_for_email(
        client,
        event,
        user_id,
        home_ordinal,
        int(config.get("STATUS_TIMEOUT")),
        int(config.get("STATUS_INTERVAL")),
    )
    detail(f"SUCCESS: sentCount={count}")
    if event.subject:
        detail(f'look for the email with subject: "{event.subject}"')
    return count
