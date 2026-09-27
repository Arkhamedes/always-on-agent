#!/usr/bin/env python3
"""
ClickUp POC smoke test -- prove the bridge end to end.

Live mode (needs CLICKUP_API_TOKEN in agent_env.sh):
    ./venv/bin/python test/clickup_smoke_test.py
  Steps: token -> whoami -> workspaces -> recent activity -> model digest.

Mock mode (no token, no network -- proves the summarize pipeline today):
    ./venv/bin/python test/clickup_smoke_test.py --mock
  Feeds a fixture through clickup.summarize() and prints the digest.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import envfile  # noqa: E402

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
envfile.load("~/agent_env.sh", os.path.join(REPO_DIR, "agent_env.sh"))

import clickup  # noqa: E402

FIXTURE = {
    "user": {"id": 1, "username": "bryan", "email": "bryan@example.com"},
    "workspaces": [{"id": "9001", "name": "Acme Robotics"}],
    "days": 7,
    "tasks": [
        {"id": "abc1", "name": "Ship staging pipeline", "status": "in progress",
         "url": "https://app.clickup.com/t/abc1", "updated": "2026-07-11 09:12",
         "workspace": "Acme Robotics",
         "comments": [
             {"who": "maria", "when": "2026-07-11 09:12", "mentions_me": True,
              "text": "@bryan can you confirm the deploy key is rotated "
                      "before Friday?"},
             {"who": "bryan", "when": "2026-07-10 18:40", "mentions_me": False,
              "text": "Pipeline green on the last three runs."}]},
        {"id": "abc2", "name": "Q3 sensor budget", "status": "review",
         "url": "https://app.clickup.com/t/abc2", "updated": "2026-07-09 14:02",
         "workspace": "Acme Robotics",
         "comments": [
             {"who": "dan", "when": "2026-07-09 14:02", "mentions_me": False,
              "text": "Moved to review, finance signed off."}]},
    ],
    "chat": [
        {"channel": "general", "who": "maria", "when": "2026-07-11 10:01",
         "mentions_me": True,
         "text": "Reminder: @bryan owns the demo slot at Tuesday standup."},
        {"channel": "general", "who": "dan", "when": "2026-07-11 08:30",
         "mentions_me": False, "text": "Coffee machine fixed."},
    ],
}


def main():
    if "--mock" in sys.argv[1:]:
        print("== mock mode: fixture -> clickup.summarize() ==")
        digest = clickup.summarize(FIXTURE)
        print(f"\n{digest}\n")
        for needle in ("deploy key", "demo"):
            status = "ok" if needle.lower() in digest.lower() else "MISSING"
            print(f"  [{status}] digest covers the '{needle}' mention")
        return

    if not os.environ.get(clickup.TOKEN_ENV):
        sys.exit(f"! {clickup.TOKEN_HINT}\n"
                 "  (or run with --mock to prove the pipeline without one)")

    me = clickup.whoami()
    print(f"ok -- token belongs to {me['username']} ({me['email']})")
    teams = clickup.workspaces()
    print(f"ok -- {len(teams)} workspace(s): "
          f"{', '.join(t['name'] for t in teams)}")
    activity = clickup.recent_activity(days=7)
    mentions = sum(c.get("mentions_me", False)
                   for t in activity["tasks"] for c in t["comments"])
    mentions += sum(m.get("mentions_me", False) for m in activity["chat"])
    print(f"ok -- {len(activity['tasks'])} assigned task(s), "
          f"{len(activity['chat'])} chat message(s), "
          f"{mentions} mention(s) of you")
    print("\n== model digest ==")
    print(clickup.summarize(activity))


if __name__ == "__main__":
    main()
