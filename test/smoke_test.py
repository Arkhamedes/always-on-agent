#!/usr/bin/env python3
"""
Plumbing smoke test for the autonomous coding agent.

Goal: prove the GitHub App can mint a repo-scoped token, push a branch, and
open a PR against a sandbox repo -- with NO AI involved. If a PR shows up in
the repo, the entire GitHub half of the loop is verified end to end.

Prerequisites (do these once):
  1. GitHub App created and installed on the sandbox repo.
  2. The App's private key (.pem) saved on this machine.
  3. Three env vars set (agent_env.sh provides them -- or run setup.py):
        export GH_APP_ID=123456                 # the App ID (NOT the Client ID)
        export GH_APP_KEY=/path/to/app.pem      # path to the private key file
        export CODER_REPO=owner/sandbox-repo    # a THROWAWAY repo -- it gets a real PR
  4. Dependencies:  pip install "pyjwt[crypto]" requests
        (the [crypto] extra is required -- RS256 needs the crypto backend)

Run:  python test/smoke_test.py
Expected output:  PR opened: https://github.com/<owner>/<sandbox-repo>/pull/N
"""

import os
import time
import uuid
import tempfile
import subprocess

import jwt
import requests

# --- config -----------------------------------------------------------------
APP_ID = os.environ["GH_APP_ID"]
PRIVATE_KEY = open(os.environ["GH_APP_KEY"]).read()
REPO = os.environ["CODER_REPO"]       # owner/name -- the App must be installed on it
BASE = "main"
# ----------------------------------------------------------------------------


def _app_jwt() -> str:
    """Short-lived JWT signed with the App private key. Identifies the App itself."""
    now = int(time.time())
    return jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": APP_ID},
        PRIVATE_KEY,
        algorithm="RS256",
    )


def token_for(repo: str) -> str:
    """Mint an installation token scoped to ONE repo + only the perms we need.
    Valid ~1 hour. This is the function the real worker will call per task."""
    headers = {
        "Authorization": f"Bearer {_app_jwt()}",
        "Accept": "application/vnd.github+json",
    }
    inst = requests.get(
        f"https://api.github.com/repos/{repo}/installation", headers=headers
    )
    inst.raise_for_status()
    installation_id = inst.json()["id"]

    tok = requests.post(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        headers=headers,
        json={
            "repositories": [repo.split("/")[1]],          # just Test_1
            "permissions": {"contents": "write", "pull_requests": "write"},
        },
    )
    tok.raise_for_status()
    return tok.json()["token"]


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True)


def main():
    token = token_for(REPO)
    branch = f"smoke-test-{uuid.uuid4().hex[:8]}"
    clone_url = f"https://x-access-token:{token}@github.com/{REPO}.git"

    with tempfile.TemporaryDirectory() as work:
        _git(["clone", clone_url, work], cwd=".")
        _git(["checkout", "-b", branch], cwd=work)

        # --- the trivial change (later: a real agent edits files here) -------
        with open(os.path.join(work, "SMOKE_TEST.md"), "w") as f:
            f.write(f"Plumbing verified at {time.ctime()}\n")
        # --------------------------------------------------------------------

        _git(["config", "user.name", "coding-agent[bot]"], cwd=work)
        _git(["config", "user.email",
              "coding-agent@users.noreply.github.com"], cwd=work)
        _git(["add", "-A"], cwd=work)
        _git(["commit", "-m", "smoke test: verify token + push + PR"], cwd=work)
        _git(["push", "origin", branch], cwd=work)

    pr = requests.post(
        f"https://api.github.com/repos/{REPO}/pulls",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        json={
            "title": "Smoke test PR",
            "head": branch,
            "base": BASE,
            "body": "Opened by smoke_test.py to verify the plumbing. Safe to close.",
        },
    )
    pr.raise_for_status()
    print("PR opened:", pr.json()["html_url"])


if __name__ == "__main__":
    main()