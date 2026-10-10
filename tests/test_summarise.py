from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from fakes import SUMMARY_DICT, FakeOpenAI, char_count, completion
from meeting_summariser import summarise
from meeting_summariser.summarise import (
    MERGE_PROMPT,
    SUMMARY_SCHEMA,
    ActionItem,
    MeetingSummary,
    Summariser,
    SummaryError,
    chunk_utterances,
)
from meeting_summariser.transcript import Utterance, to_text


def turns(*pairs: tuple[str | None, str]) -> list[Utterance]:
    return [Utterance(speaker, text) for speaker, text in pairs]


# Chunking (one token per character)


def test_short_transcript_is_one_chunk():
    utterances = turns(("Maya", "Morning."), ("Tom", "Hi."))
    assert chunk_utterances(utterances, char_count, 1000) == [to_text(utterances)]


def test_chunks_stay_within_the_limit_and_break_between_turns():
    utterances = [
        Utterance(name, f"Point {i} " + "word " * (i % 7))
        for i, name in enumerate(["Maya", "Tom", "Priya", "Daniel"] * 10)
    ]
    chunks = chunk_utterances(utterances, char_count, 120)
    assert len(chunks) > 1
    assert all(char_count(chunk) <= 120 for chunk in chunks)
    # Nothing lost or reordered, and no turn split across chunks.
    assert "\n".join(chunks) == to_text(utterances)


def test_a_long_turn_is_split_between_words_and_keeps_the_speaker():
    words = [f"w{i}" for i in range(200)]
    utterances = turns(("Maya", "Short."), ("Tom", " ".join(words)), ("Maya", "Thanks."))
    chunks = chunk_utterances(utterances, char_count, 100)
    assert all(char_count(chunk) <= 100 for chunk in chunks)
    lines = [line for chunk in chunks for line in chunk.split("\n")]
    tom_lines = [line for line in lines if line.startswith("Tom: ")]
    assert len(tom_lines) > 1
    rebuilt = " ".join(line[len("Tom: ") :] for line in tom_lines)
    assert rebuilt.split() == words
    assert lines[0] == "Maya: Short."
    assert lines[-1] == "Maya: Thanks."


def test_a_word_longer_than_the_limit_is_cut():
    word = "x" * 300
    chunks = chunk_utterances(turns((None, word)), char_count, 50)
    assert all(char_count(chunk) <= 50 for chunk in chunks)
    assert "".join(chunks) == word


def test_no_turns_means_no_chunks():
    assert chunk_utterances([], char_count, 100) == []


def test_tiny_limits_are_rejected():
    with pytest.raises(ValueError, match="at least"):
        chunk_utterances(turns(("Maya", "Hi")), char_count, 5)


# Summarising with a fake OpenAI client

SMALL = turns(("Maya", "We move the release to Tuesday."), ("Priya", "I'll finish testing."))


def test_one_chunk_means_one_request_with_a_strict_json_schema():
    client = FakeOpenAI(SUMMARY_DICT)
    summariser = Summariser(
        client, model="test-model", chunk_tokens=10_000, count_tokens=char_count
    )
    summary = summariser.summarise(SMALL)

    assert summary == MeetingSummary.from_dict(SUMMARY_DICT)
    assert len(client.requests) == 1
    request = client.requests[0]
    assert request["model"] == "test-model"
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["strict"] is True
    assert request["response_format"]["json_schema"]["schema"] is SUMMARY_SCHEMA
    system, user = request["messages"]
    assert system["role"] == "system"
    assert "the full transcript of a meeting" in system["content"]
    assert user == {"role": "user", "content": to_text(SMALL)}
    # Left out so that models which only accept the defaults also work.
    assert "temperature" not in request
    assert "max_tokens" not in request


def test_a_long_transcript_is_summarised_in_parts_and_merged():
    utterances = [
        Utterance("Maya", "a" * 60),
        Utterance("Tom", "b" * 60),
        Utterance("Priya", "c" * 60),
    ]
    parts = [
        {**SUMMARY_DICT, "overview": f"Part {n}.", "decisions": [f"Decision {n}"]}
        for n in (1, 2, 3)
    ]
    merged = {**SUMMARY_DICT, "overview": "Whole meeting."}
    client = FakeOpenAI(*parts, merged)
    summariser = Summariser(client, model="m", chunk_tokens=80, count_tokens=char_count)

    summary = summariser.summarise(utterances)

    assert summary.overview == "Whole meeting."
    assert len(client.requests) == 4
    for number, request in enumerate(client.requests[:3], start=1):
        assert f"part {number} of 3" in request["messages"][0]["content"]
    merge_request = client.requests[3]
    assert merge_request["messages"][0]["content"] == MERGE_PROMPT
    sent_parts = json.loads(merge_request["messages"][1]["content"])
    assert [p["overview"] for p in sent_parts] == ["Part 1.", "Part 2.", "Part 3."]
    assert sent_parts[2]["decisions"] == ["Decision 3"]


def test_refusal_raises():
    client = FakeOpenAI(completion(None, refusal="I can't help with that."))
    with pytest.raises(SummaryError, match="declined"):
        Summariser(client, count_tokens=char_count).summarise(SMALL)


def test_cut_off_reply_raises():
    client = FakeOpenAI(completion('{"overview": "The team', finish_reason="length"))
    with pytest.raises(SummaryError, match="cut off"):
        Summariser(client, count_tokens=char_count).summarise(SMALL)


@pytest.mark.parametrize("content", ["not json", "", None])
def test_reply_that_is_not_json_raises(content):
    client = FakeOpenAI(completion(content))
    with pytest.raises(SummaryError):
        Summariser(client, count_tokens=char_count).summarise(SMALL)


def test_reply_that_is_not_an_object_raises():
    client = FakeOpenAI(completion("[1, 2, 3]"))
    with pytest.raises(SummaryError):
        Summariser(client, count_tokens=char_count).summarise(SMALL)


def test_empty_transcript_raises_without_calling_the_api():
    client = FakeOpenAI()
    with pytest.raises(SummaryError):
        Summariser(client, count_tokens=char_count).summarise([])
    assert client.requests == []


# The summary structure


def test_from_dict_ignores_unknown_keys_and_blank_entries():
    summary = MeetingSummary.from_dict(
        {
            "overview": "  Two   spaces\nand a newline. ",
            "key_points": ["One", "", "   ", 42, None, "Two"],
            "decisions": "not a list",
            "action_items": [
                {"task": "Do it", "owner": "", "due": None},
                {"task": "  ", "owner": "Tom"},
                {"owner": "Maya"},
                "not an object",
                {"task": "Ship it", "owner": "Priya", "due": "Friday", "extra": True},
            ],
            "extra": "ignored",
        }
    )
    assert summary == MeetingSummary(
        overview="Two spaces and a newline.",
        key_points=("One", "Two"),
        decisions=(),
        action_items=(ActionItem("Do it"), ActionItem("Ship it", "Priya", "Friday")),
    )


def test_to_dict_round_trip():
    summary = MeetingSummary.from_dict(SUMMARY_DICT)
    assert summary.to_dict() == SUMMARY_DICT
    assert MeetingSummary.from_dict(summary.to_dict()) == summary


def test_schema_meets_structured_output_rules():
    """Strict mode needs every object closed and every property listed as required."""

    def objects(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                yield node
            for value in node.values():
                yield from objects(value)

    found = list(objects(SUMMARY_SCHEMA))
    assert len(found) == 2
    for node in found:
        assert node["additionalProperties"] is False
        assert set(node["required"]) == set(node["properties"])


def test_tiktoken_counter_falls_back_to_o200k_for_unknown_models(monkeypatch):
    calls = []

    class Encoding:
        name = "o200k_base"

        def encode_ordinary(self, text):
            return list(text.encode())

    def encoding_for_model(model):
        calls.append(model)
        raise KeyError(model)

    fake = SimpleNamespace(
        encoding_for_model=encoding_for_model, get_encoding=lambda name: Encoding()
    )
    monkeypatch.setitem(sys.modules, "tiktoken", fake)
    counter = summarise.TiktokenCounter("a-model-tiktoken-does-not-know")
    assert calls == []  # nothing is loaded until the first count
    assert counter("abc") == 3
    assert counter.encoding_name == "o200k_base"
    assert calls == ["a-model-tiktoken-does-not-know"]


def test_openai_client_has_a_timeout_and_retries():
    client = summarise.make_openai_client("sk-test-not-a-real-key")
    assert client.timeout == summarise.OPENAI_TIMEOUT_SECONDS
    assert client.max_retries == summarise.OPENAI_MAX_RETRIES
