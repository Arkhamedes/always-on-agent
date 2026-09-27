#!/usr/bin/env python3
"""
One-time Google re-auth: mint a token.json carrying EVERY scope the agent
needs -- Calendar events (secretary), Calendar free/busy (availability only,
not event contents), Sheets read-only (Finance Pulse), Gmail read-only
(mail watch/search), and Drive METADATA-only (the librarian's document
finder, ADR-0007: it can name files and hand back links, never read their
contents).

Scope changes require full re-consent, so this ALWAYS runs the browser flow
(existing token.json is backed up to token.json.bak first). LAPTOP ONLY.

Before running (console.cloud.google.com, same project as your Calendar
OAuth client):
  - Enable the "Google Sheets API", the "Gmail API", AND the
    "Google Drive API" (APIs & Services -> Library).
  - Have credentials.json (OAuth Desktop client) next to this script or in
    the repo root -- re-download from Credentials if you've lost it.

Run:
  pip install google-auth google-auth-oauthlib google-api-python-client
  python3 test/google_reauth.py

Then copy the new token.json to the VM (or ask the agent to push it).
"""

import os
import shutil

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
    # Read/write: the finance pulse only reads, but expense tracking
    # (expenses.py) appends rows to the finance sheet's Expenses tabs.
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def find(name):
    for d in (os.getcwd(), ROOT, HERE):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return os.path.join(ROOT, name)


def main():
    creds_file = find("credentials.json")
    if not os.path.exists(creds_file):
        raise SystemExit(
            "credentials.json not found -- download your OAuth Desktop client "
            "from console.cloud.google.com -> Credentials and put it in the "
            "repo root.")

    token_file = find("token.json")
    if os.path.exists(token_file):
        shutil.copy(token_file, token_file + ".bak")
        print(f"backed up existing token to {token_file}.bak")

    flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
    # If no browser opens (WSL), copy the printed URL into Windows manually.
    creds = flow.run_local_server(port=0)

    out = os.path.join(ROOT, "token.json")
    with open(out, "w") as f:
        f.write(creds.to_json())
    os.chmod(out, 0o600)
    print(f"\nwrote {out} with scopes:")
    for s in creds.scopes:
        print(f"  - {s}")
    print("\nDone. Now get this token.json onto the VM (ask the agent).")


if __name__ == "__main__":
    main()
