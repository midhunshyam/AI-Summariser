"""Test configuration: keep the developer's environment out of the tests."""

from __future__ import annotations

import pytest

from meeting_summariser import config

ENV_KEYS = (
    "SECRET_ID",
    "EMAIL_TO",
    "EMAIL_FROM",
    "EMAIL_GREETING",
    "TIMEZONE",
    "OPENAI_MODEL",
    "CHUNK_TOKENS",
    "WORKER_FUNCTION_NAME",
    "DEDUPE_TABLE",
    "LOG_LEVEL",
    *config.SECRET_KEYS,
)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch):
    """Keep the developer's environment out of the tests and reset the settings cache."""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()
