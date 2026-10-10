"""The work done for one recording: download, summarise, render and send."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import requests

from . import zoom
from .config import ConfigError, Settings, require
from .mailer import GmailSender
from .render import Meeting, render_email
from .summarise import Summariser, make_openai_client
from .transcript import Utterance, parse_vtt

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Job:
    """The parts of a ``recording.transcript_completed`` event the worker needs.

    The webhook passes this to the worker instead of the whole event, which
    also carries the host's e-mail address and the recording passcode.
    """

    recording_uuid: str
    topic: str
    start_time: str | None
    transcript_urls: tuple[str, ...]
    download_token: str | None = field(default=None, repr=False)

    @classmethod
    def from_zoom_event(cls, event: Mapping[str, Any]) -> Job:
        payload = _mapping(event.get("payload"))
        recording = _mapping(payload.get("object"))
        token = event.get("download_token") or payload.get("download_token")
        return cls(
            recording_uuid=str(recording.get("uuid") or ""),
            topic=str(recording.get("topic") or ""),
            start_time=_optional_text(recording.get("start_time")),
            transcript_urls=tuple(zoom.transcript_download_urls(recording)),
            download_token=_optional_text(token),
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Job:
        """Rebuild a job from the worker's invocation payload, checking its shape."""
        uuid = payload.get("recording_uuid")
        if not isinstance(uuid, str) or not uuid:
            raise ValueError("The job has no recording_uuid")
        urls = payload.get("transcript_urls")
        if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
            raise ValueError("The job's transcript_urls must be a list of strings")
        return cls(
            recording_uuid=uuid,
            topic=str(payload.get("topic") or ""),
            start_time=_optional_text(payload.get("start_time")),
            transcript_urls=tuple(urls),
            download_token=_optional_text(payload.get("download_token")),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "recording_uuid": self.recording_uuid,
            "topic": self.topic,
            "start_time": self.start_time,
            "transcript_urls": list(self.transcript_urls),
            "download_token": self.download_token,
        }


def run(
    job: Job,
    settings: Settings,
    *,
    session: requests.Session | None = None,
    summariser: Summariser | None = None,
    sender: GmailSender | None = None,
) -> str | None:
    """Download, summarise and e-mail one recording.

    Returns the Gmail message id, or None when there was nothing to send. The
    keyword arguments let tests replace the network clients.
    """
    if not job.transcript_urls:
        log.info("Recording %s has no transcript file; nothing to send", job.recording_uuid)
        return None

    # Check the settings before spending anything on downloads or model calls.
    recipients = require(settings.email_to, "EMAIL_TO")
    if sender is None:
        token_info = require(settings.gmail_token, "GMAIL_TOKEN_JSON")
        sender = GmailSender(token_info, sender=settings.email_from)
    if summariser is None:
        client = make_openai_client(require(settings.openai_api_key, "OPENAI_API_KEY"))
        summariser = Summariser(
            client, model=settings.openai_model, chunk_tokens=settings.chunk_tokens
        )

    session = session or zoom.make_session()
    utterances = _download(job, settings, session)
    if not utterances:
        log.warning("Transcript for %s is empty; nothing to send", job.recording_uuid)
        return None

    summary = summariser.summarise(utterances)
    meeting = Meeting(topic=job.topic, start=zoom.parse_start_time(job.start_time))
    email = render_email(summary, meeting, greeting=settings.email_greeting, tz=settings.tz)
    message_id = sender.send(to=recipients, subject=email.subject, text=email.text, html=email.html)
    log.info("Sent the summary of recording %s as message %s", job.recording_uuid, message_id)
    return message_id


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_text(value: Any) -> str | None:
    return str(value) if value else None


def _download(job: Job, settings: Settings, session: requests.Session) -> list[Utterance]:
    token = job.download_token or _oauth_token(settings, session)
    utterances: list[Utterance] = []
    for url in job.transcript_urls:
        utterances.extend(parse_vtt(zoom.download_transcript(url, token, session=session)))
    return utterances


def _oauth_token(settings: Settings, session: requests.Session) -> str:
    credentials = settings.zoom_oauth
    if credentials is None:
        raise ConfigError(
            "The event has no download token, and ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET "
            "and ZOOM_ACCOUNT_ID are not all configured"
        )
    log.info("The event has no download token; requesting a Server-to-Server OAuth token")
    return zoom.get_oauth_token(*credentials, session=session)
