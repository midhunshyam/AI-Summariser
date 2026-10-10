"""Fakes and sample data shared by the tests. Nothing here touches the network."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
EXAMPLE_VTT = EXAMPLES / "weekly-release-sync.vtt"
ILLUSTRATIVE_SUMMARY = EXAMPLES / "illustrative-summary.json"

SUMMARY_DICT: dict[str, Any] = {
    "overview": "The team moved the release to Tuesday.",
    "key_points": ["The crash fix is merged.", "Regression testing is half done."],
    "decisions": ["Release 2.4 moves to next Tuesday."],
    "action_items": [
        {"task": "Finish the regression run", "owner": "Priya", "due": "Wednesday"},
        {"task": "Renew the staging certificate", "owner": None, "due": None},
    ],
}

SMALL_VTT = """WEBVTT

1
00:00:00.000 --> 00:00:02.000
Maya: Morning, everyone.

2
00:00:02.000 --> 00:00:04.000
Tom: The crash fix is merged.
"""


def char_count(text: str) -> int:
    """Fake tokenizer: one token per character."""
    return len(text)


class FakeCounter:
    """Stands in for TiktokenCounter without downloading an encoding."""

    encoding_name = "one-token-per-character"

    def __init__(self, model: str | None = None) -> None:
        self.model = model

    def __call__(self, text: str) -> int:
        return len(text)


def completion(content: str | None, *, refusal: str | None = None, finish_reason: str = "stop"):
    """Shaped like an openai ChatCompletion, with just the fields the code reads."""
    message = SimpleNamespace(content=content, refusal=refusal)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)])


class FakeOpenAI:
    """Stands in for openai.OpenAI: returns queued replies and records each request."""

    def __init__(self, *replies: Any) -> None:
        self._replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any):
        self.requests.append(kwargs)
        reply = self._replies.pop(0)
        if isinstance(reply, dict):
            return completion(json.dumps(reply))
        return reply


class FakeResponse:
    def __init__(self, status_code: int = 200, content: bytes = b"", data: Any = None) -> None:
        self.status_code = status_code
        self.content = content
        self._data = data

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self) -> Any:
        if self._data is None:
            raise ValueError("no JSON")
        return self._data


class FakeSession:
    """Records requests and returns queued responses (or raises queued exceptions)."""

    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.calls: list[SimpleNamespace] = []

    def _next(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(SimpleNamespace(method=method, url=url, **kwargs))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._next("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._next("POST", url, **kwargs)


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, **kwargs: Any) -> str:
        self.sent.append(kwargs)
        return f"msg-{len(self.sent)}"


def transcript_event(*, download_token: str | None = "download-token-123", files=None) -> dict:
    """A recording.transcript_completed event shaped like Zoom's, with made-up values."""
    if files is None:
        files = [
            {
                "file_type": "MP4",
                "file_extension": "MP4",
                "download_url": "https://zoom.us/rec/webhook_download/video-file",
                "recording_start": "2025-03-04T22:30:05Z",
            },
            {
                "file_type": "TRANSCRIPT",
                "file_extension": "VTT",
                "download_url": "https://zoom.us/rec/webhook_download/transcript-file",
                "recording_start": "2025-03-04T22:30:05Z",
            },
        ]
    event: dict[str, Any] = {
        "event": "recording.transcript_completed",
        "event_ts": 1741128000000,
        "payload": {
            "account_id": "test-account",
            "object": {
                "uuid": "test-meeting-uuid==",
                "id": 123456789,
                "host_email": "host@example.com",
                "topic": "Weekly release sync",
                "start_time": "2025-03-04T22:30:00Z",
                "password": "not-a-real-passcode",
                "recording_files": files,
            },
        },
    }
    if download_token is not None:
        event["download_token"] = download_token
    return event
