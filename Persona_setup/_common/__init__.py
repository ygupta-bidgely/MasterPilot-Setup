"""Shared code for the Persona_setup scripts.

Nothing here is specific to a pilot or an environment - every pilot-specific
value is discovered at run time from the pilot itself, so the scripts work
against a newly created pilot by changing `PILOT_ID` in the shared
`Persona_setup/config.json`.
"""

from .api import (
    ApiClient,
    SetupError,
    detail,
    fetch_user,
    log,
    payload_of,
    require_pilot,
    require_uuid,
    run_aws_json,
    user_pilot_id,
    write_entity_config,
)
from .config import Config, load
from .notification_trigger import (
    DELIVERY_MODE_EMAIL,
    DELIVERY_MODE_PAPER,
    MEASUREMENT_TYPE_ELECTRIC,
    MEASUREMENT_TYPE_GAS,
    NotificationEvent,
    rerun_aggregation,
    reset_sent_count,
    sent_count,
    subscribe_email_delivery,
    trigger_notification,
)
from .pilot_context import PilotContext, read_config_map

__all__ = [
    "ApiClient",
    "Config",
    "DELIVERY_MODE_EMAIL",
    "DELIVERY_MODE_PAPER",
    "MEASUREMENT_TYPE_ELECTRIC",
    "MEASUREMENT_TYPE_GAS",
    "NotificationEvent",
    "PilotContext",
    "SetupError",
    "detail",
    "fetch_user",
    "load",
    "log",
    "payload_of",
    "read_config_map",
    "require_pilot",
    "require_uuid",
    "rerun_aggregation",
    "reset_sent_count",
    "run_aws_json",
    "sent_count",
    "subscribe_email_delivery",
    "trigger_notification",
    "user_pilot_id",
    "write_entity_config",
]
