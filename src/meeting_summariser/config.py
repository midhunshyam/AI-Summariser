"""Settings from environment variables and AWS Secrets Manager.

Plain settings (recipients, model, time zone) come from environment variables.
Secrets come from one Secrets Manager secret, a JSON object named by
``SECRET_ID``. When ``SECRET_ID`` is not set, the same secret keys are read from
the environment instead, which is how the local CLI and the tests run.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_CHUNK_TOKENS = 12_000
MIN_CHUNK_TOKENS = 500
DEFAULT_GREETING = "Hi all,"
DEFAULT_TIMEZONE = "UTC"

SECRET_KEYS = (
    "ZOOM_WEBHOOK_SECRET",
    "OPENAI_API_KEY",
    "GMAIL_TOKEN_JSON",
    "ZOOM_CLIENT_ID",
    "ZOOM_CLIENT_SECRET",
    "ZOOM_ACCOUNT_ID",
)

T = TypeVar("T")

_ADDRESS = re.compile(r"[^@\s<>,]+@[^@\s<>,]+\.[^@\s<>,]+")
_NAMED_ADDRESS = re.compile(r"(?P<name>[^<>]*)<(?P<address>[^<>]*)>")


class ConfigError(RuntimeError):
    """A setting is missing or has an invalid value."""


@dataclass(frozen=True)
class Settings:
    """Validated settings for the handlers and the CLI.

    Secret fields are left out of ``repr()`` so that a stray log line cannot
    print them.
    """

    zoom_webhook_secret: str | None = field(default=None, repr=False)
    openai_api_key: str | None = field(default=None, repr=False)
    gmail_token: Mapping[str, Any] | None = field(default=None, repr=False)
    zoom_client_id: str | None = field(default=None, repr=False)
    zoom_client_secret: str | None = field(default=None, repr=False)
    zoom_account_id: str | None = field(default=None, repr=False)
    email_to: tuple[str, ...] = ()
    email_from: str | None = None
    email_greeting: str = DEFAULT_GREETING
    timezone: str = DEFAULT_TIMEZONE
    openai_model: str = DEFAULT_MODEL
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS
    worker_function_name: str | None = None
    dedupe_table: str | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def zoom_oauth(self) -> tuple[str, str, str] | None:
        """Zoom Server-to-Server OAuth credentials, or None if any part is missing."""
        if self.zoom_client_id and self.zoom_client_secret and self.zoom_account_id:
            return (self.zoom_client_id, self.zoom_client_secret, self.zoom_account_id)
        return None


def require(value: T | None, name: str) -> T:
    """Return ``value``, or raise ConfigError naming the missing setting."""
    if not value:
        raise ConfigError(f"{name} is not configured")
    return value


def settings_from(environ: Mapping[str, str], secrets: Mapping[str, Any]) -> Settings:
    """Build and validate settings from environment variables and secret values."""
    return Settings(
        zoom_webhook_secret=_secret(secrets, "ZOOM_WEBHOOK_SECRET"),
        openai_api_key=_secret(secrets, "OPENAI_API_KEY"),
        gmail_token=_gmail_token(secrets.get("GMAIL_TOKEN_JSON")),
        zoom_client_id=_secret(secrets, "ZOOM_CLIENT_ID"),
        zoom_client_secret=_secret(secrets, "ZOOM_CLIENT_SECRET"),
        zoom_account_id=_secret(secrets, "ZOOM_ACCOUNT_ID"),
        email_to=_addresses(environ.get("EMAIL_TO", "")),
        email_from=_clean(environ.get("EMAIL_FROM")),
        email_greeting=_clean(environ.get("EMAIL_GREETING")) or DEFAULT_GREETING,
        timezone=_timezone(_clean(environ.get("TIMEZONE")) or DEFAULT_TIMEZONE),
        openai_model=_clean(environ.get("OPENAI_MODEL")) or DEFAULT_MODEL,
        chunk_tokens=_chunk_tokens(environ.get("CHUNK_TOKENS")),
        worker_function_name=_clean(environ.get("WORKER_FUNCTION_NAME")),
        dedupe_table=_clean(environ.get("DEDUPE_TABLE")),
    )


def fetch_secret(secret_id: str, client: Any = None) -> dict[str, Any]:
    """Read a JSON object secret from AWS Secrets Manager."""
    if client is None:
        import boto3  # provided by the Lambda runtime

        client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_id)
    try:
        values = json.loads(response["SecretString"])
    except (KeyError, TypeError, ValueError):
        values = None
    if not isinstance(values, dict):
        raise ConfigError("The secret must be a JSON object with one key per secret value")
    return values


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Settings for this process, loaded once and reused by a warm Lambda container."""
    environ = os.environ
    secret_id = (environ.get("SECRET_ID") or "").strip()
    if secret_id:
        secrets = fetch_secret(secret_id)
    else:
        secrets = {key: environ[key] for key in SECRET_KEYS if environ.get(key)}
    return settings_from(environ, secrets)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


def _secret(secrets: Mapping[str, Any], key: str) -> str | None:
    value = secrets.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string")
    return value.strip() or None


def _gmail_token(value: Any) -> dict[str, Any] | None:
    if value is None or value == "":
        return None
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    raise ConfigError("GMAIL_TOKEN_JSON must be the token JSON object, or a string holding it")


def _addresses(value: str) -> tuple[str, ...]:
    """Comma-separated ``a@example.com`` or ``Name <a@example.com>`` entries.

    Checked with a plain pattern rather than email.utils, whose parsing of
    malformed addresses differs between Python releases.
    """
    result = []
    for part in (piece.strip() for piece in value.split(",")):
        if not part:
            continue
        named = _NAMED_ADDRESS.fullmatch(part)
        address = named.group("address").strip() if named else part
        if not part.isprintable() or not _ADDRESS.fullmatch(address):
            raise ConfigError(f"EMAIL_TO contains an invalid address: {part!r}")
        result.append(part)
    return tuple(result)


def _timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (KeyError, ValueError):
        raise ConfigError(f"TIMEZONE {name!r} is not a known IANA time zone") from None
    return name


def _chunk_tokens(value: str | None) -> int:
    if value is None or not value.strip():
        return DEFAULT_CHUNK_TOKENS
    try:
        tokens = int(value)
    except ValueError:
        raise ConfigError(f"CHUNK_TOKENS must be a whole number, got {value!r}") from None
    if tokens < MIN_CHUNK_TOKENS:
        raise ConfigError(f"CHUNK_TOKENS must be at least {MIN_CHUNK_TOKENS}")
    return tokens
