#!/usr/bin/env bash
# Launch the Claude Code remote-control server: the claude.ai app creates
# sessions ON THIS MACHINE on demand (no SSH per session). Runs from
# deploy/remote-control.service; also works from an interactive shell.
#
# Sessions spawn in the agent checkout (same-dir), so each one gets the MCP
# tools (.mcp.json) against the live agent.db. Capacity is deliberately tiny:
# every concurrent session is a Node process, and this box has 1 GB RAM.
#
# Auth: needs the FULL-SCOPE stored login (one-time `claude` -> /login on the
# box). CLAUDE_CODE_OAUTH_TOKEN is dropped below -- it is inference-only,
# outranks the stored login, and blocks remote control. The always-on agent
# service keeps using it; only this server and kc run on the stored login.

set -euo pipefail

# Always run from the repo directory (where this script lives).
cd "$(dirname "$(readlink -f "$0")")"

# systemd's PATH is minimal; put node + `claude` on it (same as run_listener.sh).
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
if [ -s "$NVM_DIR/nvm.sh" ]; then source "$NVM_DIR/nvm.sh"; fi

# Load secrets, then drop the tokens that must not reach interactive sessions.
# The minimal client profile may legitimately have no env file (setup.py
# writes one only when something needs recording) -- don't die on it.
[ -f ./agent_env.sh ] && source ./agent_env.sh
unset ANTHROPIC_API_KEY
unset CLAUDE_CODE_OAUTH_TOKEN

exec claude remote-control \
    --spawn same-dir \
    --capacity "${REMOTE_CONTROL_CAPACITY:-3}"
