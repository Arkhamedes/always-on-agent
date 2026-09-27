# Librarian — personal knowledge base (`librarian.py`)

The user's own files, searchable and ask-able over Telegram. A **store of files
on disk** (notes they tell the agent to remember, documents/PDFs they upload)
plus a **SQLite FTS5 keyword index** — no embeddings, no vector store, no local
model (see [ADR-0003](../adr/0003-personal-knowledge-base-fts5.md)). Every
"thinking" step stays an on-demand `claude -p` call, like every other role.

Three dispatched ops (read-only, like the researcher):

- **search** — keyword query → FTS5-ranked (`bm25`) snippets + file names. No
  model call.
- **ask** — retrieves the top-K chunks, then answers in one tool-less `claude -p`
  call **grounded in those excerpts and framed around who the user is** (the
  psychologist profile + pinned facts are blended in).
- **drive** — finds files in the user's **Google Drive by name** and replies
  with `webViewLink`s only ([ADR-0007](../adr/0007-sensitive-documents-drive-metadata-only.md)).
  Runs on the `drive.metadata.readonly` scope: the agent can never read file
  contents, so a compromised box leaks names, not documents, and Telegram
  carries a link that only the user's own Google session can open. Sensitive
  documents (passport, ID) live in Drive by design — never uploaded over
  Telegram. Every query word must appear in the file name (tidy naming is
  the contract); no model call. Fails cleanly with a pointer to
  `test/google_reauth.py` until the token carries the scope (and the Drive
  API is enabled in the Google Cloud project).

## Store

- `KNOWLEDGE_DIR` (default `~/knowledge`), gitignored and **outside the repo
  checkout**, so auto-deploy's `git reset --hard` never touches it — same
  durability story as `agent.db` / `token.json`.
- Files land there three ways: a saved note (`knowledge_save` action → a
  markdown file), a Telegram document upload (listener → `save_upload`), or a
  file dropped in directly over Tailscale/scp. All are picked up by `reindex()`.
- Managed from the chat by two **instant** orchestrator actions (filesystem
  only — they never queue behind a long coder build): `knowledge_list` shows
  what the store holds (newest first, non-indexed files flagged), and
  `knowledge_delete` removes one file by exact name — permanent, so the
  orchestrator proposes the exact filename and waits for a confirmed yes
  first (the only knowledge action that confirms).

## Index

- FTS5 virtual table `knowledge_fts` in `agent.db`, plus `knowledge_files`
  (mtime + chunk count per file). `reindex()` is **incremental** — it re-reads
  only files whose mtime changed, drops deleted files, and runs on demand before
  a query. No daemon, no second worker.
- Text is split into ~1500-char chunks on paragraph boundaries. Non-text files
  are **stored but not indexed**; PDFs are indexed only if the `pdftotext`
  binary (poppler) is installed (no Python dependency).
- If a SQLite build ever lacks FTS5, it falls back to a plain `knowledge_chunks`
  table + `LIKE` search (degraded ranking, still functional).

## Interface

- `run_librarian_task(task_id) -> message` — worker entry. Parses the JSON
  instruction and routes on `op`.
- `search(query) -> str`, `ask(question, chat_id=None) -> str` — the two ops.
- `save_note(text, title=None) -> filename` — persist + reindex a note (the
  orchestrator's instant `knowledge_save` action).
- `save_upload(data, filename) -> (filename, indexed_bool)` — persist + reindex
  a Telegram document; `indexed` is False for files we can't read as text.
- `list_report() -> str` — Telegram-ready listing of the store, newest first,
  capped at 40 entries (the `knowledge_list` action).
- `delete_file(name) -> basename` — remove one stored file and drop it from
  the index (the `knowledge_delete` action); resolves through `resolve_file`,
  so it can never reach outside the store; `KeyError` when absent.
- `knowledge_context_lines()` — context lines for the orchestrator: how many
  files are indexed (a cheap COUNT, no reindex) plus the most recent file
  names, so "build the spec I just uploaded" resolves to a concrete filename.
- `resolve_file(name) -> path | None` / `read_file(name) -> (basename, text)`
  — look up a stored file by basename (exact, then case-insensitive) and read
  its text (same extraction rules as indexing, PDFs included; `text` is None
  for non-text files, `KeyError` when absent). The coder's spec intake
  (ADR-0006) reads specs through this, keeping file access in the owning
  module.
- `init_librarian_db()` — called at startup by `telegram_listener.py`.

Instruction shape (from the orchestrator):

```
{"op": "ask",    "query": "<self-contained question about the user's files>"}
{"op": "search", "query": "<keywords>"}
{"op": "drive",  "query": "<file name words>"}
```

## Behavior

- FTS5 MATCH strings are built by quoting each query word and OR-joining them, so
  punctuation/operators in free text can't break the query or inject FTS syntax.
- `ask` reads the psychologist profile and `facts` **directly, read-only** — the
  sanctioned exception to table ownership, same pattern as `lifeos._facts_tz` /
  `dashboard._fact`. Both reads are wrapped so a missing table never crashes.
- Output capped at 3900 chars (Telegram's limit is 4096) at a newline boundary.
- Task `result` stores a 300-char summary; the full text goes to the chat.

## Ownership

Owns `knowledge_files` and `knowledge_fts` (or the `knowledge_chunks` fallback)
in `agent.db`. Reads `psych_profile` (owned by `psychologist.py`) and `facts`
(owned by `orchestrator.py`) read-only.

## Depends on

`task_store`, the Claude Code CLI on PATH (Max OAuth; `ANTHROPIC_API_KEY`
stripped as everywhere), and optionally `pdftotext` for PDF text.
