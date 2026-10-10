"""Command-line use: summarise a local transcript, no AWS or Zoom needed.

    python -m meeting_summariser meeting.vtt --html summary.html

Summarising needs OPENAI_API_KEY in the environment. ``--dry-run`` and
``--from-summary`` run without it.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import ConfigError, Settings, settings_from
from .render import DEFAULT_TOPIC, Meeting, render_email
from .summarise import (
    MeetingSummary,
    Summariser,
    SummaryError,
    TiktokenCounter,
    chunk_utterances,
    make_openai_client,
)
from .transcript import Utterance, merge_turns, parse_cues, speakers, to_text
from .zoom import parse_start_time


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m meeting_summariser",
        description=(
            "Summarise a Zoom WebVTT transcript and write the e-mail body as HTML and plain text."
        ),
    )
    parser.add_argument("transcript", nargs="?", type=Path, help="WebVTT transcript (.vtt)")
    parser.add_argument("--html", type=Path, metavar="FILE", help="write the HTML body to FILE")
    parser.add_argument(
        "--text", type=Path, metavar="FILE", help="write the plain-text body to FILE"
    )
    parser.add_argument(
        "--json", type=Path, metavar="FILE", help="write the structured summary to FILE"
    )
    parser.add_argument(
        "--from-summary",
        type=Path,
        metavar="JSON",
        help="render a saved summary instead of summarising a transcript",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="parse and chunk the transcript, print statistics and stop (no API call)",
    )
    parser.add_argument("--topic", help="meeting title (default: from the file name)")
    parser.add_argument(
        "--start", metavar="TIME", help="meeting start in ISO 8601, e.g. 2025-03-04T22:30:00Z"
    )
    parser.add_argument(
        "--timezone", metavar="ZONE", help="IANA time zone for the date (default: TIMEZONE or UTC)"
    )
    parser.add_argument("--model", help="OpenAI model (default: OPENAI_MODEL or gpt-4o-mini)")
    parser.add_argument(
        "--chunk-tokens",
        type=int,
        metavar="N",
        help="largest chunk in tokens (default: CHUNK_TOKENS or 12000)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log progress to stderr")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (args.transcript is None) == (args.from_summary is None):
        parser.error("give either a transcript file or --from-summary")
    if args.dry_run and args.from_summary:
        parser.error("--dry-run needs a transcript file")
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    environ = dict(os.environ)
    for key, value in (
        ("OPENAI_MODEL", args.model),
        ("CHUNK_TOKENS", args.chunk_tokens),
        ("TIMEZONE", args.timezone),
    ):
        if value is not None:
            environ[key] = str(value)
    try:
        settings = settings_from(environ, {"OPENAI_API_KEY": environ.get("OPENAI_API_KEY", "")})
    except ConfigError as exc:
        parser.error(str(exc))
    start = parse_start_time(args.start)
    if args.start and start is None:
        parser.error("--start must be an ISO 8601 time, e.g. 2025-03-04T22:30:00Z")

    try:
        if args.from_summary:
            summary = MeetingSummary.from_dict(_read_json(args.from_summary))
            topic = args.topic or DEFAULT_TOPIC
        else:
            raw = args.transcript.read_text(encoding="utf-8-sig")
            cues = parse_cues(raw)
            turns = merge_turns(cues)
            if args.dry_run:
                _print_statistics(args.transcript, raw, cues, turns, settings)
                return 0
            if not settings.openai_api_key:
                print(
                    "error: OPENAI_API_KEY is not set. --dry-run and --from-summary work "
                    "without it.",
                    file=sys.stderr,
                )
                return 2
            summariser = Summariser(
                make_openai_client(settings.openai_api_key),
                model=settings.openai_model,
                chunk_tokens=settings.chunk_tokens,
            )
            summary = summariser.summarise(turns)
            topic = args.topic or _topic_from_path(args.transcript)
    except (OSError, ValueError, SummaryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # errors raised by the OpenAI client
        if args.verbose:
            raise
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    email = render_email(
        summary, Meeting(topic, start), greeting=settings.email_greeting, tz=settings.tz
    )
    outputs = [
        (args.json, json.dumps(summary.to_dict(), indent=2, ensure_ascii=False) + "\n"),
        (args.html, email.html),
        (args.text, email.text),
    ]
    try:
        for path, content in outputs:
            if path is not None:
                path.write_text(content, encoding="utf-8")
                print(f"Wrote {path}", file=sys.stderr)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not args.html and not args.text:
        sys.stdout.write(email.text)
    return 0


def _print_statistics(
    path: Path, raw: str, cues: Sequence[Utterance], turns: Sequence[Utterance], settings: Settings
) -> None:
    count = TiktokenCounter(settings.openai_model)
    chunks = chunk_utterances(turns, count, settings.chunk_tokens)
    model_requests = len(chunks) + 1 if len(chunks) > 1 else len(chunks)
    names = speakers(turns)
    rows = [
        ("Transcript", str(path)),
        ("Cues", f"{len(cues):,}"),
        ("Speaker turns", f"{len(turns):,}"),
        ("Speakers", f"{len(names)} ({', '.join(names)})" if names else "0"),
        ("Tokens in the file", f"{count(raw):,} ({count.encoding_name})"),
        ("Tokens after cleaning", f"{count(to_text(turns)):,}"),
        ("Chunks", f"{len(chunks)} (limit {settings.chunk_tokens:,} tokens each)"),
        ("Model requests", str(model_requests)),
    ]
    width = max(len(label) for label, _ in rows) + 2
    for label, value in rows:
        print(f"{label + ':':<{width}}{value}")


def _topic_from_path(path: Path) -> str:
    words = path.stem.replace("_", " ").replace("-", " ").split()
    text = " ".join(words)
    return text[:1].upper() + text[1:] if text else DEFAULT_TOPIC


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON ({exc.msg}, line {exc.lineno})") from None
