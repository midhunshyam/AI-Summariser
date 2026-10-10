from __future__ import annotations

from fakes import EXAMPLE_VTT
from meeting_summariser.transcript import (
    Utterance,
    merge_turns,
    parse_cues,
    parse_vtt,
    speakers,
    to_text,
)

ZOOM_STYLE = """WEBVTT

1
00:00:00.000 --> 00:00:02.500
Maya: Morning, everyone.

2
00:00:02.500 --> 00:00:05.000
Maya: We'll start with the release.

3
00:00:05.200 --> 00:00:07.000
Tom: Sounds good.
"""


def test_header_cue_numbers_and_timings_are_dropped():
    text = to_text(parse_vtt(ZOOM_STYLE))
    assert text == "Maya: Morning, everyone. We'll start with the release.\nTom: Sounds good."
    assert "WEBVTT" not in text
    assert "-->" not in text


def test_consecutive_cues_from_one_speaker_become_one_turn():
    cues = parse_cues(ZOOM_STYLE)
    assert [c.speaker for c in cues] == ["Maya", "Maya", "Tom"]
    assert merge_turns(cues) == [
        Utterance("Maya", "Morning, everyone. We'll start with the release."),
        Utterance("Tom", "Sounds good."),
    ]


def test_a_speaker_returning_after_someone_else_starts_a_new_turn():
    vtt = """WEBVTT

00:00:00.000 --> 00:00:01.000
Maya: One.

00:00:01.000 --> 00:00:02.000
Tom: Two.

00:00:02.000 --> 00:00:03.000
Maya: Three.
"""
    assert [u.speaker for u in parse_vtt(vtt)] == ["Maya", "Tom", "Maya"]


def test_windows_line_endings_byte_order_mark_and_multi_line_cues():
    vtt = (
        "﻿WEBVTT\r\n\r\n1\r\n00:00:00.000 --> 00:00:01.000\r\n"
        "Maya: first line\r\n  second line  \r\n"
    )
    assert parse_vtt(vtt) == [Utterance("Maya", "first line second line")]


def test_note_style_and_region_blocks_are_skipped():
    vtt = """WEBVTT - exported transcript

NOTE
This file was edited by hand.

STYLE
::cue { color: white; }

REGION
id:left width:40%

00:00:00.000 --> 00:00:01.000 align:start position:10%
Priya: Hello.
"""
    assert parse_vtt(vtt) == [Utterance("Priya", "Hello.")]


def test_voice_tags_markup_and_entities():
    vtt = """WEBVTT

00:00:00.000 --> 00:00:02.000
<v Priya>The <i>Q&amp;A</i> is <00:00:01.000>at 3 &lt;- confirmed</v>

00:00:02.000 --> 00:00:03.000
<v.loud Tom>Great&nbsp;news</v>
"""
    assert parse_vtt(vtt) == [
        Utterance("Priya", "The Q&A is at 3 <- confirmed"),
        Utterance("Tom", "Great news"),
    ]


def test_cues_without_a_speaker_are_kept_separate():
    vtt = """WEBVTT

00:00:00.000 --> 00:00:01.000
Welcome to the session.

00:00:01.000 --> 00:00:02.000
Meet at 10:30 tomorrow.
"""
    assert parse_vtt(vtt) == [
        Utterance(None, "Welcome to the session."),
        Utterance(None, "Meet at 10:30 tomorrow."),
    ]
    assert to_text(parse_vtt(vtt)) == "Welcome to the session.\nMeet at 10:30 tomorrow."


def test_text_identifiers_are_dropped():
    vtt = """WEBVTT

intro-cue
00:00:00.000 --> 00:00:01.000
Maya: Hello.
"""
    assert parse_vtt(vtt) == [Utterance("Maya", "Hello.")]


def test_cues_not_separated_by_blank_lines_are_still_split():
    vtt = """WEBVTT

00:00:00.000 --> 00:00:01.000
Maya: One.
00:00:01.000 --> 00:00:02.000
Tom: Two.
"""
    assert parse_vtt(vtt) == [Utterance("Maya", "One."), Utterance("Tom", "Two.")]


def test_empty_cues_and_empty_files():
    assert parse_vtt("") == []
    assert parse_vtt("WEBVTT\n\n") == []
    assert parse_vtt("WEBVTT\n\n1\n00:00:00.000 --> 00:00:01.000\n\n") == []


def test_example_transcript():
    raw = EXAMPLE_VTT.read_text(encoding="utf-8")
    cues = parse_cues(raw)
    turns = merge_turns(cues)
    assert len(cues) == 40
    assert len(turns) < len(cues)
    assert speakers(turns) == ["Maya", "Tom", "Priya", "Daniel"]
    text = to_text(turns)
    assert "Q&A" in text
    assert "&amp;" not in text
    assert "-->" not in text
    assert all(line.split(": ", 1)[0] in speakers(turns) for line in text.splitlines())
