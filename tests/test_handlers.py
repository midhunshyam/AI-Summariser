from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from fakes import transcript_event
from meeting_summariser import handlers
from meeting_summariser.pipeline import Job

SECRET = "zoom-webhook-secret"
WORKER = "meeting-summariser-worker"


def http_event(body, *, secret=SECRET, timestamp=None, encode_base64=False, headers=None):
    """An API Gateway HTTP API (payload 2.0) event, signed the way Zoom signs requests."""
    raw = body if isinstance(body, str) else json.dumps(body)
    ts = str(int(time.time())) if timestamp is None else str(timestamp)
    digest = hmac.new(secret.encode(), f"v0:{ts}:{raw}".encode(), hashlib.sha256).hexdigest()
    event = {
        "version": "2.0",
        "routeKey": "POST /zoom/webhook",
        "headers": headers
        or {
            "content-type": "application/json",
            "x-zm-request-timestamp": ts,
            "x-zm-signature": f"v0={digest}",
        },
        "body": raw,
        "isBase64Encoded": False,
    }
    if encode_base64:
        event["body"] = base64.b64encode(raw.encode()).decode()
        event["isBase64Encoded"] = True
    return event


def body_of(response):
    return json.loads(response["body"])


class FakeLambda:
    def __init__(self, status=202, error=None):
        self.status = status
        self.error = error
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"StatusCode": self.status}


@pytest.fixture
def webhook_env(monkeypatch):
    monkeypatch.setenv("ZOOM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("WORKER_FUNCTION_NAME", WORKER)


@pytest.fixture
def fake_lambda(monkeypatch):
    fake = FakeLambda()
    monkeypatch.setattr(handlers, "aws_client", lambda service: fake)
    return fake


@pytest.fixture
def no_pipeline(monkeypatch):
    """The webhook must never do the slow work itself."""

    def fail(*args, **kwargs):
        raise AssertionError("the webhook ran the pipeline")

    monkeypatch.setattr(handlers, "run", fail)


# Webhook routing


def test_url_validation_challenge_is_answered(webhook_env):
    event = http_event({"event": "endpoint.url_validation", "payload": {"plainToken": "abc123"}})
    response = handlers.webhook_handler(event)
    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"] == "application/json"
    expected = hmac.new(SECRET.encode(), b"abc123", hashlib.sha256).hexdigest()
    assert body_of(response) == {"plainToken": "abc123", "encryptedToken": expected}


def test_unsigned_url_validation_is_refused(webhook_env):
    """Answering it would give anyone an HMAC of a string they choose."""
    forged = "v0:1700000000:" + json.dumps({"event": "recording.transcript_completed"})
    event = http_event(
        {"event": "endpoint.url_validation", "payload": {"plainToken": forged}},
        secret="attacker-guess",
    )
    response = handlers.webhook_handler(event)
    assert response["statusCode"] == 401
    assert "encryptedToken" not in response["body"]


def test_url_validation_without_a_token_is_a_bad_request(webhook_env):
    for payload in ({}, {"plainToken": ""}, {"plainToken": 42}, "not an object"):
        event = http_event({"event": "endpoint.url_validation", "payload": payload})
        assert handlers.webhook_handler(event)["statusCode"] == 400


def test_transcript_event_is_queued_for_the_worker(webhook_env, fake_lambda, no_pipeline):
    response = handlers.webhook_handler(http_event(transcript_event()))
    assert response["statusCode"] == 200
    assert body_of(response) == {"status": "queued"}
    (call,) = fake_lambda.calls
    assert call["FunctionName"] == WORKER
    assert call["InvocationType"] == "Event"
    assert json.loads(call["Payload"]) == {
        "recording_uuid": "test-meeting-uuid==",
        "topic": "Weekly release sync",
        "start_time": "2025-03-04T22:30:00Z",
        "transcript_urls": ["https://zoom.us/rec/webhook_download/transcript-file"],
        "download_token": "download-token-123",
    }


def test_worker_payload_leaves_out_what_it_does_not_need(webhook_env, fake_lambda):
    handlers.webhook_handler(http_event(transcript_event()))
    payload = fake_lambda.calls[0]["Payload"].decode()
    assert "host@example.com" not in payload
    assert "not-a-real-passcode" not in payload
    assert "video-file" not in payload


def test_transcript_event_without_transcript_files_is_not_queued(webhook_env, fake_lambda):
    event = transcript_event(files=[{"file_type": "MP4", "download_url": "https://zoom.us/v"}])
    response = handlers.webhook_handler(http_event(event))
    assert response["statusCode"] == 200
    assert body_of(response) == {"status": "ignored"}
    assert fake_lambda.calls == []


@pytest.mark.parametrize(
    "fake", [FakeLambda(error=RuntimeError("throttled")), FakeLambda(status=500)]
)
def test_queue_failure_returns_an_error_so_zoom_retries(webhook_env, monkeypatch, fake):
    monkeypatch.setattr(handlers, "aws_client", lambda service: fake)
    response = handlers.webhook_handler(http_event(transcript_event()))
    assert response["statusCode"] == 500


def test_missing_worker_name_returns_an_error(monkeypatch, fake_lambda):
    monkeypatch.setenv("ZOOM_WEBHOOK_SECRET", SECRET)
    response = handlers.webhook_handler(http_event(transcript_event()))
    assert response["statusCode"] == 500
    assert fake_lambda.calls == []


def test_other_events_are_acknowledged_and_ignored(webhook_env, fake_lambda, no_pipeline):
    for name in ("meeting.started", "recording.completed", None):
        response = handlers.webhook_handler(http_event({"event": name, "payload": {}}))
        assert response["statusCode"] == 200
        assert body_of(response) == {"status": "ignored"}
    assert fake_lambda.calls == []


def test_wrong_secret_is_refused(webhook_env, fake_lambda):
    response = handlers.webhook_handler(http_event(transcript_event(), secret="wrong"))
    assert response["statusCode"] == 401
    assert fake_lambda.calls == []


def test_replayed_old_request_is_refused(webhook_env, fake_lambda):
    old = int(time.time()) - 600
    response = handlers.webhook_handler(http_event(transcript_event(), timestamp=old))
    assert response["statusCode"] == 401
    assert fake_lambda.calls == []


def test_missing_signature_headers_are_refused(webhook_env):
    event = http_event({"event": "meeting.started"}, headers={"content-type": "application/json"})
    assert handlers.webhook_handler(event)["statusCode"] == 401


def test_signed_body_that_is_not_json_is_a_bad_request(webhook_env):
    assert handlers.webhook_handler(http_event("not json"))["statusCode"] == 400
    assert handlers.webhook_handler(http_event("[1, 2]"))["statusCode"] == 400


def test_base64_encoded_body_is_decoded_before_checking(webhook_env):
    event = http_event(
        {"event": "endpoint.url_validation", "payload": {"plainToken": "abc"}},
        encode_base64=True,
    )
    assert handlers.webhook_handler(event)["statusCode"] == 200


def test_invalid_base64_body_is_a_bad_request(webhook_env):
    event = http_event({"event": "meeting.started"})
    event.update(body="%%% not base64 %%%", isBase64Encoded=True)
    assert handlers.webhook_handler(event)["statusCode"] == 400


def test_header_names_are_case_insensitive(webhook_env):
    event = http_event({"event": "endpoint.url_validation", "payload": {"plainToken": "abc"}})
    event["headers"] = {key.title(): value for key, value in event["headers"].items()}
    assert "X-Zm-Signature" in event["headers"]
    assert handlers.webhook_handler(event)["statusCode"] == 200


def test_missing_webhook_secret_is_a_server_error():
    response = handlers.webhook_handler(http_event({"event": "meeting.started"}))
    assert response["statusCode"] == 500


def test_logs_hold_no_tokens_bodies_or_personal_details(webhook_env, fake_lambda, caplog):
    caplog.set_level(logging.DEBUG)
    handlers.webhook_handler(http_event(transcript_event()))
    handlers.webhook_handler(http_event(transcript_event(), secret="wrong"))
    logged = caplog.text
    for private in ("download-token-123", SECRET, "host@example.com", "Weekly release sync"):
        assert private not in logged


# Worker


class FakeLedger:
    def __init__(self, claim_result=True):
        self.claim_result = claim_result
        self.claims = []
        self.done = []
        self.failed = []

    def claim(self, recording_uuid, lease_seconds):
        self.claims.append((recording_uuid, lease_seconds))
        return self.claim_result

    def mark_done(self, recording_uuid):
        self.done.append(recording_uuid)

    def mark_failed(self, recording_uuid):
        self.failed.append(recording_uuid)


JOB = Job.from_zoom_event(transcript_event())


@pytest.fixture
def ledger(monkeypatch):
    fake = FakeLedger()
    monkeypatch.setenv("DEDUPE_TABLE", "summariser-ledger")
    monkeypatch.setattr(handlers, "aws_client", lambda service: object())
    monkeypatch.setattr(handlers, "RecordingLedger", lambda table, client: fake)
    return fake


def test_worker_runs_the_pipeline_without_a_ledger(monkeypatch):
    runs = []
    monkeypatch.setattr(handlers, "run", lambda job, settings: runs.append(job) or "gmail-1")
    assert handlers.worker_handler(JOB.to_payload()) == {"status": "sent"}
    assert runs == [JOB]


def test_worker_claims_then_marks_done(monkeypatch, ledger):
    monkeypatch.setattr(handlers, "run", lambda job, settings: "gmail-1")
    context = SimpleNamespace(get_remaining_time_in_millis=lambda: 600_000)
    assert handlers.worker_handler(JOB.to_payload(), context) == {"status": "sent"}
    assert ledger.claims == [("test-meeting-uuid==", 600 + handlers.LEASE_MARGIN_SECONDS)]
    assert ledger.done == ["test-meeting-uuid=="]
    assert ledger.failed == []


def test_worker_skips_a_recording_that_is_already_claimed(monkeypatch, ledger):
    ledger.claim_result = False
    monkeypatch.setattr(handlers, "run", lambda job, settings: pytest.fail("ran twice"))
    assert handlers.worker_handler(JOB.to_payload()) == {"status": "duplicate"}
    assert ledger.done == []


def test_worker_releases_the_claim_and_reraises_on_failure(monkeypatch, ledger):
    def broken(job, settings):
        raise RuntimeError("OpenAI is down")

    monkeypatch.setattr(handlers, "run", broken)
    with pytest.raises(RuntimeError, match="OpenAI is down"):
        handlers.worker_handler(JOB.to_payload())
    assert ledger.failed == ["test-meeting-uuid=="]
    assert ledger.done == []


def test_worker_keeps_the_original_error_if_release_also_fails(monkeypatch, ledger):
    def broken(job, settings):
        raise RuntimeError("OpenAI is down")

    def release_fails(recording_uuid):
        raise ConnectionError("DynamoDB unreachable")

    monkeypatch.setattr(handlers, "run", broken)
    monkeypatch.setattr(ledger, "mark_failed", release_fails)
    with pytest.raises(RuntimeError, match="OpenAI is down"):
        handlers.worker_handler(JOB.to_payload())


def test_worker_without_lambda_context_uses_the_default_lease(monkeypatch, ledger):
    monkeypatch.setattr(handlers, "run", lambda job, settings: None)
    assert handlers.worker_handler(JOB.to_payload()) == {"status": "nothing to send"}
    assert ledger.claims[0][1] == handlers.DEFAULT_LEASE_SECONDS
    assert ledger.done == ["test-meeting-uuid=="]


@pytest.mark.parametrize(
    "payload",
    [{}, {"recording_uuid": "x"}, {"recording_uuid": "x", "transcript_urls": "not a list"}],
)
def test_worker_rejects_a_malformed_job(payload):
    with pytest.raises(ValueError, match=r"recording_uuid|transcript_urls"):
        handlers.worker_handler(payload)


def test_duplicate_deliveries_send_one_email(monkeypatch):
    """Webhook and worker together, with moto's DynamoDB holding the ledger."""
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "ap-southeast-2",
        "ZOOM_WEBHOOK_SECRET": SECRET,
        "WORKER_FUNCTION_NAME": WORKER,
        "DEDUPE_TABLE": "summariser-ledger",
    }.items():
        monkeypatch.setenv(key, value)

    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name="ap-southeast-2")
        dynamodb.create_table(
            TableName="summariser-ledger",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "recording_uuid", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "recording_uuid", "KeyType": "HASH"}],
        )
        queued = FakeLambda()
        clients = {"dynamodb": dynamodb, "lambda": queued}
        monkeypatch.setattr(handlers, "aws_client", clients.__getitem__)
        sent = []
        monkeypatch.setattr(handlers, "run", lambda job, settings: sent.append(job) or "gmail-1")

        # Zoom delivers the same event twice; both are acknowledged and queued.
        for _ in range(2):
            assert handlers.webhook_handler(http_event(transcript_event()))["statusCode"] == 200
        results = [handlers.worker_handler(json.loads(call["Payload"])) for call in queued.calls]

    assert results == [{"status": "sent"}, {"status": "duplicate"}]
    assert len(sent) == 1
