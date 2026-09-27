#!/usr/bin/env python3
"""
GitHub App token plumbing, shared by everything that talks to GitHub as the
agent: the coder's clone/push/PR, the reviewer's PR fetch, and the weekly
journal backup. No PAT lives on the box.

The App (id `GH_APP_ID`, private key file at `GH_APP_KEY`) is installed on
the target repos; `token_for()` mints a short-lived installation token
scoped to exactly one repo.
"""

import os
import time

import jwt
import requests


def _app_jwt() -> str:
    app_id = os.environ["GH_APP_ID"]
    private_key = open(os.environ["GH_APP_KEY"]).read()
    now = int(time.time())
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": app_id},
                      private_key, algorithm="RS256")


def token_for(repo: str, permissions=None) -> str:
    """Installation token for one repo. Default scopes cover the coder's
    push/PR; pass narrower `permissions` for read-only consumers (least
    privilege -- the reviewer must not hold a write token)."""
    headers = {"Authorization": f"Bearer {_app_jwt()}",
               "Accept": "application/vnd.github+json"}
    inst = requests.get(
        f"https://api.github.com/repos/{repo}/installation", headers=headers)
    inst.raise_for_status()
    tok = requests.post(
        f"https://api.github.com/app/installations/{inst.json()['id']}/access_tokens",
        headers=headers,
        json={"repositories": [repo.split("/")[1]],
              "permissions": permissions or {"contents": "write",
                                             "pull_requests": "write"}})
    tok.raise_for_status()
    return tok.json()["token"]
