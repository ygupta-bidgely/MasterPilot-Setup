#!/usr/bin/env python3
"""Shared HTTP/AWS plumbing for the Persona_setup scripts."""

from __future__ import annotations

import json
import re
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class SetupError(RuntimeError):
    """A safe, user-readable setup failure."""


class ApiClient:
    """Minimal bearer-token JSON client for the api-server."""

    def __init__(self, base_url: str, token: str, timeout: int = 60) -> None:
        if not base_url:
            raise SetupError("BASE_URL is required")
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
        """Make a request.

        `expected` lists statuses that are returned normally. `tolerate` lists
        statuses that mean "nothing there" - those return None instead of
        raising, which is how callers probe for optional configuration.
        """
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
            if exc.code in expected:
                status = exc.code
            else:
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


def payload_of(response: Any) -> Any:
    if isinstance(response, dict) and response.get("error"):
        raise SetupError(f"API returned an error: {response['error']}")
    if isinstance(response, dict) and "payload" in response:
        return response["payload"]
    return response


def require_uuid(value: str) -> str:
    if not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
        r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
        str(value),
    ):
        raise SetupError(f"Not a valid UUID: {value!r}")
    return str(value).lower()


def log(step: str) -> None:
    print(f"\n==> {step}", flush=True)


def detail(message: str) -> None:
    print(f"    {message}", flush=True)


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


# ---------------- user lookup ---------------- #


def fetch_user(client: ApiClient, user_id: str) -> dict[str, Any]:
    payload = payload_of(client.request("GET", f"/v2.0/users/{user_id}"))
    if not isinstance(payload, dict):
        raise SetupError(f"User {user_id} returned no user object")
    return payload


def user_pilot_id(user: dict[str, Any]) -> int | None:
    for key in ("pilotId", "pilot_id"):
        if user.get(key) is not None:
            try:
                return int(user[key])
            except (TypeError, ValueError):
                return None
    return None


def require_pilot(user: dict[str, Any], pilot_id: int) -> None:
    """Fail before any mutation if the user is not on the expected pilot."""
    actual = user_pilot_id(user)
    if actual is not None and actual != int(pilot_id):
        raise SetupError(
            f"User is on pilot {actual}, not {pilot_id}. Refusing to continue."
        )


# ---------------- user-scoped configuration ---------------- #


def write_entity_config(
    client: ApiClient,
    user_id: str,
    config_type: str,
    values: list[tuple[str, str, str]],
    *,
    managed_by: str,
    tags: list[str] | None = None,
) -> None:
    """Write user-scoped config keys. Every override is scoped to this one
    user - these scripts never write pilot-level configuration."""
    config_kvs = [
        {
            "configKey": key,
            "configVal": value,
            "configDocumentation": f"User-only test configuration ({managed_by})",
            "valueDocumentation": f"Managed by {managed_by}",
            "configRegex": ".*" if data_type == "TEXT" else "[0-9]+",
            "configDataType": data_type,
            "configTags": tags or ["Email", "ProductQA"],
        }
        for key, value, data_type in values
    ]
    client.request(
        "POST",
        f"/entities/{user_id}/configs",
        body={"configType": config_type, "configKVs": config_kvs},
        expected=(200, 201),
    )
