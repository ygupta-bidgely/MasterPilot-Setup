#!/usr/bin/env python3
"""Prepare and trigger a user-scoped TOU Rate Onboarding email in MasterPilot ProductQA.

Driven by `config.json` (see config.json.example). The script never writes
pilot-level configuration; every override it creates is scoped to the single
user configured. It:
  1. Reads the user record for pilot, notification user type and current rate plan.
  2. Reads the pilot's launchpad ingestion contract (file layout, bucket, delimiter).
  3. Picks a TOU rate plan that is currently valid and has enough TOU months.
  4. Writes user-level configs plus the subject string resource the email needs.
  5. Clones the user's newest USERENROLL row, swaps the rate plan, uploads to S3.
  6. Waits for the rate transition to land on the home.
  7. Resets the sent count and, if ingestion did not fire, publishes the event.
  8. Polls notification status, then fetches and verifies the rendered email.

Only UUID and AUTH_TOKEN are required. The token is never printed. AWS
credentials and permissions are resolved by the AWS CLI.
"""

from __future__ import annotations

import json
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zoneinfo
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

REQUIRED_CONFIG_KEYS = ("AUTH_TOKEN", "UUID")

DEFAULT_BASE_URL = "https://api-server-masterpilot-productqa.bidgely.com"
DEFAULT_HOME_ORDINAL = 1
DEFAULT_REGION = "us-west-2"
DEFAULT_HTTP_TIMEOUT = 60
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "output"
DEFAULT_SUBJECT_TEXT = "Your New Rate Can Help You Save"
DEFAULT_SCAN_LIMIT = 40
DEFAULT_TRANSITION_TIMEOUT = 300
DEFAULT_EMAIL_TIMEOUT = 240
DEFAULT_POLL_INTERVAL = 10
DEFAULT_MAX_PLAN_ATTEMPTS = 3

EVENT_NAME = "TOU_ONBOARDING"
DELIVERY_MODE = "Email"
SUBJECT_RESOURCE_ID = "com.bidgely.cloud.email.subject.tou.onboarding"
EMPTY_STRING_RESOURCE = "com.bidgely.cloud.empty.string"

# Verified layout for this event. HEADER/FEEDBACK/FOOTER are the V3 variants
# because the V1 FEEDBACK section aborts the Velocity render unless
# title_text_content is configured. CTA is deliberately absent: it renders the
# Monthly Summary home-profile/set-budget blocks, whose string resources are the
# same placeholder on some pilots, which shows up as a duplicated line.
DEFAULT_SECTIONS = [
    {"emailTemplateSection": "HEADER_V3"},
    {"emailTemplateSection": "TOU_ONBOARDING_WELCOME", "mandatory": True},
    {"emailTemplateSection": "TOU_RATE_STRUCTURE", "mandatory": True},
    {"emailTemplateSection": "TOU_BILL_SAVING_RECO"},
    {"emailTemplateSection": "TOU_MOTIVATION"},
    {"emailTemplateSection": "FEEDBACK_V3"},
    {"emailTemplateSection": "FOOTER_V3"},
]

CONSUMPTION_BASED = "CONSUMPTION_BASED"
MANAGED_BY = "Managed by setup_tou_onboarding_email.py"

# Java SimpleDateFormat -> strftime, enough for the launchpad date configs.
DATE_PATTERN_TOKENS = [("yyyy", "%Y"), ("MM", "%m"), ("dd", "%d")]


class SetupError(RuntimeError):
    """A safe, user-readable setup failure."""


@dataclass(frozen=True)
class UserContext:
    uuid: str
    pilot_id: int
    notification_user_type: str
    measurement_type: str
    locale: str
    timezone_name: str
    partner_user_id: str
    current_plan_number: int | None
    current_rate_plan_id: str | None
    rates_schedule: list[dict[str, Any]]


@dataclass(frozen=True)
class IngestionContract:
    bucket: str
    delimiter: str
    enroll_prefix: str
    date_format: str
    parser_timezone: str
    field_positions: dict[str, int]


@dataclass
class RatePlan:
    plan_number: int
    plan_name: str
    tou_months: set[int] = field(default_factory=set)
    current_month_is_tou: bool = False
    has_weekday_band: bool = False
    has_weekend_band: bool = False
    tou_names: set[str] = field(default_factory=set)

    @property
    def is_tou(self) -> bool:
        return bool(self.tou_months)

    @property
    def has_split_week_bands(self) -> bool:
        """Plans with one wk1-7 band leave weekEndPeakHrs null and fail to render.

        generateTouRateStructureResponse only fills weekEndPeakHrs when a band has
        both ends inside {6,7}; a single 1-7 band takes the weekday branch instead.
        Verified on pilot 88001: plan 194 (split 1-5 / 6-7) renders, plan 622
        (single 1-7) drops the email.
        """
        return self.has_weekday_band and self.has_weekend_band

    def describe(self) -> str:
        return (
            f"{self.plan_name} (planNumber {self.plan_number}): "
            f"{len(self.tou_months)} TOU months, currentMonthIsTou={self.current_month_is_tou}, "
            f"splitWeekBands={self.has_split_week_bands}, tou={sorted(self.tou_names)}"
        )


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
        tolerate: tuple[int, ...] = (),
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
            if exc.code in tolerate:
                return None
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


def detail(message: str) -> None:
    print(f"    {message}", flush=True)


def require_uuid(value: str) -> str:
    if not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
        r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
        value,
    ):
        raise SetupError("UUID must be a valid UUID")
    return value.lower()


def payload_of(response: Any) -> Any:
    if isinstance(response, dict) and response.get("error"):
        raise SetupError(f"API returned an error: {response['error']}")
    if isinstance(response, dict) and "payload" in response:
        return response["payload"]
    return response


def run_aws_json(arguments: list[str]) -> dict[str, Any]:
    command = ["aws", *arguments, "--output", "json"]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        error = completed.stderr.strip() or completed.stdout.strip()
        raise SetupError(f"AWS CLI failed: {error[:1000]}")
    if not completed.stdout.strip():
        return {}
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SetupError("AWS CLI did not return valid JSON") from exc


def run_aws(arguments: list[str]) -> None:
    completed = subprocess.run(
        ["aws", *arguments], text=True, capture_output=True, check=False
    )
    if completed.returncode != 0:
        error = completed.stderr.strip() or completed.stdout.strip()
        raise SetupError(f"AWS CLI failed: {error[:1000]}")


def java_date_format_to_strftime(pattern: str) -> str:
    result = pattern
    for java_token, strftime_token in DATE_PATTERN_TOKENS:
        result = result.replace(java_token, strftime_token)
    return result


# --------------------------------------------------------------------------- #
# Step 1: user record
# --------------------------------------------------------------------------- #


def read_user(client: ApiClient, user_uuid: str) -> UserContext:
    record = payload_of(client.request("GET", f"/v2.0/users/{user_uuid}"))
    if not isinstance(record, dict):
        raise SetupError(f"No user record returned for {user_uuid}")

    homes = record.get("homeAccounts")
    if isinstance(homes, list):
        home = homes[0] if homes else {}
    else:
        home = homes or {}

    rate = home.get("rate") or {}
    schedule_raw = home.get("ratesSchedule")
    try:
        schedule = json.loads(schedule_raw) if schedule_raw else []
    except json.JSONDecodeError:
        schedule = []

    measurement_types = record.get("measurementTypes") or home.get("measurementTypes")
    if isinstance(measurement_types, list) and measurement_types:
        measurement_type = (
            "ELECTRIC" if "ELECTRIC" in measurement_types else measurement_types[0]
        )
    else:
        measurement_type = "ELECTRIC"

    context = UserContext(
        uuid=user_uuid,
        pilot_id=int(record["pilotId"]),
        notification_user_type=record.get("notificationUserType") or "OPT_OUT",
        measurement_type=measurement_type,
        locale=record.get("locale") or "en_US",
        timezone_name=home.get("timeZone") or "UTC",
        partner_user_id=record.get("partnerUserId") or "",
        current_plan_number=rate.get("planNumber"),
        current_rate_plan_id=rate.get("ratePlanId"),
        rates_schedule=schedule,
    )
    detail(f"pilot={context.pilot_id} userType={context.notification_user_type}")
    detail(
        f"current plan={context.current_plan_number} "
        f"ratePlanId={context.current_rate_plan_id} tz={context.timezone_name}"
    )
    detail(f"measurementType={context.measurement_type} locale={context.locale}")
    return context


# --------------------------------------------------------------------------- #
# Step 2: pilot ingestion contract
# --------------------------------------------------------------------------- #


def read_config_map(
    client: ApiClient, config_type: str, entity: str, entity_id: Any
) -> dict[str, str]:
    response = client.request(
        "GET", f"/v2.0/configs/{config_type}/{entity}/{entity_id}", tolerate=(404, 500)
    )
    if response is None:
        return {}
    items = payload_of(response) or []
    return {item["key"]: (item.get("val") or "") for item in items}


def read_ingestion_contract(client: ApiClient, pilot_id: int) -> IngestionContract:
    ingestion = read_config_map(client, "launchpad_ingestion_configs", "pilot", pilot_id)
    data_ingestion = read_config_map(client, "data_ingestion", "pilot", pilot_id)
    s3_pull = read_config_map(client, "s3_pull", "pilot", pilot_id)
    fields_raw = read_config_map(client, "user_creation_launchpad", "pilot", pilot_id)

    bucket = data_ingestion.get("s3BucketName") or s3_pull.get("s3DestinationBucket")
    if not bucket:
        raise SetupError(f"No ingestion bucket configured for pilot {pilot_id}")

    delimiter = ingestion.get("user_creation_parser_delimiter") or "|"
    delimiter = delimiter.replace("\\", "") or "|"

    positions: dict[str, int] = {}
    for value in fields_raw.values():
        try:
            spec = json.loads(value)
        except json.JSONDecodeError:
            continue
        name = spec.get("launchPadUserCreationField")
        position = spec.get("fieldPosition")
        if name is not None and position is not None:
            positions[name] = int(position)

    for required in ("CUSTOMER_ID", "RATE_PLAN_ID", "RATE_PLAN_EFFECTIVE_DATE"):
        if required not in positions:
            raise SetupError(
                f"user_creation_launchpad for pilot {pilot_id} has no {required} position"
            )

    contract = IngestionContract(
        bucket=bucket,
        delimiter=delimiter,
        enroll_prefix=s3_pull.get("s3EnrollFilePrefix") or "USERENROLL",
        date_format=ingestion.get("user_enrollment_date_format") or "yyyy-MM-dd",
        parser_timezone=ingestion.get("parser_time_zone") or "UTC",
        field_positions=positions,
    )
    detail(f"bucket={contract.bucket} delimiter={contract.delimiter!r}")
    detail(
        f"ratePlanId@{positions['RATE_PLAN_ID']} "
        f"effectiveDate@{positions['RATE_PLAN_EFFECTIVE_DATE']} "
        f"customerId@{positions['CUSTOMER_ID']}"
    )
    return contract


def customer_id_of(client: ApiClient, user: UserContext) -> str:
    """Derive customer_id from partnerUserId using the pilot's identifier format."""
    ingestion = read_config_map(
        client, "launchpad_ingestion_configs", "pilot", user.pilot_id
    )
    template = ingestion.get("user_identifier_field_format") or "${pilot_id}_${customer_id}"
    pattern = re.escape(template)
    for name in re.findall(r"\$\{(\w+)\}", template):
        pattern = pattern.replace(re.escape(f"${{{name}}}"), f"(?P<{name}>.+?)")
    match = re.fullmatch(pattern, user.partner_user_id)
    if match and "customer_id" in match.groupdict():
        return match.group("customer_id")
    # Fall back to the trailing segment, which is the customer id in every
    # observed format.
    if "_" in user.partner_user_id:
        return user.partner_user_id.rsplit("_", 1)[-1]
    raise SetupError(
        f"Could not derive customer_id from partnerUserId {user.partner_user_id!r}"
    )


# --------------------------------------------------------------------------- #
# Step 3: TOU rate plan selection
# --------------------------------------------------------------------------- #


def load_plan(
    client: ApiClient, pilot_id: int, plan_number: int, plan_name: str, now: int, month: int
) -> RatePlan:
    plan = RatePlan(plan_number=plan_number, plan_name=plan_name)
    response = client.request(
        "GET",
        f"/v3.0/rates/utilities/{pilot_id}/plans/{plan_number}/structure",
        tolerate=(404, 500),
    )
    if response is None:
        return plan
    rows = payload_of(response) or []
    for row in rows:
        if not row.get("touName"):
            continue
        if row.get("chargeType") != CONSUMPTION_BASED:
            continue
        if row.get("isHoliday"):
            continue
        low, high = row.get("validLow"), row.get("validHigh")
        if low is None or high is None or not (int(low) <= now < int(high)):
            continue
        for candidate in range(int(row["monthLow"]), int(row["monthHigh"]) + 1):
            plan.tou_months.add(candidate)
        plan.tou_names.add(str(row["touName"]))
        week_low, week_high = int(row["weekLow"]), int(row["weekHigh"])
        if week_high <= 5:
            plan.has_weekday_band = True
        if week_low >= 6:
            plan.has_weekend_band = True
    plan.current_month_is_tou = month in plan.tou_months
    return plan


def select_tou_plan(
    client: ApiClient, user: UserContext, min_tou_months: int, forced_plan: str | None
) -> tuple[list[RatePlan], dict[int, RatePlan]]:
    response = payload_of(
        client.request("GET", f"/v3.0/rates/configuration/utilityId/{user.pilot_id}")
    )
    plans = response or []
    now = int(time.time())
    month = datetime.now(zoneinfo.ZoneInfo(user.timezone_name)).month

    cache: dict[int, RatePlan] = {}
    candidates: list[RatePlan] = []
    for entry in plans:
        plan_number = int(entry["planNumber"])
        plan_name = str(entry.get("planName") or plan_number)
        loaded = load_plan(client, user.pilot_id, plan_number, plan_name, now, month)
        cache[plan_number] = loaded
        if not loaded.is_tou:
            continue
        if plan_number == user.current_plan_number:
            continue
        if not entry.get("residential", True):
            continue
        if len(loaded.tou_months) <= min_tou_months:
            continue
        candidates.append(loaded)

    if forced_plan:
        for plan in cache.values():
            if forced_plan in (plan.plan_name, str(plan.plan_number)):
                detail(f"using requested plan {plan.describe()}")
                return [plan], cache
        raise SetupError(f"Requested rate plan {forced_plan!r} not found for pilot {user.pilot_id}")

    if not candidates:
        raise SetupError(
            f"No eligible TOU rate plan for pilot {user.pilot_id}: need a residential "
            f"plan with more than {min_tou_months} currently-valid TOU months"
        )

    # Ordering is empirical. Some plan shapes leave parts of TouRateStructureDetail
    # null or select a disclaimer string the pilot never defined, and the render then
    # fails with no email and no drop reason. Simpler shapes are the safer default,
    # and main() walks this list until one actually produces an email.
    candidates.sort(
        key=lambda p: (
            p.has_split_week_bands,
            p.current_month_is_tou,
            -len(p.tou_names),
            len(p.tou_months),
        ),
        reverse=True,
    )
    for index, plan in enumerate(candidates[:5]):
        detail(f"{'selected' if index == 0 else 'fallback '}: {plan.describe()}")
    return candidates, cache


def warn_if_not_new_tou_user(
    user: UserContext, target: RatePlan, cache: dict[int, RatePlan]
) -> None:
    """TouRateStructureDataProvider#isNewTouUser drops the event if any prior plan was TOU."""
    prior_tou = []
    for entry in user.rates_schedule:
        try:
            plan_number = int((entry.get("metaData") or {}).get("planNumber"))
        except (TypeError, ValueError):
            continue
        if plan_number == target.plan_number:
            continue
        plan = cache.get(plan_number)
        if plan and plan.is_tou:
            prior_tou.append(plan_number)
    if prior_tou:
        detail(
            f"WARNING: prior plans {sorted(set(prior_tou))} are TOU, so isNewTouUser will "
            "be false and the event will drop as OLD_TOU_USER. Re-run with "
            "--reset-schedule to collapse the schedule to a single non-TOU plan first."
        )


# --------------------------------------------------------------------------- #
# Step 4: user-level configuration
# --------------------------------------------------------------------------- #


def post_user_config(
    client: ApiClient,
    user_uuid: str,
    config_type: str,
    entries: list[tuple[str, str, str, str]],
) -> None:
    """entries: (key, value, dataType, regex). Metadata is required for new keys."""
    body = [
        {
            "configType": config_type,
            "key": key,
            "val": value,
            "documentation": MANAGED_BY,
            "dataType": data_type,
            "regex": regex,
        }
        for key, value, data_type, regex in entries
    ]
    client.request(
        "POST", f"/v2.0/configs/{config_type}/user/{user_uuid}", body=body
    )


def configure_user(
    client: ApiClient, user: UserContext, sections: list[dict[str, Any]]
) -> None:
    post_user_config(
        client,
        user.uuid,
        f"event_subscriptions.{EVENT_NAME}",
        [("delivery_modes", json.dumps([DELIVERY_MODE]), "JSON", r"\[.*\]")],
    )
    detail(f"event_subscriptions.{EVENT_NAME}/delivery_modes = [\"{DELIVERY_MODE}\"]")

    section_key = f"{EVENT_NAME}.{user.notification_user_type}"
    post_user_config(
        client,
        user.uuid,
        "email_template_sections_config",
        [(section_key, json.dumps(sections), "JSON", r"\[.*\]")],
    )
    detail(
        f"email_template_sections_config/{section_key} = "
        + ", ".join(s["emailTemplateSection"] for s in sections)
    )


def ensure_subject_resource(client: ApiClient, user: UserContext, subject_text: str) -> None:
    """StaticSubjectProvider reads this exact id; many pilots only define `.default`."""
    existing = client.request(
        "GET",
        f"/2.1/stringResources/user/{user.uuid}/resource/{SUBJECT_RESOURCE_ID}",
        query={"locale": user.locale},
        tolerate=(404, 500),
    )
    resolved = (payload_of(existing) or {}) if existing else {}
    if resolved.get(SUBJECT_RESOURCE_ID):
        detail(f"subject already resolves: {resolved[SUBJECT_RESOURCE_ID]!r}")
        return
    client.request(
        "POST",
        f"/2.1/stringResources/{user.uuid}/resource/{SUBJECT_RESOURCE_ID}",
        body=[
            {
                "id": SUBJECT_RESOURCE_ID,
                "entityId": user.uuid,
                "locale": user.locale,
                "text": subject_text,
            }
        ],
    )
    detail(f"created subject string resource = {subject_text!r}")


def copy_footer_from_pilot(client: ApiClient, user: UserContext, source_pilot: int) -> None:
    source = read_config_map(client, "email_template", "pilot", source_pilot)
    entries = []
    for key in ("utility_name", "utility_address", "utility_customer_cs_email"):
        if key in source:
            entries.append((key, source[key], "TEXT", ".*"))
    if not entries:
        detail(f"pilot {source_pilot} has no footer identity keys; leaving footer as-is")
        return
    post_user_config(client, user.uuid, "email_template", entries)
    for key, value, _, _ in entries:
        detail(f"email_template/{key} = {value!r}")


# --------------------------------------------------------------------------- #
# Step 5: enrolment file
# --------------------------------------------------------------------------- #


def find_template_row(
    contract: IngestionContract, customer_id: str, scan_limit: int
) -> tuple[str, str]:
    """Return (row, source_key) for the newest enrolment row belonging to the user."""
    listing = run_aws_json(["s3api", "list-objects-v2", "--bucket", contract.bucket])
    objects = [
        item
        for item in listing.get("Contents", [])
        if item["Key"].startswith(f"{contract.enroll_prefix}_")
    ]
    objects.sort(key=lambda item: item["LastModified"], reverse=True)

    position = contract.field_positions["CUSTOMER_ID"]
    for item in objects[:scan_limit]:
        text = _read_s3_text(contract.bucket, item["Key"])
        for line in text.splitlines():
            if not line.strip():
                continue
            columns = line.split(contract.delimiter)
            if len(columns) > position and columns[position] == customer_id:
                return line, item["Key"]
    raise SetupError(
        f"No {contract.enroll_prefix} row found for customer_id {customer_id} in the "
        f"newest {scan_limit} files of {contract.bucket}. Pass --ue-template-file with a "
        "known-good enrolment row for this user."
    )


def _read_s3_text(bucket: str, key: str) -> str:
    completed = subprocess.run(
        ["aws", "s3", "cp", f"s3://{bucket}/{key}", "-"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout


def build_transition_row(
    contract: IngestionContract, template_row: str, plan: RatePlan, effective_date: str
) -> str:
    columns = template_row.rstrip("\n").split(contract.delimiter)
    plan_position = contract.field_positions["RATE_PLAN_ID"]
    date_position = contract.field_positions["RATE_PLAN_EFFECTIVE_DATE"]
    needed = max(plan_position, date_position)
    if len(columns) <= needed:
        raise SetupError(
            f"Template row has {len(columns)} fields but the pilot layout needs "
            f"at least {needed + 1}"
        )
    previous_plan = columns[plan_position]
    columns[plan_position] = plan.plan_name
    columns[date_position] = effective_date
    detail(f"rate_plan_id {previous_plan!r} -> {plan.plan_name!r}")
    detail(f"rate_plan_effective_date -> {effective_date}")
    return contract.delimiter.join(columns)


def upload_enrolment_file(
    contract: IngestionContract, row: str, output_dir: Path
) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    key = f"{contract.enroll_prefix}_D_{stamp[-9:]}_{stamp}_T3.txt"
    output_dir.mkdir(parents=True, exist_ok=True)
    local = output_dir / key
    local.write_text(row + "\n", encoding="utf-8")

    # Both the metadata and the content type matter: without utility_file_name the
    # object is never picked up, with no error and no marker.
    run_aws(
        [
            "s3api",
            "put-object",
            "--bucket",
            contract.bucket,
            "--key",
            key,
            "--body",
            str(local),
            "--content-type",
            "text/plain; charset=UTF-8",
            "--metadata",
            f"utility_file_name={key}",
        ]
    )
    detail(f"uploaded s3://{contract.bucket}/{key}")
    detail(f"local copy {local}")
    return key


def default_effective_date(contract: IngestionContract) -> str:
    """First day of the current month, which always maps to a started bill cycle."""
    now = datetime.now(zoneinfo.ZoneInfo(contract.parser_timezone))
    return now.replace(day=1).strftime(java_date_format_to_strftime(contract.date_format))


# --------------------------------------------------------------------------- #
# Step 6/7/8: transition, trigger, verification
# --------------------------------------------------------------------------- #


def read_schedule(client: ApiClient, user: UserContext, home_ordinal: int) -> list[dict[str, Any]]:
    # /meta returns the home map at the top level, with no payload wrapper.
    home = client.request("GET", f"/meta/users/{user.uuid}/homes/{home_ordinal}")
    raw = (home or {}).get("ratesSchedule")
    try:
        return json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return []


def schedule_has_plan(schedule: list[dict[str, Any]], plan_number: int) -> bool:
    for entry in schedule:
        if str((entry.get("metaData") or {}).get("planNumber")) == str(plan_number):
            return True
    return False


def wait_for_transition(
    client: ApiClient,
    user: UserContext,
    home_ordinal: int,
    plan_number: int,
    timeout: int,
    interval: int,
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        schedule = read_schedule(client, user, home_ordinal)
        if schedule_has_plan(schedule, plan_number):
            detail(f"transition applied: {json.dumps(schedule)}")
            return True
        time.sleep(interval)
    detail(f"transition not seen within {timeout}s")
    return False


def reset_schedule_to_non_tou(
    client: ApiClient, user: UserContext, home_ordinal: int, cache: dict[int, RatePlan]
) -> None:
    non_tou = [p for p in cache.values() if not p.is_tou]
    if not non_tou:
        raise SetupError(f"Pilot {user.pilot_id} has no non-TOU plan to reset onto")
    base = next(
        (p for p in non_tou if p.plan_number == user.current_plan_number), non_tou[0]
    )
    schedule = [
        {
            "rateType": "SQL",
            "startTime": 0,
            "endTime": 2147483647,
            "metaData": {"planNumber": str(base.plan_number)},
            "measurementType": user.measurement_type,
        }
    ]
    client.request(
        "POST",
        f"/meta/users/{user.uuid}/homes/{home_ordinal}",
        body={
            "ratesSchedule": json.dumps(schedule),
            "ratePlanId": base.plan_name,
            "plannumber": str(base.plan_number),
        },
    )
    detail(f"schedule reset to single non-TOU plan {base.plan_name} ({base.plan_number})")


def notification_status_path(user: UserContext, home_ordinal: int) -> str:
    # The measurement type segment is required; without it the response is always
    # empty and reads as "no email was ever sent".
    return (
        f"/notification/notificationStatus/{user.uuid}/{home_ordinal}/"
        f"{EVENT_NAME}/{user.measurement_type}/{DELIVERY_MODE}"
    )


def sent_count(client: ApiClient, user: UserContext, home_ordinal: int) -> int:
    response = client.request(
        "GET", notification_status_path(user, home_ordinal), tolerate=(404, 500)
    )
    if isinstance(response, dict):
        try:
            return int(response.get("sentCount") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def reset_sent_count(client: ApiClient, user: UserContext, home_ordinal: int) -> None:
    client.request(
        "POST",
        notification_status_path(user, home_ordinal),
        body={"sentAt": 0, "sentCount": 0},
        tolerate=(404, 500),
    )
    detail(f"sentCount reset to {sent_count(client, user, home_ordinal)}")


def resolve_queue_url(
    client: ApiClient, user: UserContext, region: str, override: str | None
) -> str:
    if override:
        return override
    s3_pull = read_config_map(client, "s3_pull", "pilot", user.pilot_id)
    probe_base = s3_pull.get("enrollFileQueue") or "resi-fl-S3-UserEnrollment"
    # SQSFactory appends -<env>. Identify env by which suffix resolves the pilot's
    # own enrolment queue, then reuse that env for the notification queue.
    for candidate in _env_candidates(client.base_url):
        if not _queue_exists(region, f"{probe_base}-{candidate}"):
            continue
        name = f"NotificationsProcessorEventPriority-{candidate}"
        if not _queue_exists(region, name):
            continue
        resolved = run_aws_json(["sqs", "get-queue-url", "--region", region, "--queue-name", name])
        detail(f"env={candidate} queue={name}")
        return str(resolved["QueueUrl"])
    raise SetupError(
        "Could not identify the environment's notification queue. Pass --queue-url "
        "explicitly (SQSFactory names it NotificationsProcessorEventPriority-<env>)."
    )


def _queue_exists(region: str, name: str) -> bool:
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
            "text",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0 and completed.stdout.strip() != ""


def _env_candidates(base_url: str) -> list[str]:
    host = urllib.parse.urlparse(base_url).hostname or ""
    stem = host.split(".")[0]
    parts = [p for p in stem.split("-") if p not in ("api", "server")]
    candidates: list[str] = []
    if len(parts) >= 2:
        candidates.append("-".join(reversed(parts)))
        candidates.append("-".join(parts))
    if parts:
        candidates.append(parts[-1])
    seen: set[str] = set()
    return [c for c in candidates if not (c in seen or seen.add(c))]


def publish_event(
    queue_url: str, user: UserContext, home_ordinal: int, plan_number: int, region: str
) -> None:
    message = {
        "userId": user.uuid,
        "homeOrdinal": home_ordinal,
        "metaData": {"EventName": EVENT_NAME, "PlanNumber": plan_number},
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
    detail(f"published MessageId={response.get('MessageId')}")


def poll_for_email(
    client: ApiClient, user: UserContext, home_ordinal: int, timeout: int, interval: int
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sent_count(client, user, home_ordinal) >= 1:
            return True
        time.sleep(interval)
    return False


def verify_rendered_email(client: ApiClient, user: UserContext, output_dir: Path) -> None:
    summary = payload_of(
        client.request("GET", f"/2.1/utility_notifications/users/{user.uuid}", query={"limit": 5})
    ) or {}
    notification = next(
        (
            item
            for item in (summary.get("notificationsList") or [])
            if item.get("notificationType") == EVENT_NAME
        ),
        None,
    )
    if not notification:
        detail("no TOU_ONBOARDING notification found in the user's summary")
        return

    notification_id = notification["notificationId"]
    detail(f"notificationId={notification_id}")
    detail(f"subject={notification.get('notificationTitle')!r}")
    detail(f"deliveredTo={notification.get('deliveryDestination')}")

    rendered = payload_of(
        client.request("GET", f"/2.1/utility_notifications/notifications/{notification_id}")
    ) or {}
    body = rendered.get("notificationBody") or ""
    if not body:
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    artifact = output_dir / f"{EVENT_NAME.lower()}_{notification_id}.html"
    artifact.write_text(body, encoding="utf-8")
    detail(f"rendered html saved to {artifact} ({len(body)} bytes)")

    if not notification.get("notificationTitle"):
        detail("WARNING: subject is empty")
    footer = re.findall(r'<span[^>]*display: block;">([^<]*)</span>', body)
    if footer:
        detail(f"footer identity={footer[0]!r}")


def report_rate_structure_image(client: ApiClient, user: UserContext, home_ordinal: int) -> None:
    """A blank eRates.rateStructureImageUrl silently drops the email; this reveals it."""
    now = int(time.time())
    response = client.request(
        "GET",
        f"/v2.0/dashboard/users/{user.uuid}/usage-chart-details",
        query={
            "measurement-type": user.measurement_type,
            "mode": "day",  # AggMode.fromString only accepts lowercase
            "start": now - 86400,
            "end": now,
            "timestamp-present": now - 86400,
        },
        tolerate=(400, 404, 500),
    )
    if response is None:
        detail("could not read usage-chart-details to confirm the rate structure image")
        return
    details = payload_of(response) or {}
    image_data = details.get("ratePlanImageData") or {}
    if image_data.get("rateImageMap"):
        detail(f"rate structure image present for plan {image_data.get('planName')}")
    else:
        detail(
            "WARNING: no ratePlanImageData. eRates.rateStructureImageUrl is likely blank "
            "for this plan, which drops the email as TOU_RATE_STRUCTURE_NOT_AVAILABLE. "
            "That column is only writable by direct SQL."
        )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise SetupError(
            f"{CONFIG_PATH} not found. Copy config.json.example to config.json and fill it in."
        )
    with open(CONFIG_PATH) as f:
        config = json.load(f)

    missing = [k for k in REQUIRED_CONFIG_KEYS if not config.get(k)]
    if missing:
        raise SetupError(f"Missing required config key(s) in {CONFIG_PATH}: {', '.join(missing)}")

    config["UUID"] = require_uuid(config["UUID"])
    if not str(config["AUTH_TOKEN"]).strip():
        raise SetupError("AUTH_TOKEN cannot be empty")

    config.setdefault("BASE_URL", DEFAULT_BASE_URL)
    config.setdefault("HOME_ORDINAL", DEFAULT_HOME_ORDINAL)
    config.setdefault("REGION", DEFAULT_REGION)
    config.setdefault("RATE_PLAN", None)
    config.setdefault("EFFECTIVE_DATE", None)
    config.setdefault("FOOTER_FROM_PILOT", None)
    config.setdefault("SUBJECT_TEXT", DEFAULT_SUBJECT_TEXT)
    config.setdefault("QUEUE_URL", None)
    config.setdefault("SCAN_LIMIT", DEFAULT_SCAN_LIMIT)
    config.setdefault("TRANSITION_TIMEOUT", DEFAULT_TRANSITION_TIMEOUT)
    config.setdefault("EMAIL_TIMEOUT", DEFAULT_EMAIL_TIMEOUT)
    config.setdefault("POLL_INTERVAL", DEFAULT_POLL_INTERVAL)
    config.setdefault("RESET_SCHEDULE", False)
    config.setdefault("MAX_PLAN_ATTEMPTS", DEFAULT_MAX_PLAN_ATTEMPTS)
    config.setdefault("SKIP_FILE", False)
    return config


def main() -> int:
    try:
        config = load_config()
    except SetupError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1

    uuid = config["UUID"]
    home_ordinal = int(config["HOME_ORDINAL"])
    region = config["REGION"]
    output_dir = Path(config.get("OUTPUT_DIR") or DEFAULT_OUTPUT_DIR)
    poll_interval = int(config["POLL_INTERVAL"])
    client = ApiClient(config["BASE_URL"], str(config["AUTH_TOKEN"]).strip(), DEFAULT_HTTP_TIMEOUT)

    try:
        log("1/8 Reading user record")
        user = read_user(client, uuid)

        log("2/8 Reading pilot ingestion contract")
        contract = read_ingestion_contract(client, user.pilot_id)

        log("3/8 Selecting a TOU rate plan")
        moderation = read_config_map(client, "email_moderation", "pilot", user.pilot_id)
        try:
            min_tou_months = int(moderation.get("min_tou_month_for_tou_onboarding_email") or 1)
        except ValueError:
            min_tou_months = 1
        detail(
            f"min_tou_month_for_tou_onboarding_email={min_tou_months} "
            "(event needs strictly more)"
        )
        candidates, plan_cache = select_tou_plan(
            client, user, min_tou_months, config["RATE_PLAN"]
        )

        log("4/8 Writing user-level configuration")
        configure_user(client, user, DEFAULT_SECTIONS)
        ensure_subject_resource(client, user, config["SUBJECT_TEXT"])
        if config["FOOTER_FROM_PILOT"]:
            copy_footer_from_pilot(client, user, int(config["FOOTER_FROM_PILOT"]))

        attempts = candidates[: max(1, int(config["MAX_PLAN_ATTEMPTS"]))]
        for index, plan in enumerate(attempts, start=1):
            log(f"5/8 Attempt {index}/{len(attempts)} with rate plan {plan.plan_name}")

            already_on_plan = schedule_has_plan(
                read_schedule(client, user, home_ordinal), plan.plan_number
            )
            if config["RESET_SCHEDULE"] or (index > 1 and not already_on_plan):
                reset_schedule_to_non_tou(client, user, home_ordinal, plan_cache)
                user = read_user(client, uuid)
                already_on_plan = False
            warn_if_not_new_tou_user(user, plan, plan_cache)

            if config["SKIP_FILE"]:
                detail("skipping enrolment file (SKIP_FILE)")
            elif already_on_plan:
                detail("user is already on this plan; skipping the enrolment file")
            else:
                customer_id = customer_id_of(client, user)
                template_row, source_key = find_template_row(
                    contract, customer_id, int(config["SCAN_LIMIT"])
                )
                detail(f"customer_id={customer_id} template={source_key}")
                effective_date = config["EFFECTIVE_DATE"] or default_effective_date(contract)
                row = build_transition_row(contract, template_row, plan, effective_date)
                upload_enrolment_file(contract, row, output_dir)

                log("6/8 Waiting for the rate transition")
                wait_for_transition(
                    client,
                    user,
                    home_ordinal,
                    plan.plan_number,
                    int(config["TRANSITION_TIMEOUT"]),
                    poll_interval,
                )

            log("7/8 Resetting sent count and ensuring the event fires")
            reset_sent_count(client, user, home_ordinal)
            if not poll_for_email(
                client, user, home_ordinal, poll_interval * 2, poll_interval
            ):
                queue_url = resolve_queue_url(client, user, region, config["QUEUE_URL"])
                publish_event(queue_url, user, home_ordinal, plan.plan_number, region)

            log("8/8 Verifying the email")
            if poll_for_email(
                client, user, home_ordinal, int(config["EMAIL_TIMEOUT"]), poll_interval
            ):
                detail("email generated")
                verify_rendered_email(client, user, output_dir)
                return 0

            detail(f"no email for plan {plan.plan_name}; diagnosing before the next plan")
            report_rate_structure_image(client, user, home_ordinal)

        detail(
            "Exhausted the candidate plans. Pin a known-good plan with RATE_PLAN, or "
            "check the notifications-processor and Emailer logs for this user."
        )
        return 1
    except SetupError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
