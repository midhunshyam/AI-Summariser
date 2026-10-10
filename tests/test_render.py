from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from meeting_summariser.render import (
    DISCLAIMER,
    NONE_RECORDED,
    TEXT_WIDTH,
    Meeting,
    format_time,
    render_email,
)
from meeting_summariser.summarise import ActionItem, MeetingSummary

SYDNEY = ZoneInfo("Australia/Sydney")
START = datetime(2025, 3, 4, 22, 30, tzinfo=timezone.utc)  # 9:30 am the next day in Sydney

SUMMARY = MeetingSummary(
    overview="The release moves to Tuesday.",
    key_points=("The crash fix is merged.", "Regression testing is half done."),
    decisions=("Release 2.4 moves to next Tuesday.",),
    action_items=(
        ActionItem("Finish the regression run", "Priya", "Wednesday afternoon"),
        ActionItem("Renew the staging certificate"),
    ),
)

HOSTILE = MeetingSummary(
    overview='Use <b>bold</b> & "quotes" here.',
    key_points=("<script>alert('x')</script>",),
    decisions=("<img src=x onerror=alert(1)>",),
    action_items=(ActionItem("<a href='https://example.com'>click</a>", "<Tom>", "<Friday>"),),
)


def test_subject_and_date_line_use_the_configured_time_zone():
    email = render_email(SUMMARY, Meeting("Weekly release sync", START), tz=SYDNEY)
    assert email.subject == "Meeting summary: Weekly release sync, 5 March 2025"
    assert "Wednesday 5 March 2025 at 9:30 am AEDT" in email.text
    assert "Wednesday 5 March 2025 at 9:30 am AEDT" in email.html


def test_utc_is_the_default_time_zone():
    email = render_email(SUMMARY, Meeting("Sync", START))
    assert email.subject == "Meeting summary: Sync, 4 March 2025"
    assert "Tuesday 4 March 2025 at 10:30 pm UTC" in email.text


def test_naive_start_time_is_taken_as_utc():
    naive = datetime(2025, 3, 4, 22, 30)
    assert render_email(SUMMARY, Meeting("Sync", naive)) == render_email(
        SUMMARY, Meeting("Sync", START)
    )


def test_without_a_start_time_there_is_no_date():
    email = render_email(SUMMARY, Meeting("Sync"))
    assert email.subject == "Meeting summary: Sync"
    assert email.text.splitlines()[:3] == ["Meeting summary: Sync", "", "Hi all,"]


def test_html_escapes_every_value():
    page = render_email(
        HOSTILE, Meeting("Q&A <launch>", START), greeting="Hi <team> & co,", tz=SYDNEY
    ).html
    for raw in ("<b>", "<script", "<img", "<a href", "<Tom>", "<Friday>", "<launch>", "<team>"):
        assert raw not in page
    assert "Use &lt;b&gt;bold&lt;/b&gt; &amp; &quot;quotes&quot; here." in page
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in page
    assert "Q&amp;A &lt;launch&gt;" in page
    assert "Hi &lt;team&gt; &amp; co," in page


def test_plain_text_is_not_escaped():
    text = render_email(HOSTILE, Meeting("Q&A", START)).text
    assert 'Use <b>bold</b> & "quotes" here.' in text
    assert "Owner: <Tom>. Due: <Friday>." in text


def test_html_has_every_section_and_an_action_table():
    page = render_email(SUMMARY, Meeting("Sync", START)).html
    for heading in ("Overview", "Key points", "Decisions", "Action items"):
        assert f">{heading}</h2>" in page
    assert page.count("<li ") == 3
    assert page.count("<tr>") == 3  # header row and two items
    assert page.count("Not stated") == 2
    assert page.startswith("<!DOCTYPE html>")
    assert '<meta charset="utf-8">' in page


def test_empty_sections_say_none_recorded():
    email = render_email(MeetingSummary(overview=""), Meeting("Sync"))
    assert email.text.count(NONE_RECORDED) == 4
    assert email.html.count(NONE_RECORDED) == 4
    assert "<ul" not in email.html
    assert "<table" not in email.html


def test_both_bodies_end_with_the_disclaimer():
    email = render_email(SUMMARY, Meeting("Sync", START))
    assert email.text.replace("\n", " ").rstrip().endswith(DISCLAIMER)
    assert DISCLAIMER in email.html


def test_subject_cannot_carry_extra_header_lines():
    email = render_email(SUMMARY, Meeting("Sync\r\nBcc: someone@example.com"))
    assert "\n" not in email.subject
    assert "\r" not in email.subject
    assert email.subject == "Meeting summary: Sync Bcc: someone@example.com"


def test_blank_topic_and_greeting_fall_back_to_defaults():
    email = render_email(SUMMARY, Meeting("   "), greeting="  ")
    assert email.subject == "Meeting summary: Zoom meeting"
    assert "Hi all," in email.text


def test_plain_text_lines_are_wrapped():
    long_point = " ".join(["word"] * 60)
    summary = MeetingSummary(overview=long_point, key_points=(long_point,))
    text = render_email(summary, Meeting("Sync")).text
    assert max(len(line) for line in text.splitlines()) <= TEXT_WIDTH


def test_output_is_deterministic():
    first = render_email(SUMMARY, Meeting("Sync", START), tz=SYDNEY)
    second = render_email(SUMMARY, Meeting("Sync", START), tz=SYDNEY)
    assert first == second


def test_plain_text_layout():
    email = render_email(
        SUMMARY, Meeting("Weekly release sync", START), greeting="Hello team,", tz=SYDNEY
    )
    assert email.text == (
        "Meeting summary: Weekly release sync\n"
        "Wednesday 5 March 2025 at 9:30 am AEDT\n"
        "\n"
        "Hello team,\n"
        "\n"
        "Here is the summary of the meeting.\n"
        "\n"
        "OVERVIEW\n"
        "The release moves to Tuesday.\n"
        "\n"
        "KEY POINTS\n"
        "- The crash fix is merged.\n"
        "- Regression testing is half done.\n"
        "\n"
        "DECISIONS\n"
        "- Release 2.4 moves to next Tuesday.\n"
        "\n"
        "ACTION ITEMS\n"
        "- Finish the regression run\n"
        "  Owner: Priya. Due: Wednesday afternoon.\n"
        "- Renew the staging certificate\n"
        "  Owner: Not stated. Due: Not stated.\n"
        "\n"
        "--\n"
        "This summary was produced automatically from the meeting transcript and may\n"
        "contain errors or omissions.\n"
    )


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (0, 5, "12:05 am UTC"),
        (9, 30, "9:30 am UTC"),
        (12, 0, "12:00 pm UTC"),
        (23, 59, "11:59 pm UTC"),
    ],
)
def test_format_time(hour, minute, expected):
    assert format_time(datetime(2025, 1, 1, hour, minute, tzinfo=timezone.utc)) == expected
