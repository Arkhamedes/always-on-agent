#!/usr/bin/env python3
"""
Background worker -- the execution half of intake/execution decoupling.

The listener only enqueues tasks (status 'queued') and acknowledges, staying
free to receive and reply. This loop runs on its own thread, pulls one runnable
task at a time, runs the right worker, and sends the result back to the chat.
So a long coder run no longer makes the bot deaf -- you can keep chatting, queue
more work, or fire a quick calendar task while a build grinds in here.

Runnable = 'queued' (a fresh task) or 'approved' (a coder plan you okayed).
One worker, FIFO: tasks run in order, one at a time.
"""

import time

from task_store import next_runnable_task
from coding_agent import process_task, execute_approved_task
from secretary import run_secretary_task
from researcher import run_research_task
from lifeos import run_digest_task
from finance import run_finance_task
from mailwatch import run_mail_task
from psychologist import run_psych_task
from librarian import run_librarian_task
from reviewer import run_review_task
from explainer import run_explainer_task
from idea_agent import run_ideas_task
from excel_pipeline import run_excel_task
from agentlog import log, timed
import usage_limit


# role -> handler. The single source of routing truth: run_task consults it
# and stage.py validates CLI roles against ROLES. Unknown roles fall through
# to the coder.
HANDLERS = {
    "secretary": run_secretary_task,
    "researcher": run_research_task,
    "news": run_research_task,
    "digest": run_digest_task,
    "planner": run_digest_task,
    "finance": run_finance_task,
    "mail": run_mail_task,
    "psychologist": run_psych_task,
    "librarian": run_librarian_task,
    "reviewer": run_review_task,
    "explainer": run_explainer_task,
    "ideas": run_ideas_task,
    "excel": run_excel_task,
}
ROLES = ("coder", *HANDLERS)


def run_task(task, notify=None):
    """Run one task to completion and return its result: a string, or a
    document envelope {"text": <caption>, "filename": <name>,
    "document": <bytes>} (the explainer's doc mode). Routing lives here so
    every driver -- the loop below, stage.py -- runs tasks identically.
    `notify(text)` streams per-milestone progress pings (ADR-0006)."""
    tid = task["id"]
    if task["status"] == "approved":
        with timed(f"execute approved {tid[:8]}"):
            return execute_approved_task(tid, notify=notify)
    role = task["role"] if task["role"] in HANDLERS else "coder"
    handler = HANDLERS.get(role, process_task)
    with timed(f"{role} {tid[:8]}"):
        return handler(tid)


def run_worker_loop(send, send_document=None, poll=3.0):
    """Poll for runnable tasks forever; run each and deliver its result.
    `send(chat_id, text)` is the listener's Telegram sender. A role may
    return a document envelope instead of a string (see run_task) --
    delivered via `send_document(chat_id, filename, data, caption)`."""
    log("worker loop started")
    while True:
        try:
            task = next_runnable_task()
        except Exception as e:
            log(f"worker: queue read failed: {e}")
            time.sleep(poll)
            continue

        if not task:
            time.sleep(poll)
            continue

        tid = task["id"]
        chat_id = task["source_ref"]
        try:
            # notify: per-milestone progress pings go straight to the chat
            # while the run is still going.
            notify = (lambda text: send(chat_id, text)) if chat_id else None
            result = run_task(task, notify=notify)
        except Exception as e:
            log(f"worker error on {tid[:8]}: {e}")
            result = (usage_limit.notice(e)
                      or f"Something went wrong running that task: {e}")

        if chat_id:
            if isinstance(result, dict) and send_document:
                send_document(chat_id, result.get("filename") or "answer.html",
                              result.get("document") or b"",
                              result.get("text") or "")
            elif isinstance(result, dict):
                send(chat_id, (result.get("text") or "Done.") +
                     " (This build can't deliver documents -- text only.)")
            else:
                send(chat_id, result)