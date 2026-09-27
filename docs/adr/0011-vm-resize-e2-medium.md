# ADR-0011: Host moves from free-tier e2-micro to e2-medium (paid)

- **Status:** accepted
- **Date:** 2026-07-17

## Context

The free-tier `e2-micro` (1 GB RAM + 4 GB swap) was the founding constraint:
it drove stdlib-first, the single FIFO worker, and a remote-control session
capacity of 2–3 (every claude.ai-app session is a Node process). Two things
changed: the box now serves as a remote-control host for interactive Claude
Code sessions (the heaviest thing it runs, and the thing swap makes feel
worst), and the system is being prepared for a client release, where "the
owner's box is slow/OOM" becomes more than a personal annoyance.

## Decision

Resize the existing VM in place to **`e2-medium` (2 vCPU, 4 GB RAM)**,
keeping zone (`us-west1-b`), disk, IP mode (ephemeral), and every service
as-is. Accepted cost: ~$25–28/month, paid personally — the first standing
hosting cost this project has had. The 24/7 invariant (ADR-0001) is
unaffected; the resize itself took ~2 minutes of downtime.

**The 1 GB design discipline stays in force for now**: stdlib-first and the
single FIFO worker are unchanged. The extra RAM is headroom for interactive
remote-control sessions (capacity may rise via `REMOTE_CONTROL_CAPACITY`),
not a license to add heavy dependencies. Relaxing the worker-concurrency or
dependency rules is a separate, future ADR if ever wanted.

## Alternatives rejected

- **Stay on e2-micro** — remote-control sessions on 1 GB ride swap and
  crawl; with a client relationship, the box's responsiveness now has
  outside observers.
- **e2-small (2 GB)** — halves the cost but two concurrent interactive
  sessions + the agent already brush 2 GB; the next resize would be soon.
- **New, bigger VM + migration** — nothing about the current disk/OS needed
  replacing; an in-place resize is reversible in minutes and keeps the
  deploy key, Tailscale identity, and systemd setup untouched.

## Revisit trigger

- Cost stops being acceptable → resize back to e2-micro (same three
  commands, reversed) and drop `REMOTE_CONTROL_CAPACITY` accordingly.
- Sustained memory pressure on 4 GB (three sessions + a coder build) →
  consider e2-standard-2 / a workload split, with its own ADR.
- Anyone proposes relaxing stdlib-first or single-worker "because we have
  RAM now" → that's this ADR's explicit non-goal; write the new ADR first.
