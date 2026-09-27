#!/usr/bin/env python3
"""
Telegram intake -> orchestrator.

The listener is now a thin transport: it receives your messages (allowlisted)
and enqueues them; a dedicated BRAIN thread pulls them one at a time and runs
the orchestrator (decide + reply/dispatch). So the ~6-11s decide() call no
longer deafens the poll loop -- messages sent in quick succession are all
received within ~1s and then processed strictly in order. On startup it
recovers any task left 'running' by a crash.

The brain queue is IN-MEMORY by design: a crash loses messages that were
received but not yet decided (Telegram won't re-deliver past the advanced
offset). Accepted trade-off for low-stakes chat; persist the queue to the DB
if that ever stops being true.

Reads from the environment (loaded from ~/agent_env.sh):
  TELEGRAM_BOT_TOKEN        -- from BotFather
  TELEGRAM_ALLOWED_USER_ID  -- your numeric user id; every other sender is ignored

Run:   python telegram_listener.py
Stop:  Ctrl-C
"""

import os
import time
import queue
import threading
import requests

from coding_agent import load_env
from task_store import init_db, recover_orphans
from orchestrator import handle_message, init_orchestrator_db
from worker import run_worker_loop
from lifeos import init_lifeos_db, run_scheduler_loop, transcribe_voice
from mailwatch import init_mailwatch_db
from business_crm import init_business_crm_db
from claude_ops import init_claude_ops_db
import excel_pipeline
import librarian
import usage_limit
from agentlog import log

load_env()
init_db()
init_orchestrator_db()
init_lifeos_db()
init_mailwatch_db()
init_business_crm_db()
excel_pipeline.init_excel_pipeline_db()
librarian.init_librarian_db()
init_claude_ops_db()

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED = str(os.environ["TELEGRAM_ALLOWED_USER_ID"])
API = f"https://api.telegram.org/bot{TOKEN}"


def send(chat_id, text):
    try:
        requests.post(f"{API}/sendMessage",
                      json={"chat_id": chat_id, "text": text}, timeout=15)
    except requests.RequestException as e:
        log(f"send failed: {e}")


def send_document(chat_id, filename, data, caption=""):
    """Deliver a file to the chat (multipart sendDocument) -- the explainer's
    doc mode sends self-contained HTML this way. Telegram caps captions at
    1024 chars. Failures are logged, never raised, like send()."""
    try:
        requests.post(f"{API}/sendDocument",
                      data={"chat_id": chat_id,
                            "caption": (caption or "")[:1024]},
                      files={"document": (filename, data)}, timeout=60)
    except requests.RequestException as e:
        log(f"send_document failed: {e}")


BRAIN_QUEUE = queue.Queue()


def _voice_to_text(message):
    """Download a Telegram voice/audio note and transcribe it via Groq."""
    media = message.get("voice") or message.get("audio")
    info = requests.get(f"{API}/getFile",
                        params={"file_id": media["file_id"]}, timeout=15).json()
    path = info["result"]["file_path"]
    audio = requests.get(
        f"https://api.telegram.org/file/bot{TOKEN}/{path}", timeout=30).content
    return transcribe_voice(audio, filename=path.rsplit("/", 1)[-1])


def _download_document(message):
    """Download a Telegram document (getFile works up to ~20 MB). Returns
    (bytes, filename)."""
    doc = message["document"]
    info = requests.get(f"{API}/getFile",
                        params={"file_id": doc["file_id"]}, timeout=15).json()
    path = info["result"]["file_path"]
    data = requests.get(
        f"https://api.telegram.org/file/bot{TOKEN}/{path}", timeout=60).content
    return data, (doc.get("file_name") or path.rsplit("/", 1)[-1])


def handle(message):
    """Poll-loop side: validate and enqueue. Never blocks on the model.
    (Transcription is a quick bounded API call; acceptable on this thread.)"""
    chat_id = message["chat"]["id"]
    user_id = str(message.get("from", {}).get("id", ""))
    text = (message.get("text") or "").strip()

    # --- the safety line: only you can talk to the orchestrator -----------
    if user_id != ALLOWED:
        log(f"ignored message from user {user_id}")
        return
    # ---------------------------------------------------------------------
    if message.get("document"):
        doc_name = (message["document"].get("file_name") or "").lower()
        if doc_name.endswith(".xlsx") and excel_pipeline.enabled(chat_id):
            try:
                data, fname = _download_document(message)
                saved = excel_pipeline.ingest_telegram(
                    data, fname,
                    message["document"].get("file_unique_id", ""),
                    str(chat_id))
                if saved:
                    send(chat_id, f"Got {saved} -- syncing it into the CRM; "
                                  "I'll report what changed.")
                else:
                    send(chat_id, "I've ingested that exact workbook before "
                                  "-- ask me to sync it again if you need a "
                                  "re-run.")
            except Exception as e:
                log(f"excel ingest failed: {e}")
                send(chat_id, f"Couldn't ingest that workbook: {e}")
            return
        try:
            data, fname = _download_document(message)
            saved, indexed = librarian.save_upload(data, fname)
            tail = ("and indexed it." if indexed
                    else "(stored, but not text-searchable).")
            send(chat_id, f"Saved {saved} to your knowledge base {tail}")
        except Exception as e:
            log(f"document save failed: {e}")
            send(chat_id, f"Couldn't save that file: {e}")
        return
    if not text and (message.get("voice") or message.get("audio")):
        try:
            text = _voice_to_text(message)
            log(f"voice note transcribed: {text[:80]}")
            if not text:
                send(chat_id, "I couldn't hear anything in that voice note.")
                return
        except Exception as e:
            log(f"voice transcription failed: {e}")
            send(chat_id, f"Couldn't transcribe that voice note: {e}")
            return
    if not text:
        return

    log(f"received from you: {text[:80]}")
    BRAIN_QUEUE.put((chat_id, text))


def run_brain_loop():
    """Pull messages one at a time and run the orchestrator. SEQUENTIAL on
    purpose: the point is to unblock the listener, not to parallelize
    reasoning -- concurrent decides would scramble conversation order."""
    log("brain loop started")
    while True:
        chat_id, text = BRAIN_QUEUE.get()
        try:
            handle_message(chat_id, text, send)  # orchestrator sends the replies
        except Exception as e:
            send(chat_id, usage_limit.notice(e) or f"Orchestrator error: {e}")
            log(f"orchestrator error: {e}")
        finally:
            BRAIN_QUEUE.task_done()


def drain_backlog():
    try:
        r = requests.get(f"{API}/getUpdates", params={"timeout": 0}, timeout=15)
        ups = r.json().get("result", [])
        return (ups[-1]["update_id"] + 1) if ups else None
    except requests.RequestException:
        return None


def main():
    orphans = recover_orphans()
    if orphans:
        log(f"marked {len(orphans)} interrupted task(s) as failed: {orphans}")
    # Execution, deciding, AND the scheduler run on their own threads; the
    # poll loop below only receives and enqueues. In a Telegram private chat
    # the chat id equals the user id, so the allowlisted id is where
    # scheduled digests get sent.
    threading.Thread(target=run_worker_loop, args=(send, send_document),
                     daemon=True).start()
    threading.Thread(target=run_brain_loop, daemon=True).start()
    threading.Thread(target=run_scheduler_loop, args=(ALLOWED, send),
                     daemon=True).start()
    offset = drain_backlog()
    log("listener up; polling for messages")
    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(f"{API}/getUpdates", params=params, timeout=40)
            for update in r.json().get("result", []):
                offset = update["update_id"] + 1
                if "message" in update:
                    handle(update["message"])
        except requests.RequestException as e:
            log(f"poll error, retrying in 5s: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()