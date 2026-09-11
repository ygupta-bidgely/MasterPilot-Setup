#!/usr/bin/env python3
"""The single shared configuration for every Persona_setup script.

There is one `Persona_setup/config.json` for all the scripts rather than a
config file per folder. It holds the environment (`BASE_URL`, `AUTH_TOKEN`,
`PILOT_ID`, `REGION`), the users to act on, and optional per-script overrides.

Everything else is discovered from the pilot at run time (see
`pilot_context.py`), so pointing the whole suite at a brand-new pilot means
changing `PILOT_ID` and the user list - no code edits, nothing hardcoded.

Resolution order for any setting, first match wins:

  1. `scripts.<ScriptName>.<KEY>`  - per-script override
  2. `<KEY>`                        - top-level shared value
  3. the script's own default        - passed in by the caller

Users can be given in three ways, and a script takes whichever is present:

  * `scripts.<ScriptName>.UUID`   - a UUID just for that script
  * `UUID`                        - one UUID shared by every script
  * `USERS`                       - a list of {UUID, ...} entries, optionally
                                    tagged so a script can select its own
                                    (see `users_for`)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .api import SetupError, require_uuid

PERSONA_SETUP_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PERSONA_SETUP_DIR / "config.json"
EXAMPLE_PATH = PERSONA_SETUP_DIR / "config.json.example"

# Environment-level settings. These cannot be derived from the pilot because
# they identify *which* deployment the pilot lives in.
REQUIRED_KEYS = ("BASE_URL", "AUTH_TOKEN", "PILOT_ID")

DEFAULTS: dict[str, Any] = {
    "REGION": "us-west-2",
    "HOME_ORDINAL": 1,
    "HTTP_TIMEOUT": 60,
    "AGGREGATION_WAIT_SECONDS": 30,
    "STATUS_TIMEOUT": 180,
    "STATUS_INTERVAL": 10,
}

# Token may come from the environment instead of the file, so a live token
# never has to be written to disk.
TOKEN_ENV_VAR = "BIDGELY_TOKEN"


class Config:
    """Read-only view of the shared config, scoped to one script."""

    def __init__(self, raw: dict[str, Any], script: str | None = None) -> None:
        self._raw = raw
        self._script = script
        self._script_section = (raw.get("scripts") or {}).get(script or "", {}) or {}

    # ---------------- lookup ---------------- #

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._script_section:
            return self._script_section[key]
        if key in self._raw:
            return self._raw[key]
        if key in DEFAULTS:
            return DEFAULTS[key]
        return default

    def require(self, key: str) -> Any:
        value = self.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            where = f"scripts.{self._script}.{key}" if self._script else key
            raise SetupError(
                f"Missing required setting {key!r} in {CONFIG_PATH} "
                f"(set {where} or the top-level {key})"
            )
        return value

    def int_(self, key: str) -> int:
        try:
            return int(self.require(key))
        except (TypeError, ValueError) as exc:
            raise SetupError(f"{key} must be a number") from exc

    def for_script(self, script: str) -> Config:
        return Config(self._raw, script)

    # ---------------- common settings ---------------- #

    @property
    def base_url(self) -> str:
        return str(self.require("BASE_URL")).rstrip("/")

    @property
    def token(self) -> str:
        token = os.environ.get(TOKEN_ENV_VAR) or self.get("AUTH_TOKEN")
        if not token or not str(token).strip():
            raise SetupError(
                f"No auth token. Set AUTH_TOKEN in {CONFIG_PATH} or export "
                f"{TOKEN_ENV_VAR}."
            )
        token = str(token).strip()
        if token.startswith("{{"):
            raise SetupError(
                f"AUTH_TOKEN in {CONFIG_PATH} is still the placeholder "
                f"{token!r}. Fill it in or export {TOKEN_ENV_VAR}."
            )
        return token

    @property
    def pilot_id(self) -> int:
        return self.int_("PILOT_ID")

    @property
    def region(self) -> str:
        return str(self.get("REGION"))

    @property
    def home_ordinal(self) -> int:
        return int(self.get("HOME_ORDINAL"))

    @property
    def http_timeout(self) -> int:
        return int(self.get("HTTP_TIMEOUT"))

    @property
    def queue_url(self) -> str | None:
        value = self.get("QUEUE_URL")
        return str(value) if value else None

    # ---------------- users ---------------- #

    def uuid(self) -> str:
        """The single user this script acts on."""
        users = self.users()
        if not users:
            raise SetupError(
                f"No user configured. Set scripts.{self._script}.UUID, the "
                f"top-level UUID, or a USERS list in {CONFIG_PATH}."
            )
        if len(users) > 1:
            raise SetupError(
                f"{len(users)} users configured for {self._script}, but this "
                f"script runs one user at a time. Use the orchestrator, or "
                f"set a single scripts.{self._script}.UUID."
            )
        return users[0]["UUID"]

    def users(self) -> list[dict[str, Any]]:
        """Users this script should act on, most specific source first."""
        if self._script_section.get("UUID"):
            return [self._normalize_user(self._script_section)]
        if self._script_section.get("USERS"):
            return [self._normalize_user(u) for u in self._script_section["USERS"]]
        if self._raw.get("UUID"):
            return [self._normalize_user(self._raw)]
        return [self._normalize_user(u) for u in (self._raw.get("USERS") or [])]

    def users_for(self, *tags: str) -> list[dict[str, Any]]:
        """Users whose `scripts` tag list includes any of `tags`.

        Lets one `USERS` list drive the whole suite: each entry names the
        scripts it applies to, and each script picks out its own users.
        """
        selected = []
        for user in self.users():
            wanted = user.get("scripts")
            if not wanted or any(t in wanted for t in tags):
                selected.append(user)
        return selected

    @staticmethod
    def _normalize_user(entry: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(entry, dict) or not entry.get("UUID"):
            raise SetupError(f"User entry has no UUID: {entry!r}")
        normalized = dict(entry)
        normalized["UUID"] = require_uuid(entry["UUID"])
        return normalized

    # ---------------- raw access ---------------- #

    @property
    def raw(self) -> dict[str, Any]:
        return self._raw

    def script_names(self) -> list[str]:
        return sorted((self._raw.get("scripts") or {}).keys())


def load(script: str | None = None, path: Path | None = None) -> Config:
    """Load the shared config, optionally scoped to one script."""
    config_path = path or CONFIG_PATH
    if not config_path.exists():
        raise SetupError(
            f"{config_path} not found. Copy {EXAMPLE_PATH.name} to "
            f"{config_path.name} and fill it in."
        )
    try:
        with open(config_path) as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise SetupError(f"{config_path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise SetupError(f"{config_path} must contain a JSON object")

    config = Config(raw, script)
    # Fail fast on the environment settings that cannot be derived.
    for key in REQUIRED_KEYS:
        if key == "AUTH_TOKEN":
            continue  # may come from the environment instead
        config.require(key)
    config.token  # noqa: B018 - validates the token is present and not a placeholder
    return config
