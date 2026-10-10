from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import pytest
import requests

from fakes import FakeResponse, FakeSession
from meeting_summariser import zoom

SECRET = "zoom-webhook-secret"
BODY = b'{"event":"recording.transcript_completed","payload":{"object":{"uuid":"abc=="}}}'
NOW = 1_741_127_400


def zoom_signature(secret: str, timestamp: str, body: bytes) -> str:
    """Zoom's documented scheme, written out here independently of zoom.sign()."""
    message = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


# Signature verification


def test_valid_signature_is_accepted():
    ts = str(NOW)
    assert zoom.verify_signature(SECRET, BODY, ts, zoom_signature(SECRET, ts, BODY), now=NOW)


def test_body_may_be_given_as_text():
    ts = str(NOW)
    signature = zoom_signature(SECRET, ts, BODY)
    assert zoom.verify_signature(SECRET, BODY.decode(), ts, signature, now=NOW)


def test_signature_from_another_secret_is_refused():
    ts = str(NOW)
    signature = zoom_signature("some-other-secret", ts, BODY)
    assert not zoom.verify_signature(SECRET, BODY, ts, signature, now=NOW)


def test_changed_body_is_refused():
    ts = str(NOW)
    signature = zoom_signature(SECRET, ts, BODY)
    assert not zoom.verify_signature(SECRET, BODY + b" ", ts, signature, now=NOW)


def test_changed_timestamp_is_refused():
    signature = zoom_signature(SECRET, str(NOW), BODY)
    assert not zoom.verify_signature(SECRET, BODY, str(NOW + 1), signature, now=NOW)


@pytest.mark.parametrize("age", [301, 3600, -301])
def test_timestamp_outside_five_minutes_is_refused(age):
    ts = str(NOW - age)
    signature = zoom_signature(SECRET, ts, BODY)
    assert not zoom.verify_signature(SECRET, BODY, ts, signature, now=NOW)


@pytest.mark.parametrize("age", [0, 299, 300, -300])
def test_timestamp_inside_five_minutes_is_accepted(age):
    ts = str(NOW - age)
    signature = zoom_signature(SECRET, ts, BODY)
    assert zoom.verify_signature(SECRET, BODY, ts, signature, now=NOW)


def test_signature_without_v0_prefix_is_refused():
    ts = str(NOW)
    bare_digest = zoom_signature(SECRET, ts, BODY)[len("v0=") :]
    assert not zoom.verify_signature(SECRET, BODY, ts, bare_digest, now=NOW)
    assert not zoom.verify_signature(SECRET, BODY, ts, "v1=" + bare_digest, now=NOW)


@pytest.mark.parametrize("signature", [None, "", "v0=", "v0=not-hex", "v0=é"])
def test_missing_or_garbled_signature_is_refused(signature):
    assert not zoom.verify_signature(SECRET, BODY, str(NOW), signature, now=NOW)


@pytest.mark.parametrize("timestamp", [None, "", "abc", "12.5", "-5", "١٢٣"])
def test_malformed_timestamp_is_refused(timestamp):
    signature = zoom_signature(SECRET, str(NOW), BODY)
    assert not zoom.verify_signature(SECRET, BODY, timestamp, signature, now=NOW)


def test_uses_the_current_time_by_default(monkeypatch):
    monkeypatch.setattr(zoom.time, "time", lambda: NOW + 10)
    ts = str(NOW)
    assert zoom.verify_signature(SECRET, BODY, ts, zoom_signature(SECRET, ts, BODY))


# URL validation


def test_url_validation_response_is_hmac_of_the_plain_token():
    plain = "qgg8vlvZRS6UYooatFL8Aw"
    expected = hmac.new(SECRET.encode(), plain.encode(), hashlib.sha256).hexdigest()
    assert zoom.url_validation_response(SECRET, plain) == {
        "plainToken": plain,
        "encryptedToken": expected,
    }


# Choosing the transcript files


def _file(file_type, extension, url, start="2025-03-04T22:30:00Z"):
    return {
        "file_type": file_type,
        "file_extension": extension,
        "download_url": url,
        "recording_start": start,
    }


def test_transcript_files_are_chosen_in_recording_order():
    recording = {
        "recording_files": [
            _file("MP4", "MP4", "https://zoom.us/video"),
            _file("TRANSCRIPT", "VTT", "https://zoom.us/second", "2025-03-04T23:10:00Z"),
            _file("CC", "VTT", "https://zoom.us/captions"),
            _file("CHAT", "TXT", "https://zoom.us/chat"),
            _file("TRANSCRIPT", "VTT", "https://zoom.us/first", "2025-03-04T22:30:00Z"),
        ]
    }
    assert zoom.transcript_download_urls(recording) == [
        "https://zoom.us/first",
        "https://zoom.us/second",
    ]


def test_any_vtt_file_is_used_when_there_is_no_transcript_file():
    recording = {
        "recording_files": [
            _file("MP4", "MP4", "https://zoom.us/video"),
            _file("CC", ".vtt", "https://zoom.us/captions"),
        ]
    }
    assert zoom.transcript_download_urls(recording) == ["https://zoom.us/captions"]


def test_chat_and_video_files_are_not_transcripts():
    recording = {
        "recording_files": [
            _file("MP4", "MP4", "https://zoom.us/video"),
            _file("CHAT", "TXT", "https://zoom.us/chat"),
            "not a file",
        ]
    }
    assert zoom.transcript_download_urls(recording) == []
    assert zoom.transcript_download_urls({}) == []


# Start time


def test_parse_start_time():
    utc = zoom.parse_start_time("2025-03-04T22:30:00Z")
    assert utc == datetime(2025, 3, 4, 22, 30, tzinfo=timezone.utc)
    offset = zoom.parse_start_time("2025-03-05T09:30:00+11:00")
    assert offset == utc
    assert offset.utcoffset() == timedelta(hours=11)
    naive = zoom.parse_start_time("2025-03-04T22:30:00")
    assert naive == utc


@pytest.mark.parametrize("value", [None, "", "yesterday", "2025-13-40T99:00:00Z"])
def test_unparseable_start_time_is_none(value):
    assert zoom.parse_start_time(value) is None


# Downloads

DOWNLOAD_URL = "https://us02web.zoom.us/rec/webhook_download/transcript-file"


def test_download_sends_the_token_as_a_bearer_header_with_a_timeout():
    session = FakeSession(FakeResponse(200, b"WEBVTT\n"))
    text = zoom.download_transcript(DOWNLOAD_URL, "secret-token", session=session)
    assert text == "WEBVTT\n"
    call = session.calls[0]
    assert call.url == DOWNLOAD_URL
    assert call.headers == {"Authorization": "Bearer secret-token"}
    assert call.timeout == zoom.HTTP_TIMEOUT
    assert "secret-token" not in call.url


def test_download_decodes_utf8_whatever_the_headers_say():
    session = FakeSession(FakeResponse(200, "Zoë: Hallo, Grüße".encode()))
    assert zoom.download_transcript(DOWNLOAD_URL, "t", session=session) == "Zoë: Hallo, Grüße"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/rec/download/file",
        "http://zoom.us/rec/download/file",
        "https://zoom.us.example.com/rec/download/file",
        "https://notzoom.us/rec/download/file",
        "ftp://zoom.us/file",
    ],
)
def test_token_is_never_sent_outside_zoom(url):
    session = FakeSession()
    with pytest.raises(zoom.ZoomError):
        zoom.download_transcript(url, "secret-token", session=session)
    assert session.calls == []


def test_zoomgov_hosts_are_allowed():
    session = FakeSession(FakeResponse(200, b"WEBVTT\n"))
    zoom.download_transcript("https://zoomgov.com/rec/download/file", "t", session=session)
    assert len(session.calls) == 1


def test_failed_download_names_the_status_but_not_the_url_or_token():
    session = FakeSession(FakeResponse(404))
    with pytest.raises(zoom.ZoomError) as caught:
        zoom.download_transcript(DOWNLOAD_URL, "secret-token", session=session)
    message = str(caught.value)
    assert "404" in message
    assert "secret-token" not in message
    assert "zoom.us" not in message


def test_connection_errors_are_wrapped_without_their_message():
    error = requests.ConnectionError(f"failed for {DOWNLOAD_URL}?access_token=secret-token")
    session = FakeSession(error)
    with pytest.raises(zoom.ZoomError) as caught:
        zoom.download_transcript(DOWNLOAD_URL, "secret-token", session=session)
    assert "secret-token" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


# Server-to-Server OAuth


def test_oauth_token_request():
    session = FakeSession(FakeResponse(200, data={"access_token": "oauth-token"}))
    token = zoom.get_oauth_token("client-id", "client-secret", "account-id", session=session)
    assert token == "oauth-token"
    call = session.calls[0]
    assert call.method == "POST"
    assert call.url == zoom.ZOOM_OAUTH_URL
    expected = base64.b64encode(b"client-id:client-secret").decode()
    assert call.headers == {"Authorization": f"Basic {expected}"}
    assert call.params == {"grant_type": "account_credentials", "account_id": "account-id"}
    assert call.timeout == zoom.HTTP_TIMEOUT


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(401, data={"reason": "Invalid client_id or client_secret"}),
        FakeResponse(200, data={"token_type": "bearer"}),
        FakeResponse(200, data=["not", "an", "object"]),
        FakeResponse(200),
    ],
)
def test_oauth_failures_raise_zoom_error(response):
    session = FakeSession(response)
    with pytest.raises(zoom.ZoomError) as caught:
        zoom.get_oauth_token("client-id", "client-secret", "account-id", session=session)
    assert "client-secret" not in str(caught.value)


def test_oauth_connection_error_is_wrapped():
    session = FakeSession(requests.Timeout("timed out"))
    with pytest.raises(zoom.ZoomError, match="Timeout"):
        zoom.get_oauth_token("client-id", "client-secret", "account-id", session=session)


def test_session_retries_rate_limits_and_server_errors():
    session = zoom.make_session()
    retry = session.get_adapter("https://zoom.us/oauth/token").max_retries
    assert retry.total == 3
    assert retry.backoff_factor > 0
    assert {429, 500, 502, 503, 504} <= set(retry.status_forcelist)
    assert {"GET", "POST"} <= set(retry.allowed_methods)
