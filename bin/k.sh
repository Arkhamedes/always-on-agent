# bin/k.sh — drive Claude Code interactively over the personal knowledge base.
#
# This is NOT part of the always-on agent. It's a convenience for SSHing into
# the VM and steering an interactive `claude` session by hand (from a laptop or
# a phone), living inside ~/knowledge — the same file store the librarian role
# indexes. Source it from your shell rc on the box:
#
#     echo 'source ~/autonomous-agent/bin/k.sh' >> ~/.bashrc
#
# All helpers run in tmux so the session survives disconnects: detach with
# Ctrl-b then d, close your phone, reattach later by running the same command
# (tmux -A reattaches an existing session instead of erroring).
#
# WHY IT'S SCOPED TO ~/knowledge, NOT ~/autonomous-agent:
# this box also holds live secrets (token.json, agent_env.sh, the GitHub App
# .pem) and the running agent. ~/knowledge is just personal notes/documents —
# low blast radius. Never point an unattended session at the prod checkout.
# (The checkout can't push anyway: it uses a read-only deploy key.)

KNOWLEDGE_DIR="${KNOWLEDGE_DIR:-$HOME/knowledge}"

# k — SUPERVISED. You're at the keyboard. Auto-accepts file edits but still
# asks before running shell commands. Use when you're present and steering.
k() {
    tmux new -As knowledge -c "$KNOWLEDGE_DIR" claude --permission-mode acceptEdits
}

# kgo — UNATTENDED. Hand it a task, detach, let it run to completion without
# ever pausing on a prompt. Never stalls; also never asks — so only ever aimed
# at ~/knowledge. Do not use this on the prod repo.
kgo() {
    tmux new -As knowledge-go -c "$KNOWLEDGE_DIR" claude --dangerously-skip-permissions
}

# kc — SUPERVISED session in the agent checkout, with the MCP tools
# (docs/subsystems/mcp.md): knowledge base, calendar, todos/habits against
# the live agent.db. Reads run promptless; every write asks first -- that
# prompt IS the confirm-before-write gate, so never run this unattended and
# never with --dangerously-skip-permissions. Don't edit files here either:
# the checkout belongs to autodeploy's reset --hard.
# Auth note: interactive sessions run on the FULL-SCOPE stored login (one-time
# `claude` -> /login on the box), NOT on CLAUDE_CODE_OAUTH_TOKEN -- that env
# token is inference-only, takes precedence over the stored login, and blocks
# remote control (/remote-control -> drive this session from the claude.ai
# app). agent_env.sh is still sourced for the rest of the env; only the token
# is dropped. The headless service keeps using the env token as before.
kc() {
    tmux new -As agent-code -c "$HOME/autonomous-agent" \
        bash -c 'source agent_env.sh; unset CLAUDE_CODE_OAUTH_TOKEN; exec claude'
}

# kcn — kc but guaranteed FRESH: kills any lingering agent-code session first.
# Sessions are disposable by design -- durable state lives in agent.db, the
# knowledge base, and git, never only in a conversation. Ending claude
# (/exit) already ends the tmux session (exec semantics); kcn is for when
# you detached instead and a stale session is still holding context + RAM.
# To reopen the previous conversation in a fresh process: kcn, then /resume.
kcn() {
    tmux kill-session -t agent-code 2>/dev/null
    kc
}
