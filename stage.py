#!/usr/bin/env python3
"""
Laptop staging driver -- run the agent end to end with NO Telegram bot.

Staging is a laptop checkout, not a second VM (docs/adr/0009). This driver
replaces the listener: messages go in as CLI arguments, replies come out on
stdout, and everything between -- orchestrator decide(), instant actions,
task queue, worker roles, real claude -p calls, real GitHub/Google APIs --
is the production code path (worker.run_task is the same function the VM
loop calls).

Isolation: the DB is ALWAYS forced to staging.db (or STAGING_DB_PATH), so a
staging run can never touch agent.db. Point CODER_REPO at a sandbox repo in
agent_env.staging.sh -- coder tasks open real PRs.

Env loading order (later wins): ~/agent_env.sh, ./agent_env.sh,
./agent_env.staging.sh. Copy agent_env.staging.sh.example to get started.

Usage:
  python3 stage.py chat "message"         one full orchestrator turn, then
                                          run any tasks it dispatched
  python3 stage.py chat                   interactive chat loop (Ctrl-C ends)
  python3 stage.py <role> "instruction"   enqueue + run ONE role directly;
                                          non-coder roles expect their
                                          instruction JSON (see the dispatch
                                          arm in orchestrator.handle_message)
  python3 stage.py drain                  run whatever is already queued

Roles: whatever worker.py routes (worker.ROLES) -- coder, secretary,
       researcher, news, digest, finance, mail, psychologist, librarian,
       reviewer, explainer at the time of writing.
"""

import os
import sys

import envfile   # leaf module: safe to import before AGENT_DB_PATH is pinned

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
CHAT_ID = "staging"


def _setup_env():
    envfile.load("~/agent_env.sh",
                 os.path.join(REPO_DIR, "agent_env.sh"),
                 os.path.join(REPO_DIR, "agent_env.staging.sh"))
    # Staging NEVER touches agent.db. Set before any project import --
    # task_store reads AGENT_DB_PATH at import time.
    os.environ["AGENT_DB_PATH"] = os.environ.get(
        "STAGING_DB_PATH", os.path.join(REPO_DIR, "staging.db"))


def _send(chat_id, text):
    print(f"\n[bot] {text}")


def _deliver(result):
    """Print a role result; document envelopes are written next to the DB."""
    if isinstance(result, dict):
        name = result.get("filename") or "answer.html"
        path = os.path.join(REPO_DIR, f"staging_{name}")
        with open(path, "wb") as f:
            f.write(result.get("document") or b"")
        print(f"\n[bot] {result.get('text') or ''}\n[document -> {path}]")
    else:
        print(f"\n[result] {result}")


def _drain():
    """Run queued/approved tasks until the queue is empty, like the worker
    loop would -- but synchronously, delivering to stdout."""
    from task_store import next_runnable_task
    from worker import run_task
    ran = 0
    while True:
        task = next_runnable_task()
        if not task:
            break
        ran += 1
        print(f"[staging] running {task['role']} {task['id'][:8]} ...")
        try:
            _deliver(run_task(task, notify=lambda t: print(f"[ping] {t}")))
        except Exception as e:
            print(f"[staging] task {task['id'][:8]} failed: {e}")
    if not ran:
        print("[staging] queue empty")


def _turn(message):
    from orchestrator import handle_message
    handle_message(CHAT_ID, message, _send)
    _drain()


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    _setup_env()

    # Project imports only from here on (AGENT_DB_PATH is pinned). The valid
    # role set comes from the worker's own routing table -- never a copy.
    from worker import ROLES
    if sys.argv[1] not in (*ROLES, "chat", "drain"):
        sys.exit(__doc__)

    from task_store import init_db, create_task, get_task
    from orchestrator import init_orchestrator_db
    from lifeos import init_lifeos_db
    from mailwatch import init_mailwatch_db
    from business_crm import init_business_crm_db
    from excel_pipeline import init_excel_pipeline_db
    from claude_ops import init_claude_ops_db
    import librarian
    init_db()
    init_orchestrator_db()
    init_lifeos_db()
    init_mailwatch_db()
    init_business_crm_db()
    init_excel_pipeline_db()
    librarian.init_librarian_db()
    init_claude_ops_db()
    print(f"[staging] db: {os.environ['AGENT_DB_PATH']}")

    mode = sys.argv[1]
    if mode == "drain":
        _drain()
    elif mode == "chat":
        if len(sys.argv) > 2:
            _turn(sys.argv[2])
        else:
            try:
                while True:
                    _turn(input("\n[you] "))
            except (KeyboardInterrupt, EOFError):
                print()
    else:
        if len(sys.argv) < 3:
            sys.exit(f'Usage: python3 stage.py {mode} "instruction"')
        from worker import run_task
        task_id = create_task(source="staging", source_ref=None, role=mode,
                              repo=os.environ.get("CODER_REPO", ""),
                              instruction=sys.argv[2])
        _deliver(run_task(get_task(task_id)))


if __name__ == "__main__":
    main()
