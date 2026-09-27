# ADR-0009: Laptop staging + clone-ready setup (no second environment to pay for)

- **Status:** accepted
- **Date:** 2026-07-11

## Context

Two goals arrived together:

1. **A staging environment.** `git push origin main` deploys to the live VM
   within ~2 minutes (ADR-0001's pull-based CI), so every push is a
   production deploy. Non-trivial changes need somewhere to run first.
2. **Clone-ready setup.** A friend should be able to `git clone` this repo
   and stand up their own instance without reading Bryan-specific values out
   of the source.

The forces: the GCP free tier covers exactly **one** e2-micro, so a second
VM costs real money; the 1 GB RAM budget makes running two full stacks (two
concurrent `claude -p` node processes) on the one VM an OOM risk; and one
Telegram bot token cannot serve two long-pollers (`getUpdates` returns 409
to the second), so any always-on staging needs its own bot regardless of
where it runs. Meanwhile both goals share a prerequisite: every personal
literal (default coder repo, journal-backup repo, systemd paths, the
dashboard's tailnet URL) must move out of the code into config.

## Decision

**Staging is a laptop checkout driven by `stage.py`, not a second VM and not
a second bot.** `stage.py` replaces only the Telegram transport (CLI in,
stdout out, its own forced `staging.db`); everything else — orchestrator
`decide()`, instant actions, `worker.run_task()` (the same function the VM
loop calls), real `claude -p`, real GitHub/Google APIs against a sandbox
`CODER_REPO` — is the production code path. Staging is on-demand, not 24/7:
the 24/7 invariant (ADR-0001) protects production, not testing.

**The repo is a template.** No personal value may be a committed literal:
instance identity lives in env vars (`CODER_REPO`, `JOURNAL_BACKUP_REPO`, …
in `agent_env.sh`), in gitignored files (`token.json`, `*.pem`,
`agent_env.staging.sh`), in the DB (`facts`), or in placeholder-marked
systemd units (`YOUR_USER`). `setup.py` is the onboarding path: an
interactive stdlib wizard that validates each credential against its real
service as it is entered, then writes `agent_env.sh`. A new instance is:
clone → `pip install` → `setup.py` → `test/smoke_test.py` →
`run_listener.sh`.

## Alternatives rejected

- **Second VM as staging** — the free tier covers one e2-micro; ~$7–9/mo to
  test a single-user hobby system, and it still needs its own bot token.
- **Second instance on the same VM (tracking a `staging` branch)** — two
  concurrent `claude -p` processes in 1 GB RAM is an OOM risk; the interlock
  needed to prevent that adds complexity in the most fragile spot. Revisit
  only for breakage that reproduces solely in the VM environment.
- **Bryan's VM as canary, a friend's VM as prod (`release` branch)** —
  couples the friend's uptime to Bryan's release discipline, makes Bryan the
  operator of a box he doesn't use (their failures report to their Telegram,
  not his), and still does zero staging of the friend's own config paths.
  Each instance is sovereign: its own VM, its own `main`, its own billing.
- **Second Telegram bot for staging now** — BotFather makes this cheap (one
  account can own many bots), but the listener transport is the thinnest,
  least-changed layer; not worth a standing credential today. Bolt one on if
  `telegram_listener.py` itself starts churning.
- **A web UI for setup** — setup happens over SSH on a fresh headless VM;
  serving a UI there means ports/auth against the outbound-only doctrine,
  and a browser flow can't beat prompt-validate-write in the terminal.

## Revisit trigger

- A second regular user (not a clone-owner) or a paying user appears —
  revisit real staging infra and tagged releases.
- A breakage class that only reproduces on the VM (RAM pressure, systemd,
  autodeploy) bites more than twice — revisit the same-VM staging instance.
- The listener/transport layer starts changing frequently — add a staging
  bot token.
