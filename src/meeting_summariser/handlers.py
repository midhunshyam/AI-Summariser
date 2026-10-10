"""AWS Lambda entry points.

``webhook_handler`` sits behind the public HTTP endpoint. It checks Zoom's
signature, answers the URL-validation challenge and hands transcript events to
the worker with an asynchronous invocation, so Zoom gets its 200 response
without waiting for the summary. ``worker_handler`` does the slow part.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from collections.abc import Mapping
from functools import cache
from typing import Any

from . import zoom
from .config import get_settings, require
from .dedupe import RecordingLedger
from .pipeline import Job, run

log = logging.getLogger(__name__)

URL_VALIDATION = "endpoint.url_validation"
TRANSCRIPT_COMPLETED = "recording.transcript_completed"
DEFAULT_LEASE_SECONDS = 900.0
LEASE_MARGIN_SECONDS = 5.0


def _configure_logging() -> None:
    try:
        logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    except ValueError:
        logging.getLogger().setLevel(logging.INFO)
    # The Google client logs every request URL at INFO.
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)


_configure_logging()


@cache
def aws_client(service: str) -> Any:
    """One boto3 client per service, reused while the Lambda container stays warm."""
    import boto3  # provided by the Lambda runtime

    return boto3.client(service)


def webhook_handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """Handle one Zoom webhook request (API Gateway HTTP API, payload format 2.0)."""
    raw = _raw_body(event)
    if raw is None:
        return _respond(400, {"error": "Body is not valid base64"})
    headers = {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()}

    secret = get_settings().zoom_webhook_secret
    if not secret:
        log.error("ZOOM_WEBHOOK_SECRET is not configured")
        return _respond(500, {"error": "Webhook is not configured"})

    # The URL-validation challenge is checked too. Answering an unsigned
    # challenge would hand anyone an HMAC of text they choose, which is all it
    # takes to forge a signed event.
    timestamp = headers.get("x-zm-request-timestamp")
    if not zoom.verify_signature(secret, raw, timestamp, headers.get("x-zm-signature")):
        return _respond(401, {"error": "Invalid signature"})

    try:
        data = json.loads(raw)
    except ValueError:
        return _respond(400, {"error": "Body is not valid JSON"})
    if not isinstance(data, dict):
        return _respond(400, {"error": "Body is not a JSON object"})

    event_type = data.get("event")
    if event_type == URL_VALIDATION:
        payload = data.get("payload")
        plain_token = payload.get("plainToken") if isinstance(payload, dict) else None
        if not isinstance(plain_token, str) or not plain_token:
            return _respond(400, {"error": "Missing plainToken"})
        return _respond(200, zoom.url_validation_response(secret, plain_token))

    if event_type == TRANSCRIPT_COMPLETED:
        return _queue(Job.from_zoom_event(data))

    log.info("Ignoring Zoom event %s", event_type)
    return _respond(200, {"status": "ignored"})


def worker_handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """Summarise and e-mail one recording. Invoked asynchronously by the webhook."""
    job = Job.from_payload(event)
    settings = get_settings()
    ledger = None
    if settings.dedupe_table:
        ledger = RecordingLedger(settings.dedupe_table, aws_client("dynamodb"))
        if not ledger.claim(job.recording_uuid, _lease_seconds(context)):
            log.info("Recording %s is already done or in progress; skipping", job.recording_uuid)
            return {"status": "duplicate"}
    try:
        message_id = run(job, settings)
    except Exception:
        if ledger:
            try:
                ledger.mark_failed(job.recording_uuid)
            except Exception:
                log.exception("Could not release the claim on recording %s", job.recording_uuid)
        raise  # Lambda retries failed asynchronous invocations
    if ledger:
        ledger.mark_done(job.recording_uuid)
    return {"status": "sent" if message_id else "nothing to send"}


def _queue(job: Job) -> dict[str, Any]:
    if not job.recording_uuid or not job.transcript_urls:
        log.warning("Transcript event without a recording UUID or transcript file; ignored")
        return _respond(200, {"status": "ignored"})
    try:
        function_name = require(get_settings().worker_function_name, "WORKER_FUNCTION_NAME")
        response = aws_client("lambda").invoke(
            FunctionName=function_name,
            InvocationType="Event",
            Payload=json.dumps(job.to_payload()).encode("utf-8"),
        )
        if response.get("StatusCode") != 202:
            raise RuntimeError(f"Lambda answered the invocation with {response.get('StatusCode')}")
    except Exception:
        # An error status makes Zoom deliver the event again later.
        log.exception("Could not hand recording %s to the worker", job.recording_uuid)
        return _respond(500, {"error": "Could not queue the event"})
    log.info("Queued recording %s for the worker", job.recording_uuid)
    return _respond(200, {"status": "queued"})


def _raw_body(event: Mapping[str, Any]) -> bytes | None:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            return base64.b64decode(body, validate=True)
        except (binascii.Error, ValueError):
            return None
    return body.encode("utf-8") if isinstance(body, str) else bytes(body)


def _lease_seconds(context: Any) -> float:
    """How long a claim lasts: the time this invocation has left, plus a small margin."""
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    if callable(remaining):
        return remaining() / 1000 + LEASE_MARGIN_SECONDS
    return DEFAULT_LEASE_SECONDS


def _respond(status: int, body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
