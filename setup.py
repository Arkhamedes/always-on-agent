#!/usr/bin/env python3
"""
Interactive setup wizard -- turns a fresh clone into a configured agent.

Walks through every credential the agent needs, VALIDATES each one against
the real service the moment you enter it (the pain of setup is never typing
values in -- it's discovering twenty minutes later which one was wrong), then
writes ./agent_env.sh (chmod 600, gitignored). Re-running is safe: existing
values are offered as defaults and the old file is kept as agent_env.sh.bak.
Two profiles: FULL (the always-on Telegram agent) or MINIMAL (the client
instance -- Claude Code as the interface, no Telegram, no GitHub App; see
docs/extras/client-vm-deploy-key-setup.md).

Run it where the agent will run (the VM over SSH, or the laptop for a
staging setup -- docs/staging.md), after:
    python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
    ./venv/bin/python setup.py

The third-party ceremony it walks you through (one-time, ~20 minutes):
BotFather bot, GitHub App, Claude Max OAuth token, optionally Groq + Google.
"""

import os
import shutil
import stat
import subprocess
import sys

import envfile

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(REPO_DIR, "agent_env.sh")

try:
    import requests
except ImportError:
    sys.exit("Install dependencies first:\n"
             "  python3 -m venv venv && ./venv/bin/pip install -r requirements.txt\n"
             "  ./venv/bin/python setup.py")


def _head(title):
    print(f"\n=== {title} " + "=" * max(0, 56 - len(title)))


def _ask(label, default="", secret=False):
    """Prompt with the current/previous value as the Enter-default."""
    shown = "(hidden, Enter keeps it)" if secret and default else default
    suffix = f" [{shown}]" if default else ""
    val = input(f"{label}{suffix}: ").strip()
    return val or default


def _yes(label, default=True):
    d = "Y/n" if default else "y/N"
    val = input(f"{label} [{d}]: ").strip().lower()
    return default if not val else val.startswith("y")


# ---------------------------------------------------------------- telegram

def _setup_telegram(cfg):
    _head("Telegram (the interface)")
    print("No bot yet? Open Telegram, talk to @BotFather, send /newbot,\n"
         "and paste the token it gives you. One account can own many bots.")
    while True:
        token = _ask("TELEGRAM_BOT_TOKEN",
                     os.environ.get("TELEGRAM_BOT_TOKEN", ""), secret=True)
        if not token:
            print("  ! required -- the bot IS the interface")
            continue
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe",
                         timeout=15).json()
        if r.get("ok"):
            print(f"  ok -- bot @{r['result'].get('username')}")
            break
        print(f"  ! Telegram rejected that token: {r.get('description')}")
    cfg["TELEGRAM_BOT_TOKEN"] = token

    api = f"https://api.telegram.org/bot{token}"
    user_id = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "")
    print("\nYour numeric user id is the allowlist -- only it can talk to the "
         "agent.")
    if not user_id or not _yes(f"Keep allowed user id {user_id}?"):
        input("Send your bot any message now, then press Enter... ")
        user_id = ""
        for _ in range(3):
            upd = requests.get(f"{api}/getUpdates",
                               params={"offset": -1, "timeout": 10},
                               timeout=20).json()
            for u in reversed(upd.get("result") or []):
                frm = (u.get("message") or {}).get("from") or {}
                if frm.get("id"):
                    user_id = str(frm["id"])
                    print(f"  detected {frm.get('first_name', '?')} "
                         f"(id {user_id})")
                    break
            if user_id:
                break
            input("  nothing yet -- message the bot, then press Enter... ")
        user_id = _ask("TELEGRAM_ALLOWED_USER_ID", user_id)
    cfg["TELEGRAM_ALLOWED_USER_ID"] = user_id
    requests.post(f"{api}/sendMessage",
                  json={"chat_id": user_id,
                        "text": "Setup wizard connected -- this is your "
                                "agent's voice."}, timeout=15)
    print("  test message sent -- check your Telegram")


# ---------------------------------------------------------------- github

def _setup_github(cfg):
    _head("GitHub App (the coder's hands)")
    print("No App yet? github.com -> Settings -> Developer settings -> New\n"
         "GitHub App: any name, no webhook, repo permissions Contents +\n"
         "Pull requests = Read and write. Generate a private key (.pem),\n"
         "then INSTALL the App on the repos the agent may touch.")
    while True:
        app_id = _ask("GH_APP_ID (numeric App ID, NOT the Client ID)",
                      os.environ.get("GH_APP_ID", ""))
        key = _ask("GH_APP_KEY (absolute path to the .pem)",
                   os.environ.get("GH_APP_KEY", ""))
        key = os.path.expanduser(key)
        if not os.path.isfile(key):
            print(f"  ! no file at {key}")
            continue
        if "PRIVATE KEY" not in open(key).read():
            print("  ! that file doesn't look like a private key")
            continue
        repo = _ask("CODER_REPO (owner/name the coder targets by default -- "
                    "start\n  with a sandbox repo; you can rename it in chat "
                    "later)", os.environ.get("CODER_REPO", ""))
        os.environ.update(GH_APP_ID=app_id, GH_APP_KEY=key)
        try:
            from github_app import token_for
            token_for(repo)
            print(f"  ok -- minted an installation token for {repo}")
            break
        except Exception as e:
            print(f"  ! could not mint a token for {repo}: {e}")
            print("    401 = wrong App ID or key; 404 = the App is not "
                 "installed on that repo")
    cfg["GH_APP_ID"], cfg["GH_APP_KEY"] = app_id, key
    cfg["CODER_REPO"] = repo


# ---------------------------------------------------------------- claude

def _setup_claude(cfg):
    _head("Claude Code (the brain -- runs on a Claude Max plan)")
    if not shutil.which("claude"):
        print("  ! `claude` CLI not found on PATH. Install it first:\n"
             "    npm install -g @anthropic-ai/claude-code\n"
             "  (continuing -- the token can still be recorded)")
    print("Generate the token with:  claude setup-token")
    while True:
        tok = _ask("CLAUDE_CODE_OAUTH_TOKEN",
                   os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""), secret=True)
        if tok.startswith("sk-ant-oat"):
            break
        print("  ! expected an sk-ant-oat... token (NOT an sk-ant-api key -- "
             "API keys bill per token)")
    cfg["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    if shutil.which("claude") and _yes("Live-test it with one tiny claude -p "
                                       "call (~10s)?"):
        env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": tok}
        env.pop("ANTHROPIC_API_KEY", None)
        try:
            out = subprocess.run(
                ["claude", "-p", "Reply with exactly: ok", "--tools", "",
                 "--max-turns", "1", "--output-format", "json"],
                capture_output=True, text=True, timeout=90, env=env)
            print("  ok -- the model answered" if out.returncode == 0
                 else f"  ! claude -p failed: {(out.stderr or '')[:200]}")
        except subprocess.TimeoutExpired:
            print("  ! timed out -- check the token/plan, then rerun setup.py")


# ---------------------------------------------------------------- optional

def _setup_optional(cfg):
    _head("Groq (voice notes -> text; free tier -- Enter to skip)")
    key = _ask("GROQ_API_KEY (console.groq.com -> API Keys)",
               os.environ.get("GROQ_API_KEY", ""), secret=True)
    if key:
        r = requests.get("https://api.groq.com/openai/v1/models",
                         headers={"Authorization": f"Bearer {key}"},
                         timeout=15)
        print("  ok" if r.status_code == 200
             else f"  ! Groq answered {r.status_code} -- recorded anyway")
        cfg["GROQ_API_KEY"] = key
    else:
        print("  skipped -- voice notes will fail politely until set")

    _head("Journal backup (Enter to skip)")
    repo = _ask("JOURNAL_BACKUP_REPO (owner/name, App installed on it; the\n"
                "  weekly digest pushes journey's journal.db there)",
                os.environ.get("JOURNAL_BACKUP_REPO", ""))
    if repo:
        cfg["JOURNAL_BACKUP_REPO"] = repo

    _setup_persona(cfg)
    _google_status()


def _setup_persona(cfg):
    _head("Persona (a character voice -- Enter to skip)")
    print("Give the agent's replies a character. Tone only: facts, formats,\n"
         "and confirmations never change, and the coder/reviewer PR text\n"
         "stays neutral. Example:\n"
         "  JARVIS from Iron Man: polished, courteous, understated humor,\n"
         "  addresses the user as 'sir'")
    val = _ask("AGENT_PERSONA", os.environ.get("AGENT_PERSONA", ""))
    if val:
        cfg["AGENT_PERSONA"] = val
        print("  ok -- the agent will speak in that voice")
    else:
        print("  skipped -- neutral voice (rerun setup.py or edit "
             "agent_env.sh to add one later)")


def _google_status():
    _head("Google (calendar / gmail / drive -- separate one-time flow)")
    token = os.environ.get("GCAL_TOKEN",
                           os.path.join(REPO_DIR, "token.json"))
    if os.path.exists(token):
        print(f"  found {token} -- Google roles are live "
              "(verify: ./venv/bin/python test/gcal_smoke_test.py)")
    else:
        print("  no token.json yet. Google roles fail cleanly until you run\n"
             "  test/google_reauth.py on a machine with a browser and copy\n"
             "  token.json next to the code (scp it if the agent runs on a\n"
             "  VM; see docs/extras/gcp_setup_guide.md).")


# ------------------------------------------------- minimal client profile
#
# Mirrors docs/extras/client-vm-deploy-key-setup.md §5-§7: Claude Code is
# the interface (sessions in tmux / claude.ai), so there is no Telegram bot,
# no GitHub App, no always-on service. Exactly two credentials, both the
# client's own, plus the knowledge directory and the MCP wiring check.

def _setup_minimal(cfg):
    _head("Claude login (the interface AND the brain)")
    if not shutil.which("claude"):
        print("  ! `claude` CLI not found on PATH. Install it first (see the\n"
              "    client guide §4):  npm install -g @anthropic-ai/claude-code")
    print("Preferred: run `claude` once in a terminal and follow the\n"
          "browser-URL login -- your own subscription, nothing to paste here.\n"
          "Alternative: `claude setup-token` elsewhere, then enter the token.")
    tok = _ask("CLAUDE_CODE_OAUTH_TOKEN (Enter to skip if you used the "
               "browser login)",
               os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""), secret=True)
    if tok:
        if not tok.startswith("sk-ant-oat"):
            print("  ! expected an sk-ant-oat... token (NOT an sk-ant-api "
                  "key -- API keys bill per token). Recorded anyway.")
        cfg["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("  ! ANTHROPIC_API_KEY is set in this shell -- unset it; it\n"
              "    overrides the subscription login and bills metered rates")

    _google_status()

    _head("Knowledge base (a directory the librarian indexes)")
    kdir = os.path.expanduser(
        _ask("KNOWLEDGE_DIR", os.environ.get("KNOWLEDGE_DIR")
             or "~/knowledge"))
    if not os.path.isdir(kdir) and _yes(f"{kdir} doesn't exist -- create it?"):
        os.makedirs(kdir, exist_ok=True)
        print(f"  created {kdir}")
    if kdir != os.path.expanduser("~/knowledge"):
        cfg["KNOWLEDGE_DIR"] = kdir      # non-default only; default is baked in

    _setup_persona(cfg)

    _head("MCP wiring (knowledge + calendar tools inside Claude Code)")
    if shutil.which("claude"):
        try:
            out = subprocess.run(["claude", "mcp", "list"], cwd=REPO_DIR,
                                 capture_output=True, text=True, timeout=90)
            line = next((l for l in out.stdout.splitlines()
                         if l.startswith("agent:")), "")
            if "Connected" in line:
                print("  ok -- `agent` MCP server connects "
                      "(docs/subsystems/mcp.md)")
            elif "Pending approval" in line:
                print("  wired, one step left: start `claude` in this "
                      "directory once and\n  approve the project's `agent` "
                      "MCP server when prompted")
            else:
                print("  ! `claude mcp list` didn't show the agent server "
                      "connecting --\n    run it from the repo dir after "
                      "pip install; see docs/subsystems/mcp.md\n"
                      f"    {(line or out.stdout or out.stderr)[:200].strip()}")
        except subprocess.TimeoutExpired:
            print("  ! `claude mcp list` timed out -- check it by hand")
    else:
        print("  skipped -- install the claude CLI, then: claude mcp list")


# ---------------------------------------------------------------- write

def _write(cfg):
    if not cfg:
        print("\nNothing to record -- this profile needs no agent_env.sh "
              "(browser login + token.json are files, not env vars).")
        return
    if os.path.exists(ENV_FILE):
        if not _yes(f"\n{ENV_FILE} exists -- overwrite (old file kept as "
                    ".bak)?"):
            sys.exit("Nothing written.")
        shutil.copy2(ENV_FILE, ENV_FILE + ".bak")
    lines = ["# Written by setup.py -- rerun it to change values safely.",
             "# NEVER commit this file (gitignored)."]
    lines += [f'export {k}="{v}"' for k, v in cfg.items()]
    lines += ["", "# Never let a metered API key shadow the Max OAuth token.",
              "unset ANTHROPIC_API_KEY", ""]
    with open(ENV_FILE, "w") as f:
        f.write("\n".join(lines))
    os.chmod(ENV_FILE, stat.S_IRUSR | stat.S_IWUSR)
    print(f"\nwrote {ENV_FILE} (chmod 600)")


def main():
    print(__doc__.split("\n\n")[0])
    # Existing values become the Enter-defaults on a re-run.
    envfile.load("~/agent_env.sh", ENV_FILE)
    cfg = {}
    _head("Profile")
    print("full    -- the always-on Telegram agent: bot, GitHub App, worker,\n"
          "           scheduled digests (the owner's setup)\n"
          "minimal -- Claude Code as the interface: knowledge base, calendar,\n"
          "           research; no Telegram, no GitHub App, nothing always-on\n"
          "           (docs/extras/client-vm-deploy-key-setup.md)")
    if _yes("Set up the FULL instance?"):
        _setup_telegram(cfg)
        _setup_github(cfg)
        _setup_claude(cfg)
        _setup_optional(cfg)
        _write(cfg)
        _head("Next steps")
        print("  1. Prove the GitHub plumbing end to end (opens a real PR on "
             f"{cfg.get('CODER_REPO', 'your sandbox')}):\n"
             "       ./venv/bin/python test/smoke_test.py\n"
             "  2. On a laptop: drive it without Telegram -- docs/staging.md\n"
             "       ./venv/bin/python stage.py chat\n"
             "  3. Run the real thing:  ./run_listener.sh\n"
             "  4. Make it 24/7 on a VM: docs/extras/gcp_setup_guide.md + "
             "deploy/*.service")
    else:
        _setup_minimal(cfg)
        _write(cfg)
        _head("Next steps (client guide 6-7)")
        print("  1. Work in tmux so sessions survive disconnects:\n"
              "       tmux new -s code    then:  claude\n"
              "  2. Optional, phone-first: the remote-control service lets the\n"
              "     claude.ai app open sessions on this machine directly --\n"
              "     install steps in deploy/remote-control.service\n"
              "  3. Stay up to date hands-free: install the pull-only timer\n"
              "     (steps in deploy/client/clientpull.service) -- or plain\n"
              "     `git pull`. Do NOT install deploy/autodeploy.* here.")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        sys.exit("\nAborted -- nothing written.")
