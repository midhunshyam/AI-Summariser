"""Send the summary e-mail through the Gmail API."""

from __future__ import annotations

import base64
import logging
from collections.abc import Mapping, Sequence
from email.message import EmailMessage
from typing import Any

log = logging.getLogger(__name__)

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
SEND_RETRIES = 2


def build_message(
    *,
    to: Sequence[str],
    subject: str,
    text: str,
    html: str,
    sender: str | None = None,
) -> EmailMessage:
    """A multipart/alternative message: plain text first, HTML second."""
    if not to:
        raise ValueError("At least one recipient is required")
    message = EmailMessage()
    message["To"] = ", ".join(to)
    if sender:
        message["From"] = sender
    message["Subject"] = subject
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message


class GmailSender:
    """Sends messages as the Google account that authorised the token.

    ``token_info`` is the authorised-user JSON written by
    ``scripts/create_gmail_token.py``. The access token is refreshed with the
    refresh token when needed. ``service`` can be passed in by tests.
    """

    def __init__(
        self,
        token_info: Mapping[str, Any],
        *,
        sender: str | None = None,
        service: Any = None,
    ) -> None:
        self._token_info = dict(token_info)
        self._sender = sender
        self._service = service

    def send(self, *, to: Sequence[str], subject: str, text: str, html: str) -> str:
        """Send one message and return its Gmail message id."""
        message = build_message(to=to, subject=subject, text=text, html=html, sender=self._sender)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        request = self._gmail().users().messages().send(userId="me", body={"raw": raw})
        result = request.execute(num_retries=SEND_RETRIES)
        log.info("Summary e-mail sent to %d recipient(s)", len(to))
        return str(result.get("id", ""))

    def _gmail(self) -> Any:
        if self._service is None:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            credentials = Credentials.from_authorized_user_info(
                self._token_info, scopes=[GMAIL_SEND_SCOPE]
            )
            # build() gives the HTTP client a 60 s timeout by default.
            self._service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        return self._service
