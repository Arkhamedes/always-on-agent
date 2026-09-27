# ADR-0003: Personal knowledge base uses on-disk files + SQLite FTS5, not embeddings

- **Status:** accepted
- **Date:** 2026-07-06

## Context

The agent is growing a personal knowledge base: a place to hold a few of the
user's own files (notes they tell it to remember, documents/PDFs they upload
over Telegram) and answer questions grounded in them ("what did I save about
X", "according to my files, when is Falcon's kickoff"). The natural reach for
"ask questions over my documents" is semantic retrieval — embeddings + a vector
store. That collides head-on with the box's constraints (see ADR-0001, AGENTS.md
"e2-micro constraint"):

- **1 GB RAM.** A resident embedding model blows the budget. There is *no* local
  model inference on this box by doctrine — every thinking step is shelled out to
  `claude -p`.
- **No new metered key.** Hosted embeddings would add an external, per-token API
  dependency; the only external keys allowed are Groq (voice) and the
  Google/GitHub credentials. Anthropic is billed through the flat Max plan via
  the Claude Code CLI, never a metered key (`ANTHROPIC_API_KEY` is explicitly
  unset everywhere).
- **Single FIFO worker, stdlib-first.** No room for a second concurrent indexer
  process or a vector-DB runtime dependency.

The corpus is also *small* (one person's important files), which is exactly the
regime where keyword search is competitive with semantic search.

## Decision

The knowledge base is **files on disk + a SQLite FTS5 keyword index**, owned by
`librarian.py`:

- **Store:** plain files under `KNOWLEDGE_DIR` (default `~/knowledge`), gitignored
  and *outside* the repo checkout, so auto-deploy's `git reset --hard` never
  touches them (same durability story as `agent.db` and `token.json`).
- **Index:** an FTS5 virtual table in `agent.db` (one SQLite file, per the data
  model). `reindex()` is incremental (re-reads only files whose mtime changed)
  and runs on demand before a query — **no resident indexer, no daemon.** Idle
  RAM cost is an inverted index in SQLite, effectively zero.
- **Retrieval → read:** `search` returns FTS5-ranked (`bm25`) snippets with no
  model call at all; `ask` retrieves the top-K chunks and feeds them, plus the
  psychologist profile and pinned facts, into **one tool-less `claude -p` call**
  — the same offloaded-thinking pattern as every other role.
- **Portability guard:** if a SQLite build ever lacks FTS5, `librarian.py` falls
  back to a plain table + `LIKE` search (degraded ranking, still functional).

Files the store can't read as text (arbitrary binaries) are **stored but not
indexed**; PDFs are indexed only when the `pdftotext` binary is present (poppler),
so text extraction adds **no Python dependency**.

## Alternatives rejected

- **Local embedding model + vector store (FAISS/sqlite-vec/Chroma).** A resident
  model or native vector runtime does not fit 1 GB RAM alongside a big-context
  `claude -p` run. This is the constraint that killed the "obvious" design.
- **Hosted embedding API (OpenAI/Voyage/Google).** Adds a metered per-token key,
  which the no-new-key doctrine forbids; also a new failure mode on every query.
- **Bundle a PDF/parsing library (pypdf, unstructured).** A new runtime
  dependency for a phase-1 feature dominated by markdown/text notes. Deferred —
  optional `pdftotext` covers the common case with zero Python deps.
- **Make Claude Code itself the knowledge layer (resident session on the VM).**
  A persistent Node + Claude Code process is the RAM risk the whole design avoids;
  the on-demand equivalent (SSH over Tailscale, run `claude` in `~/knowledge`)
  already exists and needs no build.

## Revisit trigger

Reconsider — most likely toward a hosted embedding API or a bigger host (its own
ADR, preserving the ADR-0001 invariant) — when **either**: keyword FTS5 retrieval
measurably fails on real queries (the user asks for things by meaning and the
right file isn't retrieved), **or** the corpus grows past the low hundreds of
files where keyword ranking stops being competitive. Absent that, this stands.
