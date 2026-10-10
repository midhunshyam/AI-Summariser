"""Turn a WebVTT transcript into speaker turns.

Zoom's transcript files are WebVTT: a ``WEBVTT`` header, then cues made of a
number, a timing line and the text, with the speaker's name in front of the
text (``Maya: Morning, everyone.``). The model only needs who said what, so cue
numbers and timings are dropped and consecutive cues from the same speaker are
joined into one turn.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

_TAG = re.compile(r"<[^>]*>")
_VOICE = re.compile(r"<v(?:\.[^\s>]*)?\s+([^>]+)>", re.IGNORECASE)
_SPEAKER = re.compile(r"^([^:]{1,60}?):\s+(\S.*)$")
_BLANK_LINE = re.compile(r"\n[ \t]*\n")


@dataclass(frozen=True)
class Utterance:
    """What one speaker said, possibly across several cues."""

    speaker: str | None
    text: str

    def __str__(self) -> str:
        return f"{self.speaker}: {self.text}" if self.speaker else self.text


def parse_cues(vtt: str) -> list[Utterance]:
    """One utterance per cue, with speaker names split off and markup removed."""
    cues = []
    for payload in _cue_payloads(vtt):
        speaker, text = _split_speaker(payload)
        if text:
            cues.append(Utterance(speaker, text))
    return cues


def merge_turns(cues: Iterable[Utterance]) -> list[Utterance]:
    """Join consecutive utterances from the same named speaker."""
    turns: list[Utterance] = []
    for cue in cues:
        previous = turns[-1] if turns else None
        if previous and cue.speaker and previous.speaker == cue.speaker:
            turns[-1] = Utterance(cue.speaker, f"{previous.text} {cue.text}")
        else:
            turns.append(cue)
    return turns


def parse_vtt(vtt: str) -> list[Utterance]:
    """Speaker turns from a WebVTT transcript."""
    return merge_turns(parse_cues(vtt))


def to_text(utterances: Iterable[Utterance]) -> str:
    """One ``Speaker: text`` line per turn."""
    return "\n".join(str(u) for u in utterances)


def speakers(utterances: Iterable[Utterance]) -> list[str]:
    """Named speakers in order of first appearance."""
    seen: dict[str, None] = {}
    for u in utterances:
        if u.speaker:
            seen.setdefault(u.speaker, None)
    return list(seen)


def _cue_payloads(vtt: str) -> Iterator[str]:
    """Yield the text of each cue, joined onto one line.

    Blocks are separated by blank lines. Inside a block, lines before the first
    timing line (the cue number, or a header, NOTE or STYLE block) are skipped,
    and every timing line starts a new cue.
    """
    text = vtt.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    for block in _BLANK_LINE.split(text):
        cue: list[str] | None = None
        for raw_line in block.split("\n"):
            line = raw_line.strip()
            if "-->" in line:
                if cue:
                    yield " ".join(cue)
                cue = []
            elif cue is not None and line:
                cue.append(line)
        if cue:
            yield " ".join(cue)


def _split_speaker(payload: str) -> tuple[str | None, str]:
    """Separate the speaker from the cue text and strip WebVTT markup."""
    voice = _VOICE.search(payload)
    speaker = voice.group(1).strip() if voice else None
    text = " ".join(html.unescape(_TAG.sub("", payload)).split())
    if not speaker:
        match = _SPEAKER.match(text)
        if match:
            return match.group(1).strip(), match.group(2)
        return None, text
    return speaker, text
