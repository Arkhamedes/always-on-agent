#!/usr/bin/env bash
# Launch the Telegram listener + background worker.
#
# Works from an interactive shell AND from a systemd service. systemd runs with
# a BARE environment (no venv active, minimal PATH), so this script rebuilds
# what's needed: cd to the repo, put node/claude on PATH, load secrets, and run
# the venv's Python UNBUFFERED so a frozen process still flushes its last line.

set -euo pipefail

# Always run from the repo directory (where this script lives).
cd "$(dirname "$(readlink -f "$0")")"

# --- Put node + the `claude` CLI on PATH -----------------------------------
# systemd's PATH is minimal, so `claude` won't be found unless you add it.
# Find where it lives with:   which claude
# Then uncomment/edit ONE of the lines below to match your install:
#
#   export PATH="$HOME/.nvm/versions/node/vXX.X.X/bin:$PATH"   # nvm
#   export PATH="/usr/local/bin:$PATH"                          # system-wide
#
# If nvm is present, this loads it automatically:
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
if [ -s "$NVM_DIR/nvm.sh" ]; then source "$NVM_DIR/nvm.sh"; fi

# --- Load secrets (copy agent_env.sh.example -> agent_env.sh first) ---------
source ./agent_env.sh

# Never let a metered API key shadow the Max OAuth token.
unset ANTHROPIC_API_KEY

# --- Run --------------------------------------------------------------------
exec ./venv/bin/python -u telegram_listener.py >> agent.log 2>&1