"""Plain-text and HTML e-mail bodies for a meeting summary.

Both bodies are built from the structured summary, and every value is escaped
before it goes into HTML. The output depends only on the inputs (no clock, no
locale), so tests can compare it exactly.
"""

from __future__ import annotations

import html
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo

from .config import DEFAULT_GREETING
from .summarise import ActionItem, MeetingSummary

DISCLAIMER = (
    "This summary was produced automatically from the meeting transcript "
    "and may contain errors or omissions."
)
NONE_RECORDED = "None recorded."
NOT_STATED = "Not stated"
DEFAULT_TOPIC = "Zoom meeting"
TEXT_WIDTH = 76

_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip

_MUTED = "color:#6b7280;"
_P = "margin:0 0 12px;"
_H2 = "margin:24px 0 8px;font-size:16px;line-height:1.3;color:#111827;"
_UL = "margin:0 0 12px;padding-left:20px;"
_LI = "margin:0 0 6px;"
_TH = (
    "text-align:left;padding:6px 8px;border-bottom:2px solid #d1d5db;font-size:13px;color:#374151;"
)
_TD = "padding:6px 8px;border-bottom:1px solid #e5e7eb;vertical-align:top;"
_CARD = (
    "max-width:640px;margin:0 auto;padding:28px 32px;background-color:#ffffff;"
    "border-radius:8px;font-family:Arial,Helvetica,sans-serif;font-size:15px;"
    "line-height:1.55;color:#1f2937;"
)


@dataclass(frozen=True)
class Meeting:
    topic: str
    start: datetime | None = None


@dataclass(frozen=True)
class Email:
    subject: str
    text: str
    html: str


def format_date(moment: datetime) -> str:
    """``Wednesday 5 March 2025``, independent of the system locale."""
    return f"{_DAYS[moment.weekday()]} {moment.day} {_MONTHS[moment.month - 1]} {moment.year}"


def format_time(moment: datetime) -> str:
    """``9:30 am AEDT``, independent of the system locale."""
    hour = moment.hour % 12 or 12
    suffix = "am" if moment.hour < 12 else "pm"
    return f"{hour}:{moment.minute:02d} {suffix} {moment.tzname() or ''}".rstrip()


def render_email(
    summary: MeetingSummary,
    meeting: Meeting,
    *,
    greeting: str = DEFAULT_GREETING,
    tz: tzinfo = timezone.utc,
) -> Email:
    """Subject, plain-text body and HTML body for one meeting."""
    topic = _one_line(meeting.topic) or DEFAULT_TOPIC
    greeting = _one_line(greeting) or DEFAULT_GREETING
    when = None
    subject = f"Meeting summary: {topic}"
    if meeting.start is not None:
        start = meeting.start
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        local = start.astimezone(tz)
        when = f"{format_date(local)} at {format_time(local)}"
        subject += f", {local.day} {_MONTHS[local.month - 1]} {local.year}"
    return Email(
        subject=subject,
        text=_text_body(summary, topic, when, greeting),
        html=_html_body(summary, topic, when, greeting, subject),
    )


def _one_line(value: str | None) -> str:
    return " ".join((value or "").split())


def _e(value: str) -> str:
    return html.escape(value, quote=True)


def _html_body(
    summary: MeetingSummary, topic: str, when: str | None, greeting: str, subject: str
) -> str:
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_e(subject)}</title>",
        "</head>",
        '<body style="margin:0;padding:24px 12px;background-color:#f3f4f6;">',
        f'<div style="{_CARD}">',
        f'<p style="margin:0 0 4px;font-size:12px;letter-spacing:0.04em;'
        f'text-transform:uppercase;{_MUTED}">Meeting summary</p>',
        f'<h1 style="margin:0 0 4px;font-size:22px;line-height:1.3;color:#111827;">'
        f"{_e(topic)}</h1>",
    ]
    if when:
        parts.append(f'<p style="margin:0;font-size:14px;{_MUTED}">{_e(when)}</p>')
    parts += [
        f'<p style="margin:20px 0 12px;">{_e(greeting)}</p>',
        f'<p style="{_P}">Here is the summary of the meeting.</p>',
        f'<h2 style="{_H2}">Overview</h2>',
        _paragraph_html(summary.overview),
        f'<h2 style="{_H2}">Key points</h2>',
        _list_html(summary.key_points),
        f'<h2 style="{_H2}">Decisions</h2>',
        _list_html(summary.decisions),
        f'<h2 style="{_H2}">Action items</h2>',
        _actions_html(summary.action_items),
        f'<p style="margin:28px 0 0;padding-top:12px;border-top:1px solid #e5e7eb;'
        f'font-size:12px;{_MUTED}">{_e(DISCLAIMER)}</p>',
        "</div>",
        "</body>",
        "</html>",
    ]
    return "\n".join(parts) + "\n"


def _paragraph_html(text: str) -> str:
    if not text:
        return f'<p style="{_P}{_MUTED}">{NONE_RECORDED}</p>'
    return f'<p style="{_P}">{_e(text)}</p>'


def _list_html(items: Sequence[str]) -> str:
    if not items:
        return f'<p style="{_P}{_MUTED}">{NONE_RECORDED}</p>'
    rows = "\n".join(f'<li style="{_LI}">{_e(item)}</li>' for item in items)
    return f'<ul style="{_UL}">\n{rows}\n</ul>'


def _actions_html(items: Sequence[ActionItem]) -> str:
    if not items:
        return f'<p style="{_P}{_MUTED}">{NONE_RECORDED}</p>'
    rows = [
        '<table style="width:100%;border-collapse:collapse;font-size:14px;margin:0 0 12px;">',
        "<thead>",
        f'<tr><th style="{_TH}">Task</th><th style="{_TH}">Owner</th>'
        f'<th style="{_TH}">Due</th></tr>',
        "</thead>",
        "<tbody>",
    ]
    for item in items:
        rows.append(
            f'<tr><td style="{_TD}">{_e(item.task)}</td>'
            f'<td style="{_TD}">{_cell(item.owner)}</td>'
            f'<td style="{_TD}">{_cell(item.due)}</td></tr>'
        )
    rows += ["</tbody>", "</table>"]
    return "\n".join(rows)


def _cell(value: str | None) -> str:
    if value:
        return _e(value)
    return f'<span style="{_MUTED}">{NOT_STATED}</span>'


def _text_body(summary: MeetingSummary, topic: str, when: str | None, greeting: str) -> str:
    lines = [f"Meeting summary: {topic}"]
    if when:
        lines.append(when)
    lines += ["", greeting, "", "Here is the summary of the meeting.", ""]
    lines += ["OVERVIEW", _wrap(summary.overview or NONE_RECORDED), ""]
    lines += ["KEY POINTS", *_bullets(summary.key_points), ""]
    lines += ["DECISIONS", *_bullets(summary.decisions), ""]
    lines.append("ACTION ITEMS")
    if summary.action_items:
        for item in summary.action_items:
            owner = (item.owner or NOT_STATED).rstrip(".")
            due = (item.due or NOT_STATED).rstrip(".")
            lines.append(_wrap(item.task, "- ", "  "))
            lines.append(_wrap(f"Owner: {owner}. Due: {due}.", "  ", "  "))
    else:
        lines.append(NONE_RECORDED)
    lines += ["", "--", _wrap(DISCLAIMER)]
    return "\n".join(lines) + "\n"


def _bullets(items: Sequence[str]) -> list[str]:
    if not items:
        return [NONE_RECORDED]
    return [_wrap(item, "- ", "  ") for item in items]


def _wrap(text: str, first: str = "", rest: str = "") -> str:
    return textwrap.fill(
        text,
        width=TEXT_WIDTH,
        initial_indent=first,
        subsequent_indent=rest,
        break_long_words=False,
        break_on_hyphens=False,
    )
