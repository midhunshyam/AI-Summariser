"""Create the Gmail OAuth token for the worker. Run it once, on your own computer.

1. In Google Cloud, enable the Gmail API and create an OAuth client ID of type
   "Desktop app". Download its JSON file.
2. pip install google-auth-oauthlib   (or: pip install -e ".[gmail-setup]")
3. python scripts/create_gmail_token.py --client-secrets path/to/client_secret.json

A browser window asks you to sign in to the Gmail account that will send the
summaries and to allow it to send e-mail (the gmail.send scope, nothing else).
The token is written to .auth/gmail_token.json, which git ignores. Its contents
go in the Secrets Manager secret under GMAIL_TOKEN_JSON.

    python scripts/create_gmail_token.py --print-secret-shape

prints the shape of the whole secret, with descriptions instead of values.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

SECRET_SHAPE = {
    "ZOOM_WEBHOOK_SECRET": "<secret token from the Zoom app>",
    "OPENAI_API_KEY": "<OpenAI API key>",
    "GMAIL_TOKEN_JSON": {
        "token": "<access token>",
        "refresh_token": "<refresh token>",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "<OAuth client ID>",
        "client_secret": "<OAuth client secret>",
        "scopes": SCOPES,
    },
    "ZOOM_CLIENT_ID": "<optional: Server-to-Server OAuth client ID>",
    "ZOOM_CLIENT_SECRET": "<optional: Server-to-Server OAuth client secret>",
    "ZOOM_ACCOUNT_ID": "<optional: Zoom account ID>",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the Gmail OAuth token for the summariser.")
    parser.add_argument(
        "--client-secrets", type=Path, help="OAuth client JSON downloaded from Google Cloud"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(".auth/gmail_token.json"),
        help="where to write the token (default: .auth/gmail_token.json)",
    )
    parser.add_argument(
        "--print-secret-shape",
        action="store_true",
        help="print the shape of the Secrets Manager JSON and exit",
    )
    args = parser.parse_args(argv)

    if args.print_secret_shape:
        print(json.dumps(SECRET_SHAPE, indent=2))
        return 0
    if args.client_secrets is None:
        parser.error("--client-secrets is required unless --print-secret-shape is given")

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("error: run `pip install google-auth-oauthlib` first", file=sys.stderr)
        return 2

    flow = InstalledAppFlow.from_client_secrets_file(str(args.client_secrets), SCOPES)
    # prompt="consent" makes Google return a refresh token even on a repeat run.
    credentials = flow.run_local_server(port=0, prompt="consent")
    if not credentials.refresh_token:
        print("error: Google did not return a refresh token; run the script again", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(credentials.to_json())
    os.chmod(args.out, 0o600)  # in case the file already existed with wider permissions
    print(f"Wrote {args.out}. Its contents go in the secret under GMAIL_TOKEN_JSON.")
    print("The whole secret has this shape:")
    print(json.dumps(SECRET_SHAPE, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
