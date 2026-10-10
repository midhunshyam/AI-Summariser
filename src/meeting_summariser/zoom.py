"""Zoom webhook checks and transcript download."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

SIGNATURE_TOLERANCE_SECONDS = 300
ZOOM_OAUTH_URL = "https://zoom.us/oauth/token"
ZOOM_DOMAINS = ("zoom.us", "zoomgov.com")
HTTP_TIMEOUT = (5, 30)  # seconds to connect, seconds to wait for data
RETRY_STATUSES = (429, 500, 502, 503, 504)


class ZoomError(RuntimeError):
    """A Zoom request failed. Messages never include tokens or URLs."""


def sign(secret: str, message: str | bytes) -> str:
    """Hex HMAC-SHA256 of ``message``, keyed with the app's secret token."""
    data = message.encode("utf-8") if isinstance(message, str) else message
    return hmac.new(secret.encode("utf-8"), data, hashlib.sha256).hexdigest()


def verify_signature(
    secret: str,
    body: bytes | str,
    timestamp: str | None,
    signature: str | None,
    *,
    now: float | None = None,
    tolerance: int = SIGNATURE_TOLERANCE_SECONDS,
) -> bool:
    """Check the ``x-zm-signature`` header of a Zoom webhook request.

    Zoom signs ``v0:{x-zm-request-timestamp}:{raw body}`` with the secret token
    and sends ``v0=<hex digest>``. A timestamp more than ``tolerance`` seconds
    away from ``now`` is refused, which limits replays of captured requests.
    """
    if not timestamp or not (timestamp.isascii() and timestamp.isdigit()):
        log.warning("Webhook refused: missing or malformed timestamp")
        return False
    current = time.time() if now is None else now
    if abs(current - int(timestamp)) > tolerance:
        log.warning("Webhook refused: timestamp is outside the %d s window", tolerance)
        return False
    if not signature or not signature.startswith("v0="):
        log.warning("Webhook refused: signature is missing or lacks the v0= prefix")
        return False
    raw = body.encode("utf-8") if isinstance(body, str) else body
    expected = sign(secret, b"v0:" + timestamp.encode("ascii") + b":" + raw)
    if not hmac.compare_digest(expected.encode("ascii"), signature[3:].encode("utf-8")):
        log.warning("Webhook refused: signature does not match")
        return False
    return True


def url_validation_response(secret: str, plain_token: str) -> dict[str, str]:
    """Answer to Zoom's ``endpoint.url_validation`` challenge."""
    return {"plainToken": plain_token, "encryptedToken": sign(secret, plain_token)}


def transcript_download_urls(recording: Mapping[str, Any]) -> list[str]:
    """Download URLs of a recording's transcript files, in recording order.

    Files with ``file_type`` TRANSCRIPT are used. If there are none, any WebVTT
    file (closed captions, for example) is used instead.
    """
    files = [f for f in recording.get("recording_files") or [] if isinstance(f, Mapping)]
    chosen = [f for f in files if str(f.get("file_type", "")).upper() == "TRANSCRIPT"]
    if not chosen:
        chosen = [f for f in files if str(f.get("file_extension", "")).lower().lstrip(".") == "vtt"]
    chosen.sort(key=lambda f: str(f.get("recording_start") or ""))
    return [str(f["download_url"]) for f in chosen if f.get("download_url")]


def parse_start_time(value: str | None) -> datetime | None:
    """Parse an ISO 8601 time such as Zoom's ``start_time`` (UTC with a Z suffix).

    A time without an offset is taken to be UTC. Returns None if it cannot be parsed.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def make_session(retries: int = 3) -> requests.Session:
    """HTTP session that retries connection errors, 429 and 5xx with backoff."""
    retry = Retry(
        total=retries,
        backoff_factor=1.0,
        status_forcelist=RETRY_STATUSES,
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def get_oauth_token(
    client_id: str,
    client_secret: str,
    account_id: str,
    *,
    session: requests.Session | None = None,
) -> str:
    """Server-to-Server OAuth access token, for events that carry no download token."""
    session = session or make_session()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
    try:
        response = session.post(
            ZOOM_OAUTH_URL,
            headers={"Authorization": f"Basic {basic}"},
            params={"grant_type": "account_credentials", "account_id": account_id},
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise ZoomError(f"Zoom OAuth request failed ({type(exc).__name__})") from None
    if not response.ok:
        raise ZoomError(f"Zoom OAuth request failed with HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        data = None
    token = data.get("access_token") if isinstance(data, dict) else None
    if not token:
        raise ZoomError("Zoom OAuth response did not include an access token")
    return str(token)


def download_transcript(
    url: str,
    token: str,
    *,
    session: requests.Session | None = None,
) -> str:
    """Download one transcript file, sending the token in the Authorization header."""
    _check_zoom_url(url)
    session = session or make_session()
    try:
        response = session.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=HTTP_TIMEOUT
        )
    except requests.RequestException as exc:
        raise ZoomError(f"Transcript download failed ({type(exc).__name__})") from None
    if not response.ok:
        raise ZoomError(f"Transcript download failed with HTTP {response.status_code}")
    return response.content.decode("utf-8", errors="replace")


def _check_zoom_url(url: str) -> None:
    """Only send a token to an https URL on one of Zoom's domains."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    on_zoom = any(host == domain or host.endswith("." + domain) for domain in ZOOM_DOMAINS)
    if parts.scheme != "https" or not on_zoom:
        raise ZoomError("Refusing to send a Zoom token to a host outside Zoom's domains")
