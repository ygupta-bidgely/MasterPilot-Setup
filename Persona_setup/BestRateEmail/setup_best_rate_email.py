#!/usr/bin/env python3
"""Prepare and trigger a user-scoped Best Rate email.

Driven by the shared `Persona_setup/config.json`. The script intentionally
does not write pilot-level configuration. It:
  1. Reads the user's live BILLING_CYCLE_PROJECTED Rate Comparison result.
  2. Selects the highest-savings non-current rate.
  3. Adds user-level email/NBI configuration and rendering resources.
  4. Writes and verifies a manual RATE_COMPARISON interaction.
  5. Resets the user's RATE_COMPARISON Email sent count.
  6. Optionally reruns aggregation.
  7. Publishes the dedicated notification event to the environment's
     notification queue (discovered from the pilot).
  8. Polls notification status for sentCount >= 1.

The token is never printed. AWS credentials and permissions are resolved by
the AWS CLI.
"""

from __future__ import annotations

import json
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import ApiClient as SharedApiClient  # noqa: E402
from _common import PilotContext  # noqa: E402
from _common import SetupError as SharedSetupError  # noqa: E402
from _common import load as load_shared_config  # noqa: E402
from _common.config import CONFIG_PATH as SHARED_CONFIG_PATH  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPT_NAME = "BestRateEmail"

DEFAULT_HOME_ORDINAL = 1
DEFAULT_REGION = "us-west-2"
DEFAULT_HTTP_TIMEOUT = 60
DEFAULT_AGGREGATION_WAIT_SECONDS = 30
DEFAULT_STATUS_TIMEOUT = 180
DEFAULT_STATUS_INTERVAL = 10
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "output"
MEASUREMENT_TYPE = "ELECTRIC"
EVENT_NAME = "RATE_COMPARISON"
DELIVERY_MODE = "Email"
ASSET_TYPE = "EMAIL_SECTION_HTML"


class SetupError(RuntimeError):
    """A safe, user-readable setup failure."""


@dataclass(frozen=True)
class Recommendation:
    current_plan_number: int
    current_plan_name: str
    current_annual_cost: int
    recommended_plan_number: int
    recommended_plan_name: str
    recommended_annual_cost: int
    annual_savings: int
    savings_percentage: Decimal
    comparison_end: int

    @property
    def nbi_id(self) -> str:
        return f"nbi_rate_comparison_plan_{self.recommended_plan_number}"

    @property
    def insight_id(self) -> str:
        return f"i_rate_comparison_plan_{self.recommended_plan_number}"

    @property
    def action_id(self) -> str:
        return f"a_rate_comparison_plan_{self.recommended_plan_number}"


class ApiClient:
    def __init__(self, base_url: str, token: str, timeout: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.ssl_context = self._build_ssl_context()

    @staticmethod
    def _build_ssl_context() -> ssl.SSLContext:
        """Use certifi on Python.org macOS builds that have no default CA file."""
        try:
            import certifi

            return ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            return ssl.create_default_context()

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: Any | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        data = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self.ssl_context
            ) as response:
                raw = response.read().decode("utf-8")
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise SetupError(
                f"{method} {path} failed with HTTP {exc.code}: {raw[:1000]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise SetupError(f"{method} {path} failed: {exc.reason}") from exc

        if status not in expected:
            raise SetupError(f"{method} {path} returned unexpected HTTP {status}")
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw


def log(step: str) -> None:
    print(f"\n==> {step}", flush=True)


def round_money(value: Any) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def require_uuid(value: str) -> str:
    if not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
        r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
        value,
    ):
        raise SetupError("UUID must be a valid UUID")
    return value.lower()


def payload_of(response: Any) -> Any:
    if isinstance(response, dict) and "error" in response and response["error"]:
        raise SetupError(f"API returned an error: {response['error']}")
    if isinstance(response, dict) and "payload" in response:
        return response["payload"]
    return response


def fetch_recommendation(
    client: ApiClient, user_id: str, home_ordinal: int, pilot_id: int
) -> Recommendation:
    response = client.request(
        "GET",
        f"/v3.0/users/{user_id}/homes/{home_ordinal}/rate-comparison/details",
        query={
            "measurement-type": MEASUREMENT_TYPE,
            "scale-values": "true",
            "locale": "en_US",
            "computationType": "BILLING_CYCLE_PROJECTED",
        },
    )
    payload = payload_of(response)
    if not isinstance(payload, dict):
        raise SetupError("Rate Comparison response has no payload object")

    rates = payload.get("comparedRates") or []
    current_rates = [rate for rate in rates if rate.get("isCurrentRate") is True]
    alternatives = [rate for rate in rates if rate.get("isCurrentRate") is False]
    if len(current_rates) != 1:
        raise SetupError(f"Expected exactly one current rate, found {len(current_rates)}")
    if not alternatives:
        raise SetupError("Rate Comparison returned no alternative rates")

    current = current_rates[0]
    if int(current.get("utilityId", -1)) != pilot_id:
        raise SetupError(
            f"Current rate utilityId {current.get('utilityId')} does not match pilot {pilot_id}"
        )

    recommended = next(
        (rate for rate in alternatives if rate.get("isHighestSavings") is True), None
    )
    if recommended is None:
        recommended = max(
            alternatives,
            key=lambda rate: Decimal(str(rate.get("savings", "-Infinity"))),
        )

    current_cost = round_money(current["totalCost"])
    recommended_cost = round_money(recommended["totalCost"])
    savings = round_money(recommended.get("savings", current_cost - recommended_cost))
    if savings <= 0:
        raise SetupError(
            f"Highest candidate does not save money (computed annual savings: {savings})"
        )

    savings_percentage = (
        (Decimal(savings) * Decimal("100")) / Decimal(current_cost)
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    comparison_end = payload.get("comparisonDurationEndTS") or payload.get(
        "comparisonDurationEnd"
    )
    if not comparison_end:
        raise SetupError("Rate Comparison payload has no comparison duration end timestamp")

    return Recommendation(
        current_plan_number=int(current["planNumber"]),
        current_plan_name=str(current.get("planName") or current["planNumber"]),
        current_annual_cost=current_cost,
        recommended_plan_number=int(recommended["planNumber"]),
        recommended_plan_name=str(
            recommended.get("planName") or recommended["planNumber"]
        ),
        recommended_annual_cost=recommended_cost,
        annual_savings=savings,
        savings_percentage=savings_percentage,
        comparison_end=int(comparison_end),
    )


def post_user_config(
    client: ApiClient, user_id: str, config_type: str, values: list[tuple[str, str, str]]
) -> None:
    config_kvs = []
    for key, value, data_type in values:
        config_kvs.append(
            {
                "configKey": key,
                "configVal": value,
                "configDocumentation": "User-only Best Rate email test configuration",
                "valueDocumentation": "Managed by setup_best_rate_email.py",
                "configRegex": ".*" if data_type == "TEXT" else "[0-9]+",
                "configDataType": data_type,
                "configTags": ["Email", "RateComparison", "ProductQA"],
            }
        )
    client.request(
        "POST",
        f"/entities/{user_id}/configs",
        body={"configType": config_type, "configKVs": config_kvs},
        expected=(200, 201),
    )


def configure_user(client: ApiClient, user_id: str) -> None:
    configs: list[tuple[str, list[tuple[str, str, str]]]] = [
        (
            "email_nbi_section_configs",
            [
                ("rate_comparison_email_profile_mode", "batch", "TEXT"),
                ("rate_comparison_email_threshold_value", "-1.0", "TEXT"),
                ("top_rate_comparison_filter_count", "1", "INTEGER"),
            ],
        ),
        (
            "event_subscriptions.RATE_COMPARISON.ELECTRIC",
            [("delivery_modes", '["Email"]', "TEXT")],
        ),
        (
            "event_subscriptions.RATE_COMPARISON.ELECTRIC.OPT_OUT",
            [("delivery_modes", '["Email"]', "TEXT")],
        ),
        (
            "email_template_sections_config",
            [
                (
                    "RATE_COMPARISON.OPT_OUT",
                    json.dumps(
                        [
                            {"emailTemplateSection": "TEASER"},
                            {"emailTemplateSection": "HEADER"},
                            {"emailTemplateSection": "GREETING"},
                            {
                                "emailTemplateSection": "RATE_COMPARISON",
                                "mandatory": True,
                            },
                            {"emailTemplateSection": "FEEDBACK"},
                            {"emailTemplateSection": "FOOTER"},
                        ],
                        separators=(",", ":"),
                    ),
                    "TEXT",
                )
            ],
        ),
        (
            "email_feedback_section_configs",
            [
                (
                    "title_text_content",
                    "com.bidgely.cloud.email.content.feedback.title.v3",
                    "TEXT",
                )
            ],
        ),
    ]
    for config_type, values in configs:
        post_user_config(client, user_id, config_type, values)
        print(f"  configured {config_type}")


def put_string_resource(
    client: ApiClient, user_id: str, resource_id: str
) -> None:
    text = (
        "Did you know, you can <b>save $${annual_savings} per year</b> by "
        "switching from plan ${current_plan_number} to plan "
        "${recommended_plan_name}?<br/><br/>This is the lowest-cost plan for "
        "your recent usage."
    )
    client.request(
        "PUT",
        f"/2.1/stringResources/{user_id}/resource/{resource_id}",
        body=[{"locale": "en_US", "text": text}],
        expected=(200, 201),
    )


def upsert_asset(
    client: ApiClient,
    user_id: str,
    asset_id: str,
    asset_key: str,
    asset_value: str,
    asset_value_type: str,
) -> None:
    query = {
        "entityId": user_id,
        "assetId": asset_id,
        "assetKey": asset_key,
        "assetValue": asset_value,
        "assetType": ASSET_TYPE,
        "assetValueType": asset_value_type,
    }
    try:
        client.request(
            "GET",
            "/v1/nbi/assets",
            query={"entityId": user_id, "assetId": asset_id, "assetKey": asset_key},
        )
        method = "PUT"
        expected = (200,)
    except SetupError as exc:
        if "HTTP 404" not in str(exc):
            raise
        method = "POST"
        expected = (200, 201)
    client.request(method, "/v1/nbi/assets", query=query, expected=expected)


def configure_nbi_assets(
    client: ApiClient,
    user_id: str,
    recommendation: Recommendation,
    dashboard_url: str,
) -> str:
    resource_id = (
        f"com.bidgely.productqa.ratecomparison.plan"
        f"{recommendation.recommended_plan_number}.insight"
    )
    put_string_resource(client, user_id, resource_id)

    assets = [
        (
            recommendation.nbi_id,
            "emailSubject",
            "com.bidgely.cloud.core.lib.nbi.email.subject.new.rate.plan",
            "STRING_RESOURCE",
        ),
        (
            recommendation.nbi_id,
            "interactionOrder",
            json.dumps(
                [recommendation.insight_id, recommendation.action_id],
                separators=(",", ":"),
            ),
            "INTERACTION_ORDER",
        ),
        (
            recommendation.nbi_id,
            "disclaimerText1",
            "com.bidgely.cloud.core.lib.nbi.email.disclaimer1.dollar",
            "STRING_RESOURCE",
        ),
        (
            recommendation.insight_id,
            "text_with_circle_image-Image",
            "https://dsxxxuy8jkhol.cloudfront.net/nbi-emails/save.png",
            "IMAGE",
        ),
        (
            recommendation.insight_id,
            "text_with_circle_image-insightText",
            resource_id,
            "STRING_RESOURCE",
        ),
        (
            recommendation.insight_id,
            "velocityOrder",
            '["text_with_circle_image.vm"]',
            "VELOCITY_TEMPLATE",
        ),
        (
            recommendation.action_id,
            "full_width_image-imageURL",
            "https://dsxxxuy8jkhol.cloudfront.net/nbi-emails/rates-highersave.png",
            "IMAGE",
        ),
        (
            recommendation.action_id,
            "text_with_cta_button-button_text",
            "com.bidgely.cloud.core.lib.action.button.text.A_R_E_TE_001",
            "STRING_RESOURCE",
        ),
        (
            recommendation.action_id,
            "text_with_cta_button-description",
            "com.bidgely.cloud.core.lib.action.description.A_R_E_TE_001",
            "STRING_RESOURCE",
        ),
        (
            recommendation.action_id,
            "text_with_cta_button-title",
            "com.bidgely.cloud.core.lib.action.title.A_R_E_TE_001",
            "STRING_RESOURCE",
        ),
        (
            recommendation.action_id,
            "text_with_cta_button-url",
            dashboard_url,
            "ENDPOINT",
        ),
        (
            recommendation.action_id,
            "velocityOrder",
            '["text_with_cta_button.vm","full_width_image.vm"]',
            "VELOCITY_TEMPLATE",
        ),
    ]
    for asset_id, asset_key, asset_value, value_type in assets:
        upsert_asset(
            client, user_id, asset_id, asset_key, asset_value, value_type
        )
    return resource_id


def build_interaction(
    user_id: str, pilot_id: int, recommendation: Recommendation
) -> dict[str, Any]:
    return {
        "interactions": [
            {
                "rank": 1,
                "id": recommendation.nbi_id,
                "score": 1.0,
                "nbiType": EVENT_NAME,
                "nbiFamily": "EE",
                "applianceId": 0,
                "fuelType": MEASUREMENT_TYPE,
                "insight": {
                    "id": recommendation.insight_id,
                    "score": 1.0,
                    "language": "en_US",
                    "text": (
                        f"You could save approximately ${recommendation.annual_savings} "
                        f"per year with rate plan {recommendation.recommended_plan_name}."
                    ),
                    "description": "Best Rate Plan recommendation",
                    "message": (
                        f"Plan {recommendation.recommended_plan_number} "
                        f"({recommendation.recommended_plan_name}) is the "
                        "highest-savings alternative."
                    ),
                    "values": {
                        "current_plan_number": str(
                            recommendation.current_plan_number
                        ),
                        "recommended_plan_number": str(
                            recommendation.recommended_plan_number
                        ),
                        "recommended_plan_name": recommendation.recommended_plan_name,
                        "current_annual_cost": str(recommendation.current_annual_cost),
                        "recommended_annual_cost": str(
                            recommendation.recommended_annual_cost
                        ),
                        "annual_savings": str(recommendation.annual_savings),
                        "savings_percentage": str(recommendation.savings_percentage),
                    },
                },
                "action": {
                    "id": recommendation.action_id,
                    "score": 1.0,
                    "language": "en_US",
                    "text": "View rate plan details",
                    "description": "Review the recommended rate plan",
                    "message": (
                        f"Compare your current plan with plan "
                        f"{recommendation.recommended_plan_name}."
                    ),
                    "values": {
                        "channel": "email",
                        "recommended_plan_number": str(
                            recommendation.recommended_plan_number
                        ),
                    },
                },
                "hash": (
                    f"rc-{pilot_id}-{user_id[:8]}-plan"
                    f"{recommendation.recommended_plan_number}-"
                    f"{recommendation.comparison_end}"
                ),
            }
        ],
        "nbi_delivery_helper_dict": {
            "billing_info": {
                "last_electric_billing_cycle_info": {
                    "last_billing_start": recommendation.comparison_end - 31 * 86400,
                    "last_billing_end": recommendation.comparison_end,
                }
            }
        },
    }


def contains_interaction_id(value: Any, expected_id: str) -> bool:
    if isinstance(value, dict):
        if value.get("id") == expected_id:
            return True
        return any(contains_interaction_id(item, expected_id) for item in value.values())
    if isinstance(value, list):
        return any(contains_interaction_id(item, expected_id) for item in value)
    return False


def write_legacy_interaction(
    client: ApiClient,
    user_id: str,
    home_ordinal: int,
    interaction: dict[str, Any],
) -> None:
    path = f"/v3.0/internal/users/{user_id}/homes/{home_ordinal}/interactions"
    client.request("PUT", path, body=interaction, expected=(200, 201, 204))


def verify_interaction(
    client: ApiClient,
    user_id: str,
    home_ordinal: int,
    expected_nbi_id: str,
) -> None:
    path = f"/v3.0/internal/users/{user_id}/homes/{home_ordinal}/interactions"
    response = client.request(
        "GET",
        path,
        query={
            "nbiRun": EVENT_NAME,
            "deliveryMode": DELIVERY_MODE,
            "deliveryType": EVENT_NAME,
            "fuelType": MEASUREMENT_TYPE,
            "mode": "BATCH",
            "top": 5,
            "score-above": "-1.0",
        },
    )
    if not contains_interaction_id(response, expected_nbi_id):
        raise SetupError(
            f"Partitioned interaction {expected_nbi_id} was not selected by "
            "the verification query"
        )


def upload_partitioned_interaction(
    artifact_path: Path,
    user_id: str,
    bucket: str,
    region: str,
) -> str:
    """Upload the profile where RATE_COMPARISON notification selection reads it.

    The internal PUT endpoint writes a legacy unpartitioned profile path. The
    notification flow reads the nbiRun/deliveryMode/deliveryType/fuelType
    partition instead, so the plain JSON artifact must also be placed there.
    """
    batch_id = int(time.time())
    key = (
        "home/mode=batch/profile=interaction/"
        f"uuid={user_id}/source=DISAGG/"
        f"nbiRun={EVENT_NAME}/deliveryMode={DELIVERY_MODE}/"
        f"deliveryType={EVENT_NAME}/fuelType={MEASUREMENT_TYPE}/"
        f"batch={batch_id}/{user_id}.json"
    )
    run_aws_json(
        [
            "s3api",
            "put-object",
            "--region",
            region,
            "--bucket",
            bucket,
            "--key",
            key,
            "--body",
            str(artifact_path.resolve()),
            "--content-type",
            "application/json",
        ]
    )
    return f"s3://{bucket}/{key}"


def notification_status_path(user_id: str, home_ordinal: int) -> str:
    return (
        f"/notification/notificationStatus/{user_id}/{home_ordinal}/"
        f"{EVENT_NAME}/{MEASUREMENT_TYPE}/{DELIVERY_MODE}"
    )


def sent_count(response: Any) -> int | None:
    payload = payload_of(response)
    if isinstance(payload, dict) and "sentCount" in payload:
        return int(payload["sentCount"])
    if isinstance(response, dict) and "sentCount" in response:
        return int(response["sentCount"])
    return None


def reset_sent_count(client: ApiClient, user_id: str, home_ordinal: int) -> None:
    client.request(
        "POST",
        notification_status_path(user_id, home_ordinal),
        body={"sentCount": 0},
        expected=(200, 201, 204),
    )


def rerun_aggregation(client: ApiClient, user_id: str, home_ordinal: int) -> None:
    client.request(
        "POST",
        f"/billingdata/users/{user_id}/homes/{home_ordinal}/run/aggregations",
        body={},
        expected=(200, 201, 202, 204),
    )


def run_aws_json(arguments: list[str]) -> dict[str, Any]:
    command = ["aws", *arguments, "--output", "json"]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        error = completed.stderr.strip() or completed.stdout.strip()
        raise SetupError(f"AWS CLI failed: {error[:1000]}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SetupError("AWS CLI did not return valid JSON") from exc


def send_notification(
    user_id: str,
    home_ordinal: int,
    region: str,
    queue_url: str,
) -> str:
    resolved_queue_url = queue_url
    message = {
        "userId": user_id,
        "homeOrdinal": home_ordinal,
        "metaData": {
            "NbiType": EVENT_NAME,
            "deliveryMode": DELIVERY_MODE,
            "EventName": EVENT_NAME,
        },
    }
    response = run_aws_json(
        [
            "sqs",
            "send-message",
            "--region",
            region,
            "--queue-url",
            resolved_queue_url,
            "--message-body",
            json.dumps(message, separators=(",", ":")),
        ]
    )
    message_id = response.get("MessageId")
    if not message_id:
        raise SetupError("SQS accepted no message ID")
    return str(message_id)


def poll_for_email(
    client: ApiClient,
    user_id: str,
    home_ordinal: int,
    timeout_seconds: int,
    interval_seconds: int,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    last_count: int | None = None
    while time.monotonic() <= deadline:
        response = client.request(
            "GET", notification_status_path(user_id, home_ordinal)
        )
        last_count = sent_count(response)
        if last_count is not None and last_count >= 1:
            return last_count
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval_seconds, max(0, remaining)))
    raise SetupError(
        f"Notification sentCount did not reach 1 within {timeout_seconds}s "
        f"(last value: {last_count}). Check Notifications Processor and Emailer logs."
    )


def write_artifact(
    output_dir: Path,
    user_id: str,
    interaction: dict[str, Any],
    recommendation: Recommendation,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"manual-rate-comparison-nbi-{user_id}.json"
    path.write_text(json.dumps(interaction, indent=2) + "\n", encoding="utf-8")
    summary_path = output_dir / f"best-rate-summary-{user_id}.json"
    summary_path.write_text(
        json.dumps(
            {
                "userId": user_id,
                "nbiId": recommendation.nbi_id,
                "currentPlan": recommendation.current_plan_number,
                "recommendedPlan": recommendation.recommended_plan_number,
                "recommendedPlanName": recommendation.recommended_plan_name,
                "annualSavings": recommendation.annual_savings,
                "savingsPercentage": str(recommendation.savings_percentage),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def load_config() -> dict[str, Any]:
    shared = load_shared_config(SCRIPT_NAME)
    users = shared.users_for(SCRIPT_NAME)
    if not users:
        raise SetupError(
            f"No users configured for {SCRIPT_NAME}. Add one to USERS in "
            f'{SHARED_CONFIG_PATH} with "scripts": ["{SCRIPT_NAME}"].'
        )
    if len(users) > 1:
        raise SetupError(
            f"{len(users)} users configured for {SCRIPT_NAME}; this script runs "
            f"one user at a time. Use run_personas.py or narrow the config."
        )
    user = users[0]

    config: dict[str, Any] = {
        "UUID": user["UUID"],
        "AUTH_TOKEN": shared.token,
        "BASE_URL": shared.base_url,
        "HOME_ORDINAL": shared.home_ordinal,
        "PILOT_ID": shared.pilot_id,
        "REGION": shared.region,
    }

    # Where the partitioned interaction profile is uploaded. This is the
    # platform's profile-data bucket (bidgely-profile-data-<env>), not the
    # pilot's ingestion bucket, so it cannot be derived from the pilot's own
    # config - it stays a required setting rather than a guess.
    config["PROFILE_BUCKET"] = shared.require("PROFILE_BUCKET")
    # The action's "View rate plan details" CTA link. Environment-specific, so
    # there is no safe default.
    config["DASHBOARD_URL"] = shared.require("DASHBOARD_URL")

    # The notification queue is discovered from the pilot; QUEUE_URL in the
    # shared config short-circuits the discovery when it is passed explicitly.
    config["QUEUE_URL"] = shared.queue_url

    config.setdefault("HTTP_TIMEOUT", shared.http_timeout)
    config.setdefault("AGGREGATION_WAIT_SECONDS", DEFAULT_AGGREGATION_WAIT_SECONDS)
    config.setdefault("STATUS_TIMEOUT", DEFAULT_STATUS_TIMEOUT)
    config.setdefault("STATUS_INTERVAL", DEFAULT_STATUS_INTERVAL)
    return config


def main() -> int:
    try:
        config = load_config()
    except (SetupError, SharedSetupError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        if shutil.which("aws") is None:
            raise SetupError("AWS CLI is required to publish the notification")

        user_id = config["UUID"]
        pilot_id = int(config["PILOT_ID"])
        home_ordinal = int(config["HOME_ORDINAL"])

        client = ApiClient(
            config["BASE_URL"], str(config["AUTH_TOKEN"]).strip(), int(config["HTTP_TIMEOUT"])
        )

        log("Reading live Rate Comparison result")
        recommendation = fetch_recommendation(client, user_id, home_ordinal, pilot_id)
        print(
            f"  current plan {recommendation.current_plan_number}: "
            f"${recommendation.current_annual_cost}/year\n"
            f"  recommended plan {recommendation.recommended_plan_number} "
            f"({recommendation.recommended_plan_name}): "
            f"${recommendation.recommended_annual_cost}/year\n"
            f"  savings: ${recommendation.annual_savings}/year "
            f"({recommendation.savings_percentage}%)"
        )

        log("Applying user-only Best Rate configuration")
        configure_user(client, user_id)

        log("Creating user-only string resource and NBI rendering assets")
        resource_id = configure_nbi_assets(
            client, user_id, recommendation, config["DASHBOARD_URL"]
        )
        print(f"  NBI: {recommendation.nbi_id}")
        print(f"  insight resource: {resource_id}")

        log("Writing and verifying the manual NBI interaction")
        interaction = build_interaction(user_id, pilot_id, recommendation)
        artifact_path = write_artifact(
            DEFAULT_OUTPUT_DIR, user_id, interaction, recommendation
        )
        write_legacy_interaction(client, user_id, home_ordinal, interaction)
        s3_uri = upload_partitioned_interaction(
            artifact_path,
            user_id,
            config["PROFILE_BUCKET"],
            config["REGION"],
        )
        verify_interaction(client, user_id, home_ordinal, recommendation.nbi_id)
        print(f"  generated artifact: {artifact_path.resolve()}")
        print(f"  selectable profile: {s3_uri}")

        log("Resetting RATE_COMPARISON Email sentCount to 0")
        reset_sent_count(client, user_id, home_ordinal)

        log("Requesting aggregation rerun")
        rerun_aggregation(client, user_id, home_ordinal)
        aggregation_wait_seconds = int(config["AGGREGATION_WAIT_SECONDS"])
        print(f"  waiting {aggregation_wait_seconds}s before notification")
        time.sleep(aggregation_wait_seconds)

        # Discovered from the pilot itself rather than assuming a queue name.
        # PilotContext tolerates 404/500 on the pilot config reads via
        # `expected`, which only the shared client honours, so discovery gets
        # its own client rather than this module's stricter one.
        queue_url = PilotContext.load(
            SharedApiClient(
                config["BASE_URL"],
                str(config["AUTH_TOKEN"]).strip(),
                int(config["HTTP_TIMEOUT"]),
            ),
            pilot_id,
            config["REGION"],
            config["QUEUE_URL"],
        ).notification_queue_url()
        log(f"Publishing notification to {queue_url}")
        message_id = send_notification(
            user_id,
            home_ordinal,
            config["REGION"],
            queue_url,
        )
        print(f"  SQS message ID: {message_id}")

        log("Waiting for notification sentCount >= 1")
        count = poll_for_email(
            client,
            user_id,
            home_ordinal,
            int(config["STATUS_TIMEOUT"]),
            int(config["STATUS_INTERVAL"]),
        )
        print(f"  SUCCESS: sentCount={count}")

        print(
            "\nBest Rate event completed. The actual recipient address comes from "
            "the user's profile; it is not part of the SQS payload."
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
