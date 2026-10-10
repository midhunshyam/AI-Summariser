from __future__ import annotations

import json

import pytest

from meeting_summariser import config
from meeting_summariser.config import ConfigError, Settings, require, settings_from

GMAIL_TOKEN = {"refresh_token": "r", "client_id": "c", "client_secret": "s"}


def test_defaults():
    settings = settings_from({}, {})
    assert settings == Settings()
    assert settings.openai_model == "gpt-4o-mini"
    assert settings.chunk_tokens == 12_000
    assert settings.timezone == "UTC"
    assert settings.email_greeting == "Hi all,"
    assert settings.email_to == ()
    assert settings.zoom_oauth is None


def test_environment_and_secret_values_are_read():
    environ = {
        "EMAIL_TO": "team@example.com, Lead <lead@example.org>",
        "EMAIL_FROM": "Summaries <bot@example.com>",
        "EMAIL_GREETING": "Hello team,",
        "TIMEZONE": "Australia/Sydney",
        "OPENAI_MODEL": "another-model",
        "CHUNK_TOKENS": "8000",
        "WORKER_FUNCTION_NAME": "worker",
        "DEDUPE_TABLE": "ledger",
    }
    secrets = {
        "ZOOM_WEBHOOK_SECRET": "webhook-secret\n",
        "OPENAI_API_KEY": "sk-test",
        "GMAIL_TOKEN_JSON": json.dumps(GMAIL_TOKEN),
        "ZOOM_CLIENT_ID": "id",
        "ZOOM_CLIENT_SECRET": "secret",
        "ZOOM_ACCOUNT_ID": "account",
    }
    settings = settings_from(environ, secrets)
    assert settings.email_to == ("team@example.com", "Lead <lead@example.org>")
    assert settings.email_from == "Summaries <bot@example.com>"
    assert settings.email_greeting == "Hello team,"
    assert settings.tz.key == "Australia/Sydney"
    assert settings.openai_model == "another-model"
    assert settings.chunk_tokens == 8000
    assert settings.worker_function_name == "worker"
    assert settings.dedupe_table == "ledger"
    assert settings.zoom_webhook_secret == "webhook-secret"  # stray newline from pasting
    assert settings.gmail_token == GMAIL_TOKEN
    assert settings.zoom_oauth == ("id", "secret", "account")


def test_gmail_token_may_be_a_nested_object():
    assert settings_from({}, {"GMAIL_TOKEN_JSON": GMAIL_TOKEN}).gmail_token == GMAIL_TOKEN


@pytest.mark.parametrize("value", ["{not json", '"a string"', "[1, 2]", 42])
def test_bad_gmail_token_is_rejected(value):
    with pytest.raises(ConfigError, match="GMAIL_TOKEN_JSON"):
        settings_from({}, {"GMAIL_TOKEN_JSON": value})


def test_secret_values_must_be_strings():
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        settings_from({}, {"OPENAI_API_KEY": 123})


def test_blank_values_count_as_unset():
    settings = settings_from(
        {"EMAIL_FROM": "  ", "DEDUPE_TABLE": "", "OPENAI_MODEL": " "}, {"OPENAI_API_KEY": ""}
    )
    assert settings.email_from is None
    assert settings.dedupe_table is None
    assert settings.openai_model == "gpt-4o-mini"
    assert settings.openai_api_key is None


@pytest.mark.parametrize(
    "value",
    [
        "not-an-address",
        "two words@example.com",
        "team@example.com, broken@",
        "Lead <not-an-address>",
        "Lead\r\nBcc: other@example.com <lead@example.org>",
        "team@example.com <lead@example.org",
    ],
)
def test_invalid_recipient_is_rejected(value):
    with pytest.raises(ConfigError, match="EMAIL_TO"):
        settings_from({"EMAIL_TO": value}, {})


@pytest.mark.parametrize("value", ["Mars/Olympus_Mons", "../../etc/passwd"])
def test_unknown_time_zone_is_rejected(value):
    with pytest.raises(ConfigError, match="TIMEZONE"):
        settings_from({"TIMEZONE": value}, {})


@pytest.mark.parametrize("value", ["lots", "12.5", "100"])
def test_bad_chunk_size_is_rejected(value):
    with pytest.raises(ConfigError, match="CHUNK_TOKENS"):
        settings_from({"CHUNK_TOKENS": value}, {})


def test_recipient_list_ignores_empty_entries():
    assert settings_from({"EMAIL_TO": " team@example.com, ,"}, {}).email_to == ("team@example.com",)


def test_secrets_do_not_appear_in_repr():
    settings = settings_from(
        {"EMAIL_TO": "team@example.com"},
        {
            "ZOOM_WEBHOOK_SECRET": "webhook-secret-value",
            "OPENAI_API_KEY": "sk-secret-value",
            "GMAIL_TOKEN_JSON": json.dumps({**GMAIL_TOKEN, "refresh_token": "refresh-value"}),
            "ZOOM_CLIENT_SECRET": "client-secret-value",
        },
    )
    text = repr(settings)
    assert "team@example.com" in text
    for secret in (
        "webhook-secret-value",
        "sk-secret-value",
        "refresh-value",
        "client-secret-value",
    ):
        assert secret not in text


def test_zoom_oauth_needs_all_three_values():
    assert settings_from({}, {"ZOOM_CLIENT_ID": "id", "ZOOM_CLIENT_SECRET": "s"}).zoom_oauth is None


def test_require():
    assert require("value", "NAME") == "value"
    with pytest.raises(ConfigError, match="NAME is not configured"):
        require(None, "NAME")
    with pytest.raises(ConfigError):
        require((), "NAME")


class FakeSecretsManager:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def get_secret_value(self, SecretId):  # boto3's parameter name
        self.requests.append(SecretId)
        return self.response


def test_fetch_secret_parses_the_json_object():
    client = FakeSecretsManager({"SecretString": json.dumps({"OPENAI_API_KEY": "sk-test"})})
    assert config.fetch_secret("summariser/prod", client) == {"OPENAI_API_KEY": "sk-test"}
    assert client.requests == ["summariser/prod"]


@pytest.mark.parametrize(
    "response",
    [{"SecretString": "[1, 2]"}, {"SecretString": "plain text"}, {"SecretBinary": b"\x00"}],
)
def test_fetch_secret_rejects_anything_but_a_json_object(response):
    with pytest.raises(ConfigError):
        config.fetch_secret("summariser/prod", FakeSecretsManager(response))


def test_get_settings_reads_secrets_manager_once(monkeypatch):
    calls = []

    def fake_fetch(secret_id):
        calls.append(secret_id)
        return {"ZOOM_WEBHOOK_SECRET": "from-secrets-manager"}

    monkeypatch.setattr(config, "fetch_secret", fake_fetch)
    monkeypatch.setenv("SECRET_ID", "summariser/prod")
    monkeypatch.setenv("ZOOM_WEBHOOK_SECRET", "ignored-when-SECRET_ID-is-set")
    first = config.get_settings()
    second = config.get_settings()
    assert first is second
    assert first.zoom_webhook_secret == "from-secrets-manager"
    assert calls == ["summariser/prod"]


def test_without_secret_id_secrets_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    monkeypatch.setenv("EMAIL_TO", "team@example.com")
    settings = config.get_settings()
    assert settings.openai_api_key == "sk-from-env"
    assert settings.email_to == ("team@example.com",)
