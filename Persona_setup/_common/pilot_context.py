#!/usr/bin/env python3
"""Pilot discovery for the Persona_setup scripts.

Nothing about a pilot is hardcoded here. Given a `BASE_URL` and a `PILOT_ID`,
this module reads the pilot's own configuration to work out the values the
setup scripts need - ingestion bucket, file prefixes, delimiters, date formats,
rate plans, and the environment's notification queue - so the same scripts work
against a newly created pilot without editing any code.

Everything is read through `/v2.0/configs/{configType}/pilot/{pilotId}`, which
is the same mechanism the platform's own console uses, and the SQS environment
is identified by probing for the pilot's real enrolment queue rather than
assuming a name.
"""

from __future__ import annotations

import json
import subprocess
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from .api import ApiClient, SetupError, payload_of, run_aws_json

# Config types read from the pilot. Named here rather than inline so a new
# pilot's missing config surfaces as one clear error.
CONFIG_S3_PULL = "s3_pull"
CONFIG_DATA_INGESTION = "data_ingestion"
CONFIG_LAUNCHPAD_INGESTION = "launchpad_ingestion_configs"
CONFIG_USER_CREATION_LAUNCHPAD = "user_creation_launchpad"
CONFIG_EMAIL_MODERATION = "email_moderation"

# SQSFactory names the notification queue NotificationsProcessorEventPriority-<env>.
# The older per-pilot form is tried too, since some environments still use it.
NOTIFICATION_QUEUE_PATTERNS = (
    "NotificationsProcessorEventPriority-{env}",
    "NotificationsProcessorEvent-{env}",
)
DEFAULT_ENROLL_QUEUE_BASE = "resi-fl-S3-UserEnrollment"


def read_config_map(
    client: ApiClient, config_type: str, entity: str, entity_id: Any
) -> dict[str, str]:
    """Read a config map as {key: val}. Missing config is an empty map, not an
    error - callers decide whether a given key is required.

    A null config value becomes "" rather than None. Callers feed these values
    straight into `json.loads`, `.lower()` and `.replace()`; an empty string
    degrades gracefully there (a JSONDecodeError the callers already handle),
    whereas None would raise TypeError/AttributeError and crash the run. A new
    pilot with a half-filled config is exactly the case this protects.
    """
    response = client.request(
        "GET",
        f"/v2.0/configs/{config_type}/{entity}/{entity_id}",
        tolerate=(404, 500),
    )
    if response is None:
        return {}
    try:
        payload = payload_of(response)
    except SetupError:
        return {}
    if not isinstance(payload, list):
        return {}
    return {
        item["key"]: (item.get("val") or "")
        for item in payload
        if isinstance(item, dict) and item.get("key") is not None
    }


@dataclass
class PilotContext:
    """Everything the scripts need about one pilot, read from the pilot itself."""

    client: ApiClient
    pilot_id: int
    region: str
    _s3_pull: dict[str, str] = field(default_factory=dict, repr=False)
    _data_ingestion: dict[str, str] = field(default_factory=dict, repr=False)
    _launchpad: dict[str, str] = field(default_factory=dict, repr=False)
    _queue_url_override: str | None = None
    _resolved_queue_url: str | None = field(default=None, repr=False)

    @classmethod
    def load(
        cls,
        client: ApiClient,
        pilot_id: int,
        region: str,
        queue_url: str | None = None,
    ) -> PilotContext:
        context = cls(
            client=client,
            pilot_id=int(pilot_id),
            region=region,
            _queue_url_override=queue_url,
        )
        context._s3_pull = read_config_map(client, CONFIG_S3_PULL, "pilot", pilot_id)
        context._data_ingestion = read_config_map(
            client, CONFIG_DATA_INGESTION, "pilot", pilot_id
        )
        context._launchpad = read_config_map(
            client, CONFIG_LAUNCHPAD_INGESTION, "pilot", pilot_id
        )
        if not context._s3_pull and not context._data_ingestion:
            raise SetupError(
                f"Pilot {pilot_id} returned no s3_pull or data_ingestion "
                f"configuration. Check PILOT_ID and that the token can read "
                f"this pilot."
            )
        return context

    # ---------------- ingestion ---------------- #

    @property
    def ingestion_bucket(self) -> str:
        bucket = self._data_ingestion.get("s3BucketName") or self._s3_pull.get(
            "s3DestinationBucket"
        )
        if not bucket:
            raise SetupError(
                f"No ingestion bucket configured for pilot {self.pilot_id} "
                f"(looked for data_ingestion.s3BucketName and "
                f"s3_pull.s3DestinationBucket)"
            )
        return bucket

    @property
    def enroll_file_prefix(self) -> str:
        prefix = self._s3_pull.get("s3EnrollFilePrefix") or self._s3_pull.get(
            "enrollFilePrefix"
        )
        if not prefix:
            raise SetupError(
                f"No enrolment file prefix configured for pilot {self.pilot_id}"
            )
        return prefix

    @property
    def user_creation_delimiter(self) -> str:
        return self._launchpad.get("user_creation_parser_delimiter") or "|"

    @property
    def user_enrollment_date_format(self) -> str:
        return self._launchpad.get("user_enrollment_date_format") or "yyyy-MM-dd"

    @property
    def parser_timezone(self) -> str:
        return self._launchpad.get("parser_time_zone") or "UTC"

    @property
    def consumption_unit(self) -> str:
        email = read_config_map(
            self.client, CONFIG_EMAIL_MODERATION, "pilot", self.pilot_id
        )
        return email.get("consumption_unit_type") or "kWh"

    def launchpad_field_positions(self) -> dict[str, int]:
        """Field name -> position in the user_creation_launchpad contract.

        Each config value is a JSON object carrying the field's position, e.g.
        `email_id -> {"fieldPosition": 4, ...}`. A value that isn't parseable
        is skipped rather than failing the whole contract read - a new pilot
        may have entries this repo has never seen.
        """
        raw = read_config_map(
            self.client, CONFIG_USER_CREATION_LAUNCHPAD, "pilot", self.pilot_id
        )
        positions: dict[str, int] = {}
        for key, value in raw.items():
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(parsed, dict):
                continue
            position = parsed.get("fieldPosition")
            if position is None:
                continue
            try:
                positions[key] = int(position)
            except (TypeError, ValueError):
                continue
        return positions

    # ---------------- rate plans ---------------- #

    def rate_plans(self) -> list[dict[str, Any]]:
        """The pilot's own rate plan list, so no plan number is hardcoded."""
        payload = payload_of(
            self.client.request(
                "GET", f"/v3.0/rates/configuration/utilityId/{self.pilot_id}"
            )
        )
        if isinstance(payload, dict):
            for key in ("ratePlans", "rates", "configurations"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        if isinstance(payload, list):
            return payload
        raise SetupError(f"Could not read rate plans for pilot {self.pilot_id}")

    # ---------------- notification queue ---------------- #

    @property
    def env_candidates(self) -> list[str]:
        """Environment names to try, derived from the API hostname."""
        host = urllib.parse.urlparse(self.client.base_url).hostname or ""
        stem = host.split(".")[0]
        parts = [p for p in stem.split("-") if p not in ("api", "server")]
        candidates: list[str] = []
        if len(parts) >= 2:
            candidates.append("-".join(reversed(parts)))
            candidates.append("-".join(parts))
        if parts:
            candidates.append(parts[-1])
        seen: set[str] = set()
        return [c for c in candidates if c and not (c in seen or seen.add(c))]

    def notification_queue_url(self) -> str:
        """Resolve the environment's notification queue.

        Identifies the environment by which suffix resolves the pilot's own
        enrolment queue, then reuses that environment for the notification
        queue - so nothing is assumed about the queue's name. Resolution costs
        several AWS calls, so the result is cached per context.
        """
        if self._queue_url_override:
            return self._queue_url_override
        if self._resolved_queue_url:
            return self._resolved_queue_url

        probe_base = self._s3_pull.get("enrollFileQueue") or DEFAULT_ENROLL_QUEUE_BASE
        tried: list[str] = []
        for env in self.env_candidates:
            if not queue_exists(self.region, f"{probe_base}-{env}"):
                continue
            for pattern in NOTIFICATION_QUEUE_PATTERNS:
                name = pattern.format(env=env)
                tried.append(name)
                if not queue_exists(self.region, name):
                    continue
                resolved = run_aws_json(
                    [
                        "sqs",
                        "get-queue-url",
                        "--region",
                        self.region,
                        "--queue-name",
                        name,
                    ]
                )
                url = resolved.get("QueueUrl")
                if url:
                    self._resolved_queue_url = str(url)
                    return self._resolved_queue_url
        raise SetupError(
            "Could not identify the environment's notification queue "
            f"(tried {', '.join(tried) or 'none'}). Set QUEUE_URL in the config "
            "to pass it explicitly."
        )


def queue_exists(region: str, name: str) -> bool:
    completed = subprocess.run(
        [
            "aws",
            "sqs",
            "get-queue-url",
            "--region",
            region,
            "--queue-name",
            name,
            "--output",
            "json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0
