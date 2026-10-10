from __future__ import annotations

from dataclasses import replace

import pytest

from fakes import (
    EXAMPLE_VTT,
    SMALL_VTT,
    SUMMARY_DICT,
    FakeOpenAI,
    FakeResponse,
    FakeSender,
    FakeSession,
    char_count,
    transcript_event,
)
from meeting_summariser import pipeline
from meeting_summariser.config import ConfigError, Settings
from meeting_summariser.pipeline import Job
from meeting_summariser.summarise import Summariser

SETTINGS = Settings(
    email_to=("team@example.com",),
    email_greeting="Hello team,",
    timezone="Australia/Sydney",
    openai_api_key="sk-test-not-a-real-key",
    gmail_token={"refresh_token": "r", "client_id": "c", "client_secret": "s"},
)


def fake_summariser(*replies):
    client = FakeOpenAI(*replies)
    return Summariser(client, model="test-model", chunk_tokens=10_000, count_tokens=char_count)


def test_run_downloads_summarises_and_sends():
    session = FakeSession(FakeResponse(200, EXAMPLE_VTT.read_bytes()))
    summariser = fake_summariser(SUMMARY_DICT)
    sender = FakeSender()
    job = Job.from_zoom_event(transcript_event())

    message_id = pipeline.run(job, SETTINGS, session=session, summariser=summariser, sender=sender)

    assert message_id == "msg-1"
    (download,) = session.calls
    assert download.url == "https://zoom.us/rec/webhook_download/transcript-file"
    assert download.headers == {"Authorization": "Bearer download-token-123"}

    sent_to_model = summariser.client.requests[0]["messages"][1]["content"]
    assert sent_to_model.startswith("Maya: Morning, everyone.")
    assert "-->" not in sent_to_model
    assert "WEBVTT" not in sent_to_model

    (email,) = sender.sent
    assert email["to"] == ("team@example.com",)
    assert email["subject"] == "Meeting summary: Weekly release sync, 5 March 2025"
    assert "Hello team," in email["text"]
    assert "Wednesday 5 March 2025 at 9:30 am AEDT" in email["html"]
    assert "Finish the regression run" in email["html"]


def test_run_falls_back_to_oauth_without_a_download_token():
    settings = replace(
        SETTINGS,
        zoom_client_id="client-id",
        zoom_client_secret="client-secret",
        zoom_account_id="account-id",
    )
    session = FakeSession(
        FakeResponse(200, data={"access_token": "oauth-token"}),
        FakeResponse(200, SMALL_VTT.encode()),
    )
    job = Job.from_zoom_event(transcript_event(download_token=None))

    pipeline.run(
        job,
        settings,
        session=session,
        summariser=fake_summariser(SUMMARY_DICT),
        sender=FakeSender(),
    )

    token_request, download = session.calls
    assert token_request.method == "POST"
    assert download.headers == {"Authorization": "Bearer oauth-token"}


def test_run_without_any_zoom_credentials_fails_before_downloading():
    session = FakeSession()
    job = Job.from_zoom_event(transcript_event(download_token=None))
    with pytest.raises(ConfigError, match="download token"):
        pipeline.run(
            job, SETTINGS, session=session, summariser=fake_summariser(), sender=FakeSender()
        )
    assert session.calls == []


@pytest.mark.parametrize(
    ("missing", "name"),
    [
        ("email_to", "EMAIL_TO"),
        ("gmail_token", "GMAIL_TOKEN_JSON"),
        ("openai_api_key", "OPENAI_API_KEY"),
    ],
)
def test_settings_are_checked_before_anything_is_downloaded(missing, name):
    settings = replace(SETTINGS, **{missing: () if missing == "email_to" else None})
    session = FakeSession()
    with pytest.raises(ConfigError, match=name):
        pipeline.run(Job.from_zoom_event(transcript_event()), settings, session=session)
    assert session.calls == []


def test_empty_transcript_sends_nothing():
    session = FakeSession(FakeResponse(200, b"WEBVTT\n\n"))
    summariser = fake_summariser()
    sender = FakeSender()
    job = Job.from_zoom_event(transcript_event())
    assert (
        pipeline.run(job, SETTINGS, session=session, summariser=summariser, sender=sender) is None
    )
    assert summariser.client.requests == []
    assert sender.sent == []


def test_recording_without_transcript_files_sends_nothing():
    job = Job.from_zoom_event(transcript_event(files=[]))
    session = FakeSession()
    assert pipeline.run(job, SETTINGS, session=session, sender=FakeSender()) is None
    assert session.calls == []


def test_several_transcript_files_are_joined_in_order():
    files = [
        {"file_type": "TRANSCRIPT", "download_url": "https://zoom.us/b", "recording_start": "2"},
        {"file_type": "TRANSCRIPT", "download_url": "https://zoom.us/a", "recording_start": "1"},
    ]
    first = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nMaya: First part.\n"
    second = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nTom: Second part.\n"
    session = FakeSession(FakeResponse(200, first.encode()), FakeResponse(200, second.encode()))
    summariser = fake_summariser(SUMMARY_DICT)
    job = Job.from_zoom_event(transcript_event(files=files))

    pipeline.run(job, SETTINGS, session=session, summariser=summariser, sender=FakeSender())

    assert [call.url for call in session.calls] == ["https://zoom.us/a", "https://zoom.us/b"]
    content = summariser.client.requests[0]["messages"][1]["content"]
    assert content == "Maya: First part.\nTom: Second part."


# The job handed from the webhook to the worker


def test_job_round_trips_through_the_invocation_payload():
    job = Job.from_zoom_event(transcript_event())
    assert Job.from_payload(job.to_payload()) == job


def test_job_repr_hides_the_download_token():
    assert "download-token-123" not in repr(Job.from_zoom_event(transcript_event()))


def test_job_from_a_malformed_event_is_empty():
    job = Job.from_zoom_event({"payload": "not an object"})
    assert job == Job(recording_uuid="", topic="", start_time=None, transcript_urls=())


def test_job_uses_the_meeting_uuid_not_the_reused_meeting_id():
    event = transcript_event()
    del event["payload"]["object"]["uuid"]
    assert Job.from_zoom_event(event).recording_uuid == ""
