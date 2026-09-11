#!/usr/bin/env python3
"""Turn an existing user into the Pool Pump persona.

Driven by the shared `Persona_setup/config.json`. The script never writes
pilot-level configuration; every override it creates is scoped to the single
user configured. It:
  1. Reads the user record (the pilot is taken from the user).
  2. Reads the pilot's launchpad ingestion contract (file layout, bucket,
     delimiter, field positions).
  3. Writes `email_moderation.monthly_summary_email_variant = MS_STANDARD`.
  4. If the user isn't already on rate plan 1 (180), clones the user's
     newest USERENROLL row, sets the plan, and uploads it to S3.
  5. Waits for the rate transition to land.
  6. Reads (read-only) `disagg_preference.enable_pp` on the pilot and warns
     if it's not `true` - this script never writes pilot-level config.

This script does NOT set the Pool Pump signal itself: PP presence
(`poolsandsaunasoutput`) is disagg-detected data, not a config (see
persona_config_matrix.md §2, "Three mechanisms"). There is no API/file
contract to force it onto an arbitrary user. If you need a user with a real
Pool Pump signal, use a pre-ported Pool Pump persona UUID from the reference
sheet (created via cdg_user_setup, which clones a real production user that
already has the signal).

The user comes from USERS in the shared config. The token is never printed. AWS
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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (  # noqa: E402
    SetupError,
    detail,
    log,
    payload_of,
)
from _common import load as load_shared_config  # noqa: E402
from _common.config import CONFIG_PATH as SHARED_CONFIG_PATH  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPT_NAME = "PoolPump"

PLAN_NUMBER = 1  # 180 - cross-checked against persona_config_matrix.md and
                 # cdg_user_setup/sources.csv.example

DEFAULT_HOME_ORDINAL = 1
DEFAULT_HTTP_TIMEOUT = 60
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "output"
DEFAULT_SCAN_LIMIT = 40
DEFAULT_TRANSITION_TIMEOUT = 300
DEFAULT_POLL_INTERVAL = 10

MANAGED_BY = "Managed by setup_pool_pump_persona.py"
MS_VARIANT = "MS_STANDARD"
POOL_PUMP_SKIP_NOTE = (
    "Pool Pump disagg signal not set by this script - it's detected data, "
    "not a config. Use a pre-ported Pool Pump persona UUID from the "
    "reference sheet if you need a user with a real signal."
)

DATE_PATTERN_TOKENS = [("yyyy", "%Y"), ("MM", "%m"), ("dd", "%d")]


@dataclass(frozen=True)
class UserContext:
    uuid: str
    pilot_id: int
    notification_user_type: str
    timezone_name: str
    partner_user_id: str
    current_plan_number: int | None


@dataclass(frozen=True)
class IngestionContract:
    bucket: str
    delimiter: str
    enroll_prefix: str
    date_format: str
    field_positions: dict[str, int]


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


def warn(message: str) -> None:
    print(f"    WARNING: {message}", flush=True)


def require_uuid(value: str) -> str:
    if not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
        r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
        value,
    ):
        raise SetupError("UUID must be a valid UUID")
    return value.lower()


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

    context = UserContext(
        uuid=user_uuid,
        pilot_id=int(record["pilotId"]),
        notification_user_type=record.get("notificationUserType") or "OPT_OUT",
        timezone_name=home.get("timeZone") or "UTC",
        partner_user_id=record.get("partnerUserId") or "",
        current_plan_number=rate.get("planNumber"),
    )
    detail(f"pilot={context.pilot_id} userType={context.notification_user_type}")
    detail(f"current plan={context.current_plan_number} tz={context.timezone_name}")
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


def resolve_plan_name(client: ApiClient, pilot_id: int, plan_number: int) -> str:
    response = payload_of(
        client.request("GET", f"/v3.0/rates/configuration/utilityId/{pilot_id}")
    )
    for entry in response or []:
        if int(entry["planNumber"]) == plan_number:
            return str(entry.get("planName") or plan_number)
    raise SetupError(f"Rate plan number {plan_number} not found on pilot {pilot_id}")


# --------------------------------------------------------------------------- #
# Step 3: user-level configuration
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


def write_persona_configs(client: ApiClient, user: UserContext) -> None:
    post_user_config(
        client,
        user.uuid,
        "email_moderation",
        [("monthly_summary_email_variant", MS_VARIANT, "TEXT", ".*")],
    )
    detail(f"email_moderation/monthly_summary_email_variant = {MS_VARIANT!r}")
    detail("  (unverified key name - not seeded on this pilot; see persona_config_matrix.md)")


# --------------------------------------------------------------------------- #
# Step 4: enrolment file (rate plan)
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
        f"newest {scan_limit} files of {contract.bucket}."
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
    contract: IngestionContract, template_row: str, plan_name: str, effective_date: str
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
    columns[plan_position] = plan_name
    columns[date_position] = effective_date
    detail(f"rate_plan_id {previous_plan!r} -> {plan_name!r}")
    detail(f"rate_plan_effective_date -> {effective_date}")
    return contract.delimiter.join(columns)


def upload_enrolment_file(contract: IngestionContract, row: str, output_dir: Path) -> str:
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


def default_effective_date(contract: IngestionContract, timezone_name: str) -> str:
    """First day of the current month, which always maps to a started bill cycle."""
    now = datetime.now(zoneinfo.ZoneInfo(timezone_name))
    return now.replace(day=1).strftime(java_date_format_to_strftime(contract.date_format))


# --------------------------------------------------------------------------- #
# Step 5: rate transition wait
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


# --------------------------------------------------------------------------- #
# Step 6: prerequisite check (read-only)
# --------------------------------------------------------------------------- #


def check_pool_pump_prereq(client: ApiClient, pilot_id: int) -> None:
    disagg = read_config_map(client, "disagg_preference", "pilot", pilot_id)
    if (disagg.get("enable_pp") or "").lower() != "true":
        warn(
            f"disagg_preference.enable_pp is {disagg.get('enable_pp')!r} on pilot "
            f"{pilot_id} - Pool Pump disagg won't run pilot-wide until this is "
            "flipped to true (this script does not write pilot config)."
        )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


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
    }

    config.setdefault("EFFECTIVE_DATE", None)
    config.setdefault("SCAN_LIMIT", DEFAULT_SCAN_LIMIT)
    config.setdefault("TRANSITION_TIMEOUT", DEFAULT_TRANSITION_TIMEOUT)
    config.setdefault("POLL_INTERVAL", DEFAULT_POLL_INTERVAL)
    config.setdefault("OUTPUT_DIR", None)
    return config


def main() -> int:
    try:
        config = load_config()
    except SetupError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1

    uuid = config["UUID"]
    home_ordinal = int(config["HOME_ORDINAL"])
    output_dir = Path(config["OUTPUT_DIR"] or DEFAULT_OUTPUT_DIR)
    poll_interval = int(config["POLL_INTERVAL"])
    client = ApiClient(config["BASE_URL"], str(config["AUTH_TOKEN"]).strip(), DEFAULT_HTTP_TIMEOUT)

    try:
        log(f"Pool Pump persona (rate plan {PLAN_NUMBER})")

        log("1/6 Reading user record")
        user = read_user(client, uuid)

        log("2/6 Reading pilot ingestion contract")
        contract = read_ingestion_contract(client, user.pilot_id)

        log("3/6 Writing persona configs")
        write_persona_configs(client, user)

        if user.current_plan_number != PLAN_NUMBER:
            log("4/6 Building and uploading the enrolment file")
            plan_name = resolve_plan_name(client, user.pilot_id, PLAN_NUMBER)
            customer_id = customer_id_of(client, user)
            template_row, source_key = find_template_row(
                contract, customer_id, int(config["SCAN_LIMIT"])
            )
            detail(f"customer_id={customer_id} template={source_key}")
            effective_date = config["EFFECTIVE_DATE"] or default_effective_date(
                contract, user.timezone_name
            )
            row = build_transition_row(contract, template_row, plan_name, effective_date)
            upload_enrolment_file(contract, row, output_dir)

            log("5/6 Waiting for the rate transition")
            wait_for_transition(
                client,
                user,
                home_ordinal,
                PLAN_NUMBER,
                int(config["TRANSITION_TIMEOUT"]),
                poll_interval,
            )
        else:
            detail("already on the target plan; skipping the enrolment file")

        log("6/6 Prerequisite check")
        check_pool_pump_prereq(client, user.pilot_id)
        detail(f"NOT SET: {POOL_PUMP_SKIP_NOTE}")

        log("Done")
        detail(f"plan={PLAN_NUMBER} msVariant={MS_VARIANT}")
        return 0
    except SetupError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
