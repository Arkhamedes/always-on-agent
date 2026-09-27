#!/usr/bin/env python3
"""
Google Calendar smoke test: prove auth + a single calendar write.

Google has no clean headless login, so authorization happens ONCE on a machine
with a browser (your laptop), then the resulting token is copied to Zo:

  1. LAPTOP: put credentials.json next to this script and run it. A browser
     opens -> sign in -> click through the "unverified app" warning (expected;
     it's your own personal app) -> grant Calendar access. token.json is written
     and a test event is created.
  2. Copy token.json to Zo (/home/workspace).
  3. ZO: run this script. It finds token.json, needs no browser, and creates
     another test event -- proving the headless path.

token.json's refresh token then keeps Zo authorized. IMPORTANT: the Google
project must be published "In production" (not Testing), or the refresh token
dies after 7 days and the secretary stops working.

Setup (console.cloud.google.com):
  - New project; enable the Google Calendar API.
  - OAuth consent screen (Audience): External; fill app name + your email.
  - Publish app -> "In production" (skip verification for personal use).
  - Credentials -> OAuth client ID -> Desktop app -> download as credentials.json.

Deps (on BOTH laptop and Zo, in the venv):
  pip install google-auth google-auth-oauthlib google-api-python-client
"""

import os
import datetime

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
CREDS_FILE = "credentials.json"     # the OAuth client you downloaded (laptop)
TOKEN_FILE = "token.json"           # created on first auth; copy this to Zo


def get_credentials():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())              # silent, no browser (works on Zo)
    else:
        # No usable token -> interactive consent. LAPTOP ONLY (needs a browser).
        flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
        creds = flow.run_local_server(port=0)

    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    os.chmod(TOKEN_FILE, 0o600)               # holds a refresh token
    return creds


def main():
    creds = get_credentials()
    service = build("calendar", "v3", credentials=creds)

    # A test event: tomorrow, on the hour, for one hour (UTC).
    start = (datetime.datetime.now(datetime.timezone.utc)
             + datetime.timedelta(days=1)).replace(
                 minute=0, second=0, microsecond=0)
    end = start + datetime.timedelta(hours=1)
    event = {
        "summary": "Secretary smoke test",
        "description": "Created by gcal_smoke_test.py.",
        "start": {"dateTime": start.isoformat(), "timeZone": "UTC"},
        "end":   {"dateTime": end.isoformat(),   "timeZone": "UTC"},
    }
    created = service.events().insert(
        calendarId="primary", body=event).execute()
    print("Event created:", created.get("htmlLink"))


if __name__ == "__main__":
    main()