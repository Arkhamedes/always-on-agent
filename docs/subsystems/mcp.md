# MCP server (`mcp_server.py`)

The agent's role functions as typed tools for **Claude Code sessions** — the
Tier-2 interface ([ADR-0010](../adr/0010-mcp-tool-surface-for-claude-code.md)).
A session (tmux on the VM, a laptop checkout, or the client profile where
Claude Code is the whole interface) spawns the server over **stdio** via the
repo's `.mcp.json`; no daemon, nothing always-on. The tools call exactly the
module functions the orchestrator uses — same tables, same owners.

## Interface

Stdio MCP server, name `agent`, built on the official `mcp` SDK (FastMCP).
Tools appear to sessions as `mcp__agent__<name>`. All tools return strings
(non-string results are JSON-encoded).

| Group | Tools | Wraps |
|-------|-------|-------|
| Knowledge | `knowledge_search`, `knowledge_save_note`, `knowledge_list`, `knowledge_read`, `knowledge_delete`, `drive_find` | `librarian.py` |
| Calendar | `calendar_create_event`, `calendar_list_events`, `calendar_update_event`, `calendar_delete_event`, `calendar_free_busy` | `secretary.py` (low-level typed fns, not the op-dict layer) |
| Todos | `add_todos`, `complete_todos`, `reopen_todos`, `move_todo`, `open_todos`, `archived_todos` | `lifeos.py` |
| Habits | `add_habit`, `remove_habit`, `log_habits`, `habits_status` | `lifeos.py` |
| Reminders | `add_reminder`, `cancel_reminder`, `pending_reminders` | `lifeos.py` |
| ClickUp (POC) | `clickup_whoami`, `clickup_activity` | `clickup.py` (read-only; returns raw structured activity — the session summarizes it natively; needs `CLICKUP_API_TOKEN`, hint until set) |

Deliberately **not** exposed: `librarian.ask` and the researcher (each spawns
a nested `claude -p` to do what the calling session does natively — search,
read, reason), `clickup.summarize`/`digest` (same reason), the `run_*_task`
wrappers (orchestrator-shaped), and CRM / ideas / journal (digest- and
psychologist-coupled; revisit per ADR).

## Permission policy — the session-side confirm gate

On the Telegram path the orchestrator confirms writes before executing. In a
Claude Code session that job falls to the **permission prompt**:

- Read-only tools are allow-listed in `.claude/settings.json`
  (`knowledge_search/list/read`, `drive_find`, `calendar_list_events`,
  `calendar_free_busy`, `open_todos`, `archived_todos`, `habits_status`,
  `pending_reminders`) — they run promptless.
- Every write prompts. **`knowledge_delete` and `calendar_delete_event` are
  permanent and must never be allow-listed.**

## Env & init (order is load-bearing)

`mcp_server.py` loads env before any project import, `stage.py`-style:
`envfile.load("~/agent_env.sh", "<repo>/agent_env.sh")`, then
`AGENT_DB_PATH` defaults to `<repo>/agent.db` — a pre-set value survives,
which is how verification points the server at a disposable `staging.db`
(`AGENT_DB_PATH=$PWD/staging.db claude`). Caveat: if an env file ever
exports `AGENT_DB_PATH`, it wins over the caller (envfile precedence) and
defeats that override. On start it calls `lifeos.init_lifeos_db()` and
`librarian.init_librarian_db()`; secretary needs no DB.

Failure mapping: missing/stale Google token → a "run `test/google_reauth.py`,
copy `token.json`" hint; unknown knowledge filename → "call `knowledge_list`";
locked `agent.db` → "busy, retry". The server never dies on a tool exception.

**stdout discipline:** stdio transport means stdout is JSON-RPC. Nothing on
these call paths may `print()` (agentlog prints to stdout — keep it out of
any function this server wraps).

## How sessions start

Two ways, same tools either way:

- **Terminal:** `kc` on the box (tmux attach-or-create in the checkout).
- **Phone-first:** with `deploy/remote-control.service` installed, the
  claude.ai app **creates** sessions on the machine directly — no SSH per
  session (capacity-limited; see the unit header and
  `docs/extras/gcp_setup_guide.md` §8).

Both run on the full-scope stored login, never `CLAUDE_CODE_OAUTH_TOKEN`
(inference-only; outranks the stored login and blocks remote control).

## Concurrency with the live service

The VM's listener/worker and an interactive session share `agent.db`. Module
connections use `timeout=30` with short transactions; interleaved writes are
safe, worst case a "busy, retry" tool reply.

## Client profile notes

The minimal (no-Telegram) profile uses this server as its only interface to
knowledge + calendar. Reminders are stored but never delivered there (delivery
is a Telegram ping from `agent.service`) — the tool docstring says so.
See `docs/extras/client-vm-deploy-key-setup.md`.

## Depends on

`envfile` (env loading), `librarian`, `secretary`, `lifeos` (the wrapped
functions), `mcp` SDK (session-spawned only — not part of the always-on
service, so the 1 GB at-rest budget is unaffected).
