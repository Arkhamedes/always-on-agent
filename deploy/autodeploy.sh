#!/usr/bin/env bash
# Pull-based auto-deploy: the VM's side of "CI on git push".
#
# Runs every 2 minutes from autodeploy.timer. If origin/main has new commits:
# hard-reset the checkout to it and restart the services. Untracked files
# (secrets, agent.db, venv, logs) are never touched by reset --hard.
#
# Safety: a deploy is DEFERRED while the worker is mid-task (status='running'
# in agent.db) so a push never kills an in-flight coder build -- the timer
# simply retries two minutes later.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."   # repo root

git fetch --quiet origin main
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
[ "$LOCAL" = "$REMOTE" ] && exit 0

RUNNING=$(python3 - <<'EOF'
import sqlite3
try:
    con = sqlite3.connect("agent.db", timeout=5)
    print(con.execute("SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()[0])
except Exception:
    print(0)
EOF
)
if [ "$RUNNING" != "0" ]; then
    echo "deploy deferred: $RUNNING task(s) running (will retry)"
    exit 0
fi

echo "deploying ${LOCAL:0:8} -> ${REMOTE:0:8}"
git reset --hard --quiet origin/main

# Python deps ride the deploy: a requirements.txt change installs before the
# restart, so a new dependency never needs a manual SSH. (systemd units,
# sudoers, and apt packages remain by-hand -- they need root beyond the one
# scoped restart rule.)
if ! git diff --quiet "$LOCAL" "$REMOTE" -- requirements.txt; then
    echo "requirements.txt changed -- installing"
    ./venv/bin/pip install -q -r requirements.txt \
        || echo "pip install FAILED -- deploy continues, install by hand"
fi

sudo /usr/bin/systemctl restart agent dashboard
echo "deployed $(git log -1 --format='%h %s')"
