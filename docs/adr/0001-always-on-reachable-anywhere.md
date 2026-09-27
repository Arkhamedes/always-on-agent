# ADR-0001: The agent is on 24/7 and reachable anywhere by phone

- **Status:** accepted
- **Date:** 2026-07-04

## Context

The whole point of the system is an assistant that is *there* — while AFK,
traveling, or away from the PC. A feature that only works when a laptop is
open, or a redesign that introduces downtime windows, defeats the purpose no
matter how good it is otherwise.

## Decision

Always-on availability, reachable from anywhere with a phone and an internet
connection, is the one invariant of this project. It is decided and not to be
relitigated. Everything else — hosting, datastore, chat interface, model
provider, architecture — may change, provided the replacement preserves this.

## Consequences

- Deploys must not require taking the agent down manually (the pull-based
  auto-deploy honors this: it defers while a task is mid-run).
- Any proposed migration (new host, new interface) must include a cutover plan
  that keeps the agent reachable throughout.
- Current implementation choices (e2-micro VM, Telegram, Tailscale-only
  dashboard) are *means* to this end, not ends — replaceable via a future ADR.
