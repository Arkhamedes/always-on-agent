# ADR-0010: MCP tool surface for Claude Code sessions

- **Status:** accepted
- **Date:** 2026-07-11

## Context

The agent has grown a second way of working ("Tier 2"): interactive Claude
Code sessions — tmux on the VM, a laptop checkout, and a minimal client
profile where Claude Code is the *only* interface (no Telegram, no services;
see `docs/extras/client-vm-deploy-key-setup.md`). Those sessions need the
agent's everyday capabilities — knowledge base, calendar, todos/habits —
without going through the Telegram orchestrator, and without violating the
table-ownership rule by poking SQLite directly.

## Decision

Ship `mcp_server.py`: a stdio MCP server (official `mcp` SDK / FastMCP,
registered in the repo's `.mcp.json`) exposing the **pure role functions** of
`librarian`, `secretary`, and `lifeos` as typed tools.

Scope decisions:

- **In:** knowledge (search/save/list/read/delete, Drive name-search),
  calendar (create/list/update/delete/free-busy), todos, habits, reminders.
- **Out — redundant with the session itself:** `librarian.ask`, researcher,
  news. Each spawns a nested `claude -p` to do what the calling session does
  natively; sessions should `knowledge_search` + `knowledge_read` and reason
  in place, and use their own WebSearch.
- **Out — wrong shape:** the `run_*_task(task_id)` wrappers (they exist to
  serve the task queue, not direct calls).
- **Out — deferred:** CRM, ideas, journal (digest/psychologist-coupled;
  journal saves have backup side effects). Revisit when a session needs them.
- **Permission policy replaces the orchestrator's confirm-before-write:**
  read-only tools are allow-listed in `.claude/settings.json`; every write
  falls through to the session's permission prompt. The two permanent
  deletions (`knowledge_delete`, `calendar_delete_event`) are never
  allow-listed.
- **The `mcp` dependency is session-only.** It is imported exclusively by
  `mcp_server.py`, which Claude Code spawns per session; the always-on
  service never loads it, so the e2-micro's at-rest RAM budget is unchanged.
  This is the agreed exception lane in the stdlib-first doctrine.

## Consequences

- One new dep in `requirements.txt` (`mcp`, pulling pydantic/anyio). The VM
  needs a one-time manual `./venv/bin/pip install -r requirements.txt` —
  autodeploy never pip-installs. *(Update 2026-07-11: autodeploy now installs
  on any `requirements.txt` change, so this manual step applied only to this
  first dependency.)*
- `AGENT_DB_PATH` remains the isolation seam: the server defaults to
  `agent.db` but a caller-exported path wins, so verification runs against a
  disposable `staging.db` (same discipline as `stage.py`, ADR-0009).
- The client profile's interface is now real: knowledge + calendar over MCP,
  research via native WebSearch. Reminders store but never fire there
  (delivery is the live service's Telegram ping).
- Anything the server wraps must stay `print()`-free (stdout is the JSON-RPC
  channel) and Telegram-free — the existing pure functions already are, and
  new wrapped functions must keep that property.
- Subsystem doc: `docs/subsystems/mcp.md`.
