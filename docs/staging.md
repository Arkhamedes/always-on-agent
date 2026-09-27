# Staging — test on the laptop before it deploys itself

Staging is a **laptop checkout of this repo, driven by `stage.py`** — not a
second VM and not a second Telegram bot ([ADR-0009](adr/0009-laptop-staging-and-clone-ready-setup.md)).
Remember the deploy model: `git push origin main` is live on the VM within
~2 minutes, so anything non-trivial should pass through here first.

## What staging is

| | production (VM) | staging (laptop) |
|---|---|---|
| entry point | `telegram_listener.py` (systemd) | `stage.py` (CLI, on demand) |
| messages in/out | Telegram | stdin/stdout |
| database | `agent.db` | `staging.db` — **forced**, can't touch `agent.db` |
| coder target | `CODER_REPO` / `repo` fact | sandbox repo from `agent_env.staging.sh` |
| everything else | `orchestrator.decide()`, `worker.run_task()`, real `claude -p`, real GitHub/Google APIs | **the same code path** |

`stage.py` replaces only the transport. `worker.run_task()` is the exact
function the VM's worker loop calls, and `chat` mode goes through
`orchestrator.handle_message()`, so decide(), instant actions, dispatch,
plan-approval and clarification gates all behave as they will in production.

## One-time setup

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp agent_env.staging.sh.example agent_env.staging.sh && chmod 600 agent_env.staging.sh
# edit it: point CODER_REPO at a THROWAWAY repo the GitHub App is installed on
```

Secrets come from your existing `~/agent_env.sh` / `./agent_env.sh`
(run `setup.py` if you don't have one); `agent_env.staging.sh` is loaded last
and only holds what differs. The `claude` CLI must be on PATH (it does the
thinking); `token.json` is only needed for secretary/mail/finance tests.

## Driving it

```bash
./venv/bin/python stage.py chat "add a todo: buy milk"   # one full turn
./venv/bin/python stage.py chat                          # interactive loop
./venv/bin/python stage.py coder "add a --version flag"  # one role, directly
./venv/bin/python stage.py drain                         # run queued tasks
```

`chat` is the honest end-to-end test: your message → decide() → instant
action or dispatch → the dispatched task actually runs → the reply prints.
A coder plan-approval round trip is just two turns:
`stage.py chat "refactor X"` (plan comes back) → `stage.py chat "yes go ahead"`.

Role mode skips the orchestrator, so **non-coder roles expect their
instruction JSON** (the shapes the dispatch arm in
`orchestrator.handle_message` builds), e.g.:

```bash
./venv/bin/python stage.py researcher '{"kind":"search","query":"e2-micro swap"}'
./venv/bin/python stage.py explainer '{"repo":"owner/name","question":"how does the scheduler work"}'
```

The explainer's HTML doc mode writes `staging_<name>.html` next to the DB
(gitignored) instead of sending a Telegram document.

## Resetting

`rm staging.db` — it's disposable by design. Facts (`repo`, `timezone`,
confirmation state) live in it, so a fresh file also replays first-contact
behavior, which is itself worth testing.

## What staging does NOT cover

- The Telegram transport itself (polling, allowlist, voice download,
  document upload). Changes to `telegram_listener.py` still need a real-bot
  test — or just eyes — before pushing.
- systemd units, the autodeploy timer, and 1 GB RAM pressure. If a change
  plausibly moves memory (new dependency, bigger context), watch the VM for
  a few minutes after it deploys (`journalctl -u agent -f`).
- The scheduler's clock-driven firing (you can still run a digest directly:
  `stage.py digest '{"kind":"morning"}'`).
