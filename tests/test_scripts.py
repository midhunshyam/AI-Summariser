from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from meeting_summariser import config
from meeting_summariser.mailer import GMAIL_SEND_SCOPE

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "create_gmail_token.py"


def run_script(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


def test_secret_shape_matches_the_keys_the_code_reads():
    result = run_script("--print-secret-shape")
    assert result.returncode == 0
    shape = json.loads(result.stdout)
    assert set(shape) == set(config.SECRET_KEYS)
    assert shape["GMAIL_TOKEN_JSON"]["scopes"] == [GMAIL_SEND_SCOPE]


def test_secret_shape_holds_descriptions_not_values():
    shape = json.loads(run_script("--print-secret-shape").stdout)
    token = shape.pop("GMAIL_TOKEN_JSON")
    values = [*shape.values(), *(v for k, v in token.items() if k not in ("token_uri", "scopes"))]
    assert all(value.startswith("<") and value.endswith(">") for value in values)


def test_client_secrets_are_required_to_create_a_token():
    result = run_script()
    assert result.returncode == 2
    assert "--client-secrets" in result.stderr
