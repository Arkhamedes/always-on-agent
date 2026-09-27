# Research & news (`researcher.py`)

Live web answers over Telegram. Two roles in one module:

- **researcher** — one self-contained question → a direct answer plus a
  "Sources:" list of bare URLs.
- **news** — a digest of ≥ N distinct current stories (default 10), each a
  headline + one/two-sentence summary + link, optionally focused on a topic.
  Re-askable ("different stories than before — skip X, Y") by dispatching
  again with an adjusted topic.

Implementation: headless Claude Code with ONLY its built-in
WebSearch/WebFetch tools enabled — no Playwright, no scraping stack, no RAG
index; nothing to maintain on the 1 GB box, and it bills to the flat Max
plan like every other call.

## Interface

- `run_research_task(task_id) -> message` — worker entry. Parses the JSON
  instruction and routes on `kind`.
- `web_search(query) -> str` and `news_digest(topic, count) -> str` —
  direct callables behind the two kinds.

Instruction shapes (from the orchestrator):

```
{"kind": "search", "query": "<self-contained question, dates resolved>"}
{"kind": "news",   "topic": "<focus, or 'top world news'>", "count": 10}
```

## Behavior

- `claude -p` with `--tools WebSearch,WebFetch` (auto-permitted headlessly
  via `--allowedTools`), up to 15 turns, 420 s timeout — a digest takes
  several searches.
- Both are read-only and dispatched by the orchestrator immediately, no
  confirmation.
- Prompts demand plain text (no markdown — output goes straight to
  Telegram), fresh sources over memory, and for news: several major
  outlets, no two items on the same event, date on the first line.
- Output is capped at 3900 chars (Telegram's limit is 4096) by cutting at
  the last newline, so a URL is never chopped in half.
- Task `result` stores only a 300-char summary extract; the full text goes
  to the chat.

## Depends on

`task_store`, the Claude Code CLI on PATH (Max OAuth; `ANTHROPIC_API_KEY`
stripped as everywhere).
