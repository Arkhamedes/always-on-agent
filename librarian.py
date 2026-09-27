#!/usr/bin/env python3
"""
The librarian: your personal knowledge base -- search and ask over your own files.

Files live on disk under KNOWLEDGE_DIR (default ~/knowledge), gitignored and
OUTSIDE the repo checkout, so auto-deploy's `git reset --hard` never touches
them. The index is a SQLite FTS5 table inside agent.db -- keyword retrieval with
an inverted index, near-zero idle RAM, NO local embedding model and NO vector
store (see docs/adr/0003). Every "thinking" step stays an on-demand `claude -p`
call, exactly like every other role -- nothing runs resident on the 1 GB box.

Three ops, dispatched through the worker (read-only, like the researcher):
  {"op": "search", "query": "<words>"}     -> ranked snippets + file names
  {"op": "ask",    "query": "<question>"}  -> a grounded answer over the top
                                              chunks, framed around who you are
                                              (blends the psychologist profile
                                              + pinned facts).
  {"op": "drive",  "query": "<name words>"} -> Google Drive files matched BY
                                              NAME, returned as webViewLinks
                                              (ADR-0007: metadata-only scope,
                                              this process cannot read file
                                              contents; sensitive documents
                                              live in Drive, never here).

Ingestion (no worker task -- runs where it's called):
  - save_note(text, title): "remember this: ..." -> a note file in the store
  - save_upload(data, filename): a Telegram document -> a file in the store
  - reindex(): walk the store, (re)index files changed since last run (by mtime)

Management (also instant -- filesystem-only, so it never queues behind a
long coder build):
  - list_report(): what the store holds, newest first (knowledge_list action)
  - delete_file(name): remove one stored file by exact name (knowledge_delete
    action; the orchestrator confirms with the user first -- it's permanent)

Retrieval is keyword FTS5. If a SQLite build ever lacks FTS5, we fall back to a
plain table + LIKE search so the feature still works (degraded ranking).
"""

import os
import re
import json
import shutil
import sqlite3
import datetime
import subprocess

from task_store import DB_PATH, get_task, update_task
from agentlog import log
from claude_ops import run_claude
import persona
import usage_limit

# --- store + index tuning (all sized for the 1 GB box) -------------------
KNOWLEDGE_DIR = os.environ.get(
    "KNOWLEDGE_DIR", os.path.expanduser("~/knowledge"))
MAX_FILE_BYTES = 1_000_000    # cap text indexed per file (bounds prompt + index)
CHUNK_SIZE = 1500             # chars per indexed/retrieved chunk
RETRIEVE_K = 6                # chunks fed into an `ask` answer
SEARCH_K = 8                  # snippets returned by a `search`
MESSAGE_CAP = 3900            # Telegram hard limit is 4096
CLAUDE_TIMEOUT = 120

# Extensions we try to read as text. Anything else is stored but not indexed
# (a PDF is indexed only if the `pdftotext` binary happens to be installed).
TEXT_EXTS = {
    ".md", ".markdown", ".txt", ".text", ".rst", ".org", ".csv", ".tsv",
    ".json", ".yaml", ".yml", ".ini", ".cfg", ".toml", ".log", ".html",
    ".py", ".js", ".ts", ".sh", ".sql", ".c", ".h", ".go", ".rs",
}

_HAVE_FTS = None   # resolved on first init_librarian_db()


# ----------------------------------------------------------------- db plumbing

def _conn():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_librarian_db():
    """Create the file-tracking table and the search index. Detects whether
    this SQLite build has FTS5 and remembers it for the search path."""
    global _HAVE_FTS
    con = _conn()
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS knowledge_files ("
            " path TEXT PRIMARY KEY,"
            " mtime REAL NOT NULL,"
            " chunks INTEGER NOT NULL DEFAULT 0,"
            " updated_at TEXT NOT NULL)")
        try:
            con.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5("
                " path UNINDEXED, chunk_no UNINDEXED, body,"
                " tokenize='porter unicode61')")
            _HAVE_FTS = True
        except sqlite3.OperationalError:
            con.execute(
                "CREATE TABLE IF NOT EXISTS knowledge_chunks ("
                " path TEXT NOT NULL, chunk_no INTEGER NOT NULL, body TEXT NOT NULL)")
            _HAVE_FTS = False
            log("librarian: FTS5 unavailable -- using LIKE fallback")
        con.commit()
    finally:
        con.close()


def _fts():
    """True if the search index is FTS5. Lazily inits if needed."""
    if _HAVE_FTS is None:
        init_librarian_db()
    return _HAVE_FTS


# ----------------------------------------------------------------- ingestion

def _ensure_dir():
    os.makedirs(KNOWLEDGE_DIR, exist_ok=True)


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60] or "note"


def _stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _safe_name(filename):
    """A filename safe to write inside the store: basename only, no traversal."""
    base = os.path.basename(filename or "").strip() or "upload"
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "upload"
    return base[:120]


def _within_store(path):
    root = os.path.realpath(KNOWLEDGE_DIR)
    return os.path.realpath(path).startswith(root + os.sep)


def save_note(text, title=None):
    """Persist a free-text note to the store as markdown, then reindex.
    Returns the filename written."""
    _ensure_dir()
    first_line = (text or "").strip().split("\n", 1)[0]
    fname = f"{_stamp()}-{_slug(title or first_line)}.md"
    path = os.path.join(KNOWLEDGE_DIR, fname)
    body = f"# {title}\n\n{text}" if title else (text or "")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    reindex()
    return fname


def save_upload(data, filename):
    """Persist raw bytes (a Telegram document) to the store, then reindex.
    Returns (saved_filename, indexed_bool). indexed is False for files we
    can't read as text (they're stored, just not searchable)."""
    _ensure_dir()
    name = _safe_name(filename)
    path = os.path.join(KNOWLEDGE_DIR, name)
    if os.path.exists(path):
        stem, ext = os.path.splitext(name)
        name = f"{stem}-{_stamp()}{ext}"
        path = os.path.join(KNOWLEDGE_DIR, name)
    if not _within_store(path):
        raise ValueError("refusing to write outside the knowledge store")
    with open(path, "wb") as f:
        f.write(data)
    reindex()
    row = _file_row(path)
    return name, bool(row and row["chunks"] > 0)


# ----------------------------------------------------------------- text extract

def _pdftotext(path):
    """Extract text from a PDF via poppler's `pdftotext`, if it's installed.
    No Python dependency -- returns None when the binary is absent."""
    if not shutil.which("pdftotext"):
        return None
    try:
        proc = subprocess.run(["pdftotext", "-q", path, "-"],
                              capture_output=True, text=True, timeout=60)
        return proc.stdout if proc.returncode == 0 else None
    except Exception:
        return None


def _extract_text(path):
    """Best-effort text of a file, or None if it isn't indexable as text."""
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None
    raw = raw[:MAX_FILE_BYTES]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if ext == ".pdf":
        return _pdftotext(path)
    if ext in TEXT_EXTS:
        return raw.decode("latin-1", "replace")
    return None


def _chunk(text):
    """Split text into ~CHUNK_SIZE pieces on paragraph boundaries."""
    text = (text or "").strip()
    if not text:
        return []
    chunks, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(cur) + len(para) + 2 <= CHUNK_SIZE:
            cur = f"{cur}\n\n{para}".strip()
        else:
            if cur:
                chunks.append(cur)
            if len(para) <= CHUNK_SIZE:
                cur = para
            else:
                for i in range(0, len(para), CHUNK_SIZE):
                    chunks.append(para[i:i + CHUNK_SIZE])
                cur = ""
    if cur:
        chunks.append(cur)
    return chunks


# ----------------------------------------------------------------- indexing

def _iter_files():
    for root, _dirs, files in os.walk(KNOWLEDGE_DIR):
        for name in files:
            if name.startswith("."):
                continue
            yield os.path.join(root, name)


def _file_row(path):
    con = _conn()
    try:
        return con.execute("SELECT * FROM knowledge_files WHERE path=?",
                           (path,)).fetchone()
    finally:
        con.close()


def _delete_path(con, path):
    con.execute("DELETE FROM knowledge_files WHERE path=?", (path,))
    if _fts():
        con.execute("DELETE FROM knowledge_fts WHERE path=?", (path,))
    else:
        con.execute("DELETE FROM knowledge_chunks WHERE path=?", (path,))


def _write_chunks(con, path, chunks):
    tbl = "knowledge_fts" if _fts() else "knowledge_chunks"
    con.executemany(
        f"INSERT INTO {tbl} (path, chunk_no, body) VALUES (?,?,?)",
        [(path, i, c) for i, c in enumerate(chunks)])


def reindex():
    """Bring the index in line with the store. Incremental: only files whose
    mtime changed are re-read; deleted files are dropped. Cheap to call before
    every query -- a stat() per file, no model, no network."""
    if not os.path.isdir(KNOWLEDGE_DIR):
        return
    con = _conn()
    try:
        known = {r["path"]: r["mtime"]
                 for r in con.execute("SELECT path, mtime FROM knowledge_files")}
        seen = set()
        for path in _iter_files():
            seen.add(path)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if path in known and abs(known[path] - mtime) < 1e-6:
                continue
            text = _extract_text(path)
            chunks = _chunk(text) if text else []
            _delete_path(con, path)
            if chunks:
                _write_chunks(con, path, chunks)
            con.execute(
                "INSERT INTO knowledge_files (path, mtime, chunks, updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                "mtime=excluded.mtime, chunks=excluded.chunks, "
                "updated_at=excluded.updated_at",
                (path, mtime, len(chunks), _now()))
        for gone in set(known) - seen:
            _delete_path(con, gone)
        con.commit()
    finally:
        con.close()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def file_count():
    con = _conn()
    try:
        row = con.execute("SELECT COUNT(*) AS n FROM knowledge_files").fetchone()
        return row["n"] if row else 0
    finally:
        con.close()


# ----------------------------------------------------------------- retrieval

def _fts_query(text):
    """Turn free text into a safe FTS5 MATCH string: each word as a quoted
    literal, OR-joined. Quoting neutralizes FTS operators/punctuation."""
    words = re.findall(r"[A-Za-z0-9]+", text or "")
    words = [w for w in words if len(w) > 1]
    if not words:
        return None
    return " OR ".join(f'"{w}"' for w in words)


def _retrieve(query, k=RETRIEVE_K):
    """Top-k chunk bodies for a query, as [{path, body}]."""
    con = _conn()
    try:
        if _fts():
            q = _fts_query(query)
            if not q:
                return []
            rows = con.execute(
                "SELECT path, body FROM knowledge_fts WHERE knowledge_fts MATCH ? "
                "ORDER BY bm25(knowledge_fts) LIMIT ?", (q, k)).fetchall()
        else:
            rows = _like_rows(con, query, k, "body")
        return [{"path": r["path"], "body": r["body"]} for r in rows]
    finally:
        con.close()


def _search(query, k=SEARCH_K):
    """Ranked snippets for a query, as [{path, snippet}]."""
    con = _conn()
    try:
        if _fts():
            q = _fts_query(query)
            if not q:
                return []
            rows = con.execute(
                "SELECT path, snippet(knowledge_fts, 2, '', '', ' … ', 12) AS snip "
                "FROM knowledge_fts WHERE knowledge_fts MATCH ? "
                "ORDER BY bm25(knowledge_fts) LIMIT ?", (q, k)).fetchall()
            return [{"path": r["path"], "snippet": r["snip"]} for r in rows]
        rows = _like_rows(con, query, k, "body")
        return [{"path": r["path"], "snippet": r["body"][:160]} for r in rows]
    finally:
        con.close()


def _like_rows(con, query, k, col):
    """FTS5-free fallback: match rows containing ANY query word (LIKE)."""
    words = re.findall(r"[A-Za-z0-9]+", query or "")
    words = [w for w in words if len(w) > 1][:8]
    if not words:
        return []
    clause = " OR ".join(f"{col} LIKE ?" for _ in words)
    vals = [f"%{w}%" for w in words] + [k]
    return con.execute(
        f"SELECT path, body FROM knowledge_chunks WHERE {clause} LIMIT ?",
        vals).fetchall()


# ----------------------------------------------------------------- drive find

DRIVE_K = 8   # most Drive matches returned per query


def _drive_service():
    """Drive v3 client on token.json's OWN scopes (no scope filter, like
    secretary.free_busy) -- the metadata-only grant arrives when
    test/google_reauth.py re-mints the token (ADR-0007). Lazy imports so
    the google libs load only when the op is actually used."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from secretary import TOKEN_FILE
    creds = Credentials.from_authorized_user_file(TOKEN_FILE)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("drive", "v3", credentials=creds)


def drive_find(query, k=DRIVE_K):
    """Find files in the user's Google Drive BY NAME; reply with links only.
    The metadata-only scope is the security property (ADR-0007): even a
    compromised box could learn names, never contents -- and the reply's
    webViewLink opens under the USER's Google login, so Telegram carries a
    URL that is useless to anyone else. Tidy Drive naming is the contract:
    every query word must appear in the file name."""
    words = (query or "").split()
    if not words:
        return "Give me a file name (or part of one) to look for in Drive."
    escaped = [w.replace("\\", "\\\\").replace("'", "\\'") for w in words]
    q = " and ".join(f"name contains '{w}'" for w in escaped)
    q += " and trashed=false"
    try:
        resp = _drive_service().files().list(
            q=q, pageSize=k, orderBy="modifiedTime desc",
            fields="files(name,mimeType,webViewLink,modifiedTime)").execute()
    except Exception as e:
        msg = str(e)
        hint = ("Drive access needs the drive.metadata.readonly scope -- "
                "re-run test/google_reauth.py on the laptop, update "
                "token.json, and make sure the Drive API is enabled in the "
                "Google Cloud project."
                if any(s in msg.lower() for s in
                       ("insufficient", "403", "accessnotconfigured",
                        "scope")) else msg[:300])
        return f"Couldn't search Drive: {hint}"
    files = resp.get("files", [])
    if not files:
        return (f"No Drive files matching '{query}'. I search file NAMES "
                "only -- try part of the exact name.")
    lines = ["From your Drive (links open under your Google login; I can't "
             "read the contents):", ""]
    for f in files:
        stamp = (f.get("modifiedTime") or "")[:10]
        lines.append(f"• {f['name']}" + (f" ({stamp})" if stamp else ""))
        if f.get("webViewLink"):
            lines.append(f"  {f['webViewLink']}")
    return _cap("\n".join(lines))


# ----------------------------------------------------------------- personalize

def _about_user(chat_id):
    """The 'knows me' layer: the psychologist's rolling profile + pinned facts,
    read directly (read-only) -- the sanctioned exception to table ownership,
    same pattern as lifeos._facts_tz / dashboard._fact."""
    if not chat_id:
        return ""
    con = _conn()
    try:
        parts = []
        try:
            row = con.execute("SELECT profile FROM psych_profile WHERE chat_id=?",
                              (str(chat_id),)).fetchone()
            if row and row["profile"]:
                parts.append("Profile:\n" + row["profile"])
        except sqlite3.OperationalError:
            pass
        try:
            facts = con.execute(
                "SELECT key, value FROM facts WHERE chat_id=? AND key IN "
                "('timezone','repo','finance_sheet')", (str(chat_id),)).fetchall()
            if facts:
                parts.append("Facts: " + ", ".join(f"{r['key']}={r['value']}"
                                                    for r in facts))
        except sqlite3.OperationalError:
            pass
        return "\n".join(parts)
    finally:
        con.close()


ANSWER_PROMPT = """You are the user's personal librarian. Answer their question using (a) EXCERPTS retrieved from their OWN saved files and (b) what you know ABOUT THE USER below.

Rules:
- Ground the answer in the excerpts; name the file(s) you drew from.
- Use "About the user" to frame the answer for them -- but never invent facts the excerpts don't support.
- If the excerpts don't actually answer it, say so plainly and name the files you do have on the topic.
- PLAIN TEXT only, no markdown formatting (this goes to a Telegram message). Concise and direct.

About the user:
{about}

Excerpts from the user's files:
{excerpts}

Question: {question}
"""


def _claude_answer(question, about, excerpts):
    prompt = ANSWER_PROMPT.format(
        about=about or "(nothing on file)", excerpts=excerpts,
        question=question) + persona.line()
    data = run_claude(
        ["--tools", "", "--max-turns", "1"],
        cwd=os.path.dirname(os.path.abspath(__file__)), prompt=prompt,
        timeout=CLAUDE_TIMEOUT, label="librarian ask", role="librarian")
    if data.get("is_error"):
        raise RuntimeError(f"librarian answer did not finish cleanly:\n{data}")
    return data.get("result", "").strip()


# ----------------------------------------------------------------- public ops

def _cap(text, limit=MESSAGE_CAP):
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    return text[:cut if cut > 0 else limit].rstrip()


def search(query):
    reindex()
    hits = _search(query)
    if not hits:
        return f"No matches in your knowledge base for that. ({file_count()} file(s) indexed.)"
    lines = ["From your knowledge base:", ""]
    for h in hits:
        lines.append(f"• {os.path.basename(h['path'])}")
        snip = (h["snippet"] or "").strip()
        if snip:
            lines.append(f"  {snip}")
    return _cap("\n".join(lines))


def ask(question, chat_id=None):
    reindex()
    chunks = _retrieve(question)
    if not chunks:
        return (f"I don't have anything in your knowledge base about that. "
                f"({file_count()} file(s) indexed.)")
    excerpts = "\n\n".join(
        f"[{os.path.basename(c['path'])}]\n{c['body']}" for c in chunks)
    answer = _claude_answer(question, _about_user(chat_id), excerpts)
    return _cap(answer)


def run_librarian_task(task_id):
    """Process one librarian task. Same state shape as the other workers."""
    task = get_task(task_id)
    if not task:
        return f"Task {task_id} not found."

    update_task(task_id, status="running", inc_attempts=True)
    try:
        op = json.loads(task["instruction"])
        query = (op.get("query") or "").strip()
        if not query:
            raise ValueError("empty query")
        if op.get("op") == "search":
            answer = search(query)
        elif op.get("op") == "drive":
            answer = drive_find(query)
        else:
            answer = ask(query, task.get("source_ref"))
    except Exception as e:
        update_task(task_id, status="failed", result={"error": str(e)})
        return usage_limit.notice(e) or f"Knowledge base lookup failed: {e}"

    update_task(task_id, status="done", result={"summary": answer[:300]})
    return answer


def resolve_file(name):
    """Full path of a stored file by (base)name, or None. Exact basename
    match first, then case-insensitive. Used by the coder to read a spec
    file the user uploaded (ADR-0006); stays inside the store."""
    if not name:
        return None
    wanted = _safe_name(os.path.basename(name))
    lowered = None
    for path in _iter_files():
        base = os.path.basename(path)
        if base == wanted:
            return path
        if base.lower() == wanted.lower() and lowered is None:
            lowered = path
    return lowered


def read_file(name):
    """(basename, text) of a stored file, for consumers that need content
    (the coder's spec intake). text is None when the file isn't readable as
    text (same extraction rules as indexing, PDFs included). Raises
    KeyError when no such file exists."""
    path = resolve_file(name)
    if not path:
        raise KeyError(name)
    return os.path.basename(path), _extract_text(path)


# ----------------------------------------------------------------- management

LIST_K = 40   # most files shown in one listing


def list_report():
    """What the store holds, newest first, as a Telegram-ready listing.
    Instant (no worker task): a directory walk plus one index read."""
    reindex()
    indexed = {}
    con = _conn()
    try:
        for r in con.execute("SELECT path, chunks FROM knowledge_files"):
            indexed[r["path"]] = r["chunks"] > 0
    finally:
        con.close()
    stamped = []
    for path in _iter_files():
        try:
            stamped.append((os.path.getmtime(path), path))
        except OSError:
            continue
    if not stamped:
        return ("Your knowledge base is empty -- say \"remember ...\" or "
                "send me a document to start it.")
    stamped.sort(reverse=True)
    lines = [f"Your knowledge base ({len(stamped)} file(s)), newest first:", ""]
    for mtime, path in stamped[:LIST_K]:
        day = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
        tail = "" if indexed.get(path) else " (stored, not text-searchable)"
        lines.append(f"• {os.path.basename(path)} ({day}){tail}")
    if len(stamped) > LIST_K:
        lines.append(f"(+{len(stamped) - LIST_K} more -- search to narrow down)")
    return _cap("\n".join(lines))


def delete_file(name):
    """Remove one stored file by (base)name and drop it from the index.
    Returns the basename removed; raises KeyError when no such file exists.
    resolve_file only ever yields paths inside the store, so this cannot
    reach outside KNOWLEDGE_DIR. Permanent -- the orchestrator confirms
    with the user before emitting knowledge_delete."""
    path = resolve_file(name)
    if not path:
        raise KeyError(name)
    os.remove(path)
    reindex()
    return os.path.basename(path)


# ----------------------------------------------------------------- orchestrator context

def _recent_files(k=5):
    """The k most recently modified stored files, newest first."""
    stamped = []
    for path in _iter_files():
        try:
            stamped.append((os.path.getmtime(path), os.path.basename(path)))
        except OSError:
            continue
    stamped.sort(reverse=True)
    return [name for _, name in stamped[:k]]


def knowledge_context_lines():
    """Context lines for the orchestrator: how much it can draw on, and the
    newest file names -- so 'build the spec I just uploaded' can resolve to
    a concrete spec_file without asking."""
    try:
        n = file_count()
    except Exception:
        return []
    if not n:
        return []
    lines = [f"\nKnowledge base: {n} file(s) indexed "
             "(dispatch the librarian to search/ask)."]
    try:
        recent = _recent_files()
        if recent:
            lines.append("  Most recent files: " + ", ".join(recent))
    except Exception:
        pass
    return lines
