#!/usr/bin/env bash
# Client-profile auto-update: the minimal instance's side of "fixes flow
# one way" (owner commits to main -> client pulls).
#
# Runs every 15 minutes from clientpull.timer. If origin/main has new
# commits: fast-forward to it. That is the WHOLE job -- unlike the owner
# VM's autodeploy.sh there are no services to restart on this profile;
# Claude Code sessions read code per-use, so the next session simply runs
# the new version. --ff-only means a locally-edited checkout fails loudly
# here (journalctl -u clientpull) instead of being silently overwritten.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/../.."   # repo root

git fetch --quiet origin main
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
[ "$LOCAL" = "$REMOTE" ] && exit 0

git merge --ff-only --quiet origin/main
NEW=$(git rev-parse HEAD)
[ "$NEW" = "$LOCAL" ] ||
    echo "updated ${LOCAL:0:8} -> ${NEW:0:8} ($(git log -1 --format='%s'))"
