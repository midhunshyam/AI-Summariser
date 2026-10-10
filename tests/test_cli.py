from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes import EXAMPLE_VTT, ILLUSTRATIVE_SUMMARY, SUMMARY_DICT, FakeCounter, FakeOpenAI
from meeting_summariser import cli, summarise

SRC = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture
def offline_tokens(monkeypatch):
    monkeypatch.setattr(cli, "TiktokenCounter", FakeCounter)
    monkeypatch.setattr(summarise, "TiktokenCounter", FakeCounter)


def statistics(output: str) -> dict[str, str]:
    rows = (line.split(":", 1) for line in output.strip().splitlines())
    return {label.strip(): value.strip() for label, value in rows}


def test_dry_run_prints_statistics_without_an_api_key(offline_tokens, capsys):
    assert cli.main([str(EXAMPLE_VTT), "--dry-run"]) == 0
    stats = statistics(capsys.readouterr().out)
    assert stats["Cues"] == "40"
    assert stats["Speakers"] == "4 (Maya, Tom, Priya, Daniel)"
    assert int(stats["Tokens after cleaning"].replace(",", "")) < int(
        stats["Tokens in the file"].split()[0].replace(",", "")
    )
    assert stats["Chunks"].startswith("1 ")
    assert stats["Model requests"] == "1"


def test_dry_run_with_small_chunks_needs_a_merge_request(offline_tokens, capsys):
    assert cli.main([str(EXAMPLE_VTT), "--dry-run", "--chunk-tokens", "1000"]) == 0
    stats = statistics(capsys.readouterr().out)
    chunks = int(stats["Chunks"].split()[0])
    assert chunks > 1
    assert stats["Model requests"] == str(chunks + 1)


def test_summarise_writes_html_text_and_json(offline_tokens, monkeypatch, tmp_path, capsys):
    client = FakeOpenAI(SUMMARY_DICT)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(cli, "make_openai_client", lambda api_key: client)
    html, text, data = tmp_path / "s.html", tmp_path / "s.txt", tmp_path / "s.json"

    code = cli.main(
        [str(EXAMPLE_VTT), "--html", str(html), "--text", str(text), "--json", str(data)]
    )

    assert code == 0
    assert json.loads(data.read_text()) == SUMMARY_DICT
    assert "Weekly release sync" in html.read_text()  # topic taken from the file name
    assert text.read_text().startswith("Meeting summary: Weekly release sync\n")
    assert client.requests[0]["model"] == "gpt-4o-mini"
    assert capsys.readouterr().out == ""


def test_model_and_topic_options(offline_tokens, monkeypatch, capsys):
    client = FakeOpenAI(SUMMARY_DICT)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(cli, "make_openai_client", lambda api_key: client)
    assert cli.main([str(EXAMPLE_VTT), "--model", "other-model", "--topic", "Release sync"]) == 0
    assert client.requests[0]["model"] == "other-model"
    assert capsys.readouterr().out.startswith("Meeting summary: Release sync\n")


def test_missing_api_key_is_explained(capsys):
    assert cli.main([str(EXAMPLE_VTT)]) == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().err


def test_from_summary_renders_without_any_api(tmp_path):
    html = tmp_path / "layout.html"
    code = cli.main(
        [
            "--from-summary",
            str(ILLUSTRATIVE_SUMMARY),
            "--html",
            str(html),
            "--topic",
            "Weekly release sync",
            "--start",
            "2025-03-04T22:30:00Z",
            "--timezone",
            "Australia/Sydney",
        ]
    )
    assert code == 0
    page = html.read_text()
    assert "Wednesday 5 March 2025 at 9:30 am AEDT" in page
    assert "Q&amp;A" in page
    assert page.count("<tr>") == 9  # header row and eight action items


def test_plain_text_goes_to_stdout_by_default(capsys):
    assert cli.main(["--from-summary", str(ILLUSTRATIVE_SUMMARY)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Meeting summary: Zoom meeting\n")
    assert "ACTION ITEMS" in out


def test_illustrative_summary_is_labelled_as_not_model_output():
    data = json.loads(ILLUSTRATIVE_SUMMARY.read_text())
    assert "not output from a model run" in data["_note"]


@pytest.mark.parametrize(
    "argv",
    [
        [],
        [str(EXAMPLE_VTT), "--from-summary", str(ILLUSTRATIVE_SUMMARY)],
        ["--from-summary", str(ILLUSTRATIVE_SUMMARY), "--dry-run"],
        [str(EXAMPLE_VTT), "--start", "next Tuesday"],
        [str(EXAMPLE_VTT), "--timezone", "Not/AZone"],
        [str(EXAMPLE_VTT), "--chunk-tokens", "10"],
    ],
)
def test_usage_errors_exit_with_status_2(argv, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(argv)
    assert caught.value.code == 2


def test_missing_file_is_an_error(tmp_path, capsys):
    assert cli.main([str(tmp_path / "missing.vtt"), "--dry-run"]) == 1
    assert "missing.vtt" in capsys.readouterr().err


def test_unwritable_output_is_an_error(tmp_path, capsys):
    target = tmp_path / "missing-folder" / "layout.html"
    assert cli.main(["--from-summary", str(ILLUSTRATIVE_SUMMARY), "--html", str(target)]) == 1
    assert "missing-folder" in capsys.readouterr().err


def test_invalid_summary_file_is_an_error(tmp_path, capsys):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert cli.main(["--from-summary", str(broken)]) == 1
    assert "not valid JSON" in capsys.readouterr().err


def test_api_errors_are_reported_without_a_traceback(offline_tokens, monkeypatch, capsys):
    class Unavailable(Exception):
        pass

    def create(**kwargs):
        raise Unavailable("service unavailable")

    broken = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(cli, "make_openai_client", lambda api_key: broken)
    assert cli.main([str(EXAMPLE_VTT)]) == 1
    assert "Unavailable: service unavailable" in capsys.readouterr().err


def test_python_dash_m_entry_point():
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    result = subprocess.run(
        [sys.executable, "-m", "meeting_summariser", "--help"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.startswith("usage: python -m meeting_summariser")


def test_webhook_module_does_not_import_the_heavy_clients():
    """Keeps the webhook's cold start short: these load only in the worker."""
    code = (
        "import sys, meeting_summariser.handlers\n"
        "heavy = ('openai', 'tiktoken', 'googleapiclient', 'boto3')\n"
        "print(','.join(m for m in heavy if m in sys.modules))"
    )
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
    )
    assert result.stdout.strip() == ""
