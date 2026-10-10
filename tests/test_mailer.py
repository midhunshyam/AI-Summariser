from __future__ import annotations

import base64
from email import message_from_bytes
from email.policy import default as default_policy

import pytest

from meeting_summariser.mailer import GMAIL_SEND_SCOPE, SEND_RETRIES, GmailSender, build_message

TO = ["team@example.com", "Lead <lead@example.org>"]


class FakeGmail:
    """Mimics service.users().messages().send(...).execute(...)."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.retries: list[int] = []

    def users(self):
        return self

    def messages(self):
        return self

    def send(self, userId, body):  # Gmail's own parameter names
        self.sent.append({"userId": userId, "body": body})
        return self

    def execute(self, num_retries=0):
        self.retries.append(num_retries)
        return {"id": f"gmail-{len(self.sent)}"}


def test_message_is_multipart_alternative_with_text_first():
    message = build_message(
        to=TO, subject="Meeting summary: Sync", text="Plain body", html="<p>HTML body</p>"
    )
    assert message.get_content_type() == "multipart/alternative"
    parts = list(message.iter_parts())
    assert [p.get_content_type() for p in parts] == ["text/plain", "text/html"]
    assert parts[0].get_content().strip() == "Plain body"
    assert parts[1].get_content().strip() == "<p>HTML body</p>"
    assert message["To"] == "team@example.com, Lead <lead@example.org>"
    assert message["Subject"] == "Meeting summary: Sync"
    assert message["From"] is None


def test_from_header_is_set_when_configured():
    message = build_message(
        to=TO, subject="S", text="t", html="<p>h</p>", sender="Summaries <bot@example.com>"
    )
    assert message["From"] == "Summaries <bot@example.com>"


def test_non_ascii_text_survives():
    message = build_message(to=TO, subject="Zoë's sync", text="Grüße", html="<p>Grüße</p>")
    parsed = message_from_bytes(message.as_bytes(), policy=default_policy)
    assert parsed["Subject"] == "Zoë's sync"
    assert parsed.get_body(("plain",)).get_content().strip() == "Grüße"


def test_recipients_are_required():
    with pytest.raises(ValueError, match="recipient"):
        build_message(to=[], subject="S", text="t", html="h")


def test_sender_posts_the_raw_message_to_gmail():
    gmail = FakeGmail()
    sender = GmailSender({}, sender="Summaries <bot@example.com>", service=gmail)
    message_id = sender.send(to=TO, subject="Meeting summary: Sync", text="Plain", html="<p>H</p>")

    assert message_id == "gmail-1"
    assert gmail.retries == [SEND_RETRIES]
    call = gmail.sent[0]
    assert call["userId"] == "me"
    raw = base64.urlsafe_b64decode(call["body"]["raw"])
    parsed = message_from_bytes(raw, policy=default_policy)
    assert parsed["To"] == "team@example.com, Lead <lead@example.org>"
    assert parsed["From"] == "Summaries <bot@example.com>"
    assert parsed.get_body(("html",)).get_content().strip() == "<p>H</p>"


def test_credentials_are_built_from_the_token_with_only_the_send_scope(monkeypatch):
    import googleapiclient.discovery

    captured = {}

    def fake_build(api, version, credentials=None, cache_discovery=True):
        captured.update(api=api, version=version, credentials=credentials)
        return FakeGmail()

    monkeypatch.setattr(googleapiclient.discovery, "build", fake_build)
    token = {
        "token": "access-token",
        "refresh_token": "refresh-token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "client-id.apps.googleusercontent.com",
        "client_secret": "client-secret",
        "scopes": [GMAIL_SEND_SCOPE],
    }
    GmailSender(token).send(to=TO, subject="S", text="t", html="<p>h</p>")

    credentials = captured["credentials"]
    assert (captured["api"], captured["version"]) == ("gmail", "v1")
    assert list(credentials.scopes) == [GMAIL_SEND_SCOPE]
    assert credentials.refresh_token == "refresh-token"
    assert credentials.client_id == "client-id.apps.googleusercontent.com"


def test_token_without_refresh_details_is_rejected():
    sender = GmailSender({"token": "access-token-only"})
    with pytest.raises(ValueError, match="refresh_token"):
        sender.send(to=TO, subject="S", text="t", html="h")
