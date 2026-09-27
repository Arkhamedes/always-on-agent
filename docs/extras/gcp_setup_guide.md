# Hosting on GCP — Free-Tier Setup Guide

How to run this agent 24/7 on a Google Cloud **Always Free** VM ($0/month for
the compute). This walks through the exact provisioning used for the live
deployment; general software setup (secrets, smoke tests) stays in the
[README](../../README.md).

## What you get (and the constraints)

GCP's Always Free tier includes **one** `e2-micro` VM per billing account
(2 shared vCPUs, 1 GB RAM), a 30 GB **standard** persistent disk, and 1 GB/mo
of North-America egress — but only in `us-west1`, `us-central1`, or
`us-east1`. Any other region, machine type, or disk type bills normally.

Fit against [the hosting requirements](agent_hosting_requirements.md):

- **Always-on:** ✅ Compute Engine VMs never idle-sleep — this is the whole
  reason for the move off a sandbox host.
- **CPU / disk / network:** ✅ comfortably within spec (no inbound needed;
  the agent only makes outbound HTTPS calls).
- **RAM:** ⚠️ 1 GB is below the 2 GB minimum the requirements call for. The
  fix is a **4 GB swap file** (step 3) — a big-context Claude Code run gets
  slow under memory pressure instead of being OOM-killed. Acceptable for a
  single-worker agent; if it becomes painful, move to a 4 GB host.
- **Egress:** the ~1 GB/mo free allowance is plenty (MB-scale per task);
  overage is pennies per GB.
- The **external IPv4** is free while attached to a running Always Free
  instance. Still, check the billing report after the first few days to
  confirm the VM shows $0.

## Prerequisites

- `gcloud` CLI installed and authenticated (`gcloud auth login`)
- A GCP project with **billing enabled** (required even for free-tier
  resources — free tier is applied as credits against usage)
- Compute Engine API enabled:

```bash
export PROJECT=<your-project-id>
gcloud services enable compute.googleapis.com --project=$PROJECT
```

## 1. Create the VM

Every flag below matters for staying inside the free tier: `e2-micro`, a
free-tier region, and `pd-standard` (the default `pd-balanced` is billed).

```bash
gcloud compute instances create autonomous-agent \
  --project=$PROJECT \
  --zone=us-west1-b \
  --machine-type=e2-micro \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --boot-disk-size=30GB \
  --boot-disk-type=pd-standard
```

Debian auto-resizes the root partition to the full 30 GB on first boot; the
"you might need to resize" warning can be ignored.

## 2. SSH in

```bash
gcloud compute ssh autonomous-agent --project=$PROJECT --zone=us-west1-b
```

The first invocation generates an SSH key and pushes it to project metadata.
All remaining steps run **on the VM**.

## 3. Swap (the 1 GB RAM mitigation — do not skip)

```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab
echo "vm.swappiness=10" | sudo tee /etc/sysctl.d/99-swap.conf
sudo sysctl -p /etc/sysctl.d/99-swap.conf
```

`swappiness=10` keeps the box in RAM day-to-day; swap only absorbs the peak
moment (Node holding a ~200k-token context next to a git clone).

## 4. Runtimes

```bash
sudo apt-get update && sudo apt-get install -y git python3-venv python3-pip curl

# Node via nvm (run_listener.sh auto-loads nvm, so this layout Just Works)
curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
source ~/.nvm/nvm.sh
nvm install 20 && nvm alias default 20

npm install -g @anthropic-ai/claude-code
```

## 5. Deploy the agent

Follow the README's [Setup](../../README.md#setup) section (clone into
`~/autonomous-agent`, create the venv, install requirements). To push code
and secrets from your workstation instead of cloning:

```bash
# from the workstation
gcloud compute scp agent_env.sh token.json <your-app>.private-key.pem \
  autonomous-agent:~/autonomous-agent/ --project=$PROJECT --zone=us-west1-b
```

Then on the VM: `chmod 600` all three files.

**Gotcha (this bit us):** `GH_APP_KEY` in `agent_env.sh` must be the
**absolute path on the VM**, e.g.
`/home/<user>/autonomous-agent/<your-app>.private-key.pem`. A path copied
from another machine fails silently until the first coding task.

## 6. Install the service

Per the README's [Run](../../README.md#run) section: edit `User=` and the two paths
in `deploy/agent.service`, then:

```bash
sudo cp deploy/agent.service /etc/systemd/system/agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now agent
```

> If a previous host is still running the same Telegram bot token, **stop it
> first** — two long-pollers on one token fight over `getUpdates` and both
> see conflict errors.

Verify:

```bash
systemctl is-active agent        # active
tail -f ~/autonomous-agent/agent.log
# healthy start = "worker loop started" + "listener up; polling for messages"
```

## 7. After the first real coding task

Check that the swap plan holds up:

```bash
free -h                          # swap used a little = fine
sudo dmesg | grep -i "out of memory"   # should print nothing
```

## 8. Driving Claude interactively on the box (optional)

The always-on agent shells out to `claude -p` per task. Separately, you can SSH
in and steer an **interactive** `claude` session by hand — a Claude that keeps
working while you're away, over your own file store in `~/knowledge` (the same
files the librarian role indexes). This is optional and unrelated to the
service; it's just you at a terminal.

`bin/k.sh` (tracked, so it deploys with the repo) provides four helpers. Wire it
into the shell once:

```bash
echo 'source ~/autonomous-agent/bin/k.sh' >> ~/.bashrc && source ~/.bashrc
```

- **`k`** — supervised. Auto-accepts file edits, still asks before running
  shell commands. Use when you're present and steering.
- **`kgo`** — unattended. Runs to completion without ever pausing on a prompt
  (`--dangerously-skip-permissions`). Never stalls, so you can detach and close
  your phone — but it also never asks, so it is scoped to `~/knowledge` on
  purpose. **Never point `kgo` at the prod checkout.**
- **`kc`** — supervised, in the **agent checkout** (`~/autonomous-agent`) with
  the MCP tools loaded (`docs/subsystems/mcp.md`): knowledge base, calendar,
  todos/habits against the live `agent.db`. Reads run promptless, every write
  asks first — that prompt is the confirm-before-write gate, so never run this
  session unattended. It sources `agent_env.sh` for you; don't edit files
  there (the checkout belongs to autodeploy's `reset --hard`).
- **`kcn`** — `kc` but guaranteed fresh (kills a lingering session first).

**Remote control — drive the `kc` session from the claude.ai app.** One-time
setup: the interactive login must be **full-scope**. `kc` deliberately drops
`CLAUDE_CODE_OAUTH_TOKEN` (the `setup-token` credential is inference-only,
outranks the stored login, and blocks remote control — the always-on service
keeps using it). If the session says you're not logged in: `/login`, copy
the URL into any browser (phone or laptop), sign in, paste the code back
into the terminal. Stored in `~/.claude`, survives reboots. From then on,
`/remote-control` inside any `kc` session hands it to the claude.ai app —
AFK coding with no SSH at all. Needs Claude Code ≥ 2.1.51 and a
subscription plan (not an API key).

**Phone-first: the app CREATES sessions on the VM (no SSH per session).**
`deploy/remote-control.service` keeps a `claude remote-control` server
running (boot-start, crash-restart, via `run_remote_control.sh`). With it
installed, new sessions on this machine are spawned **from the claude.ai
app itself** — multiple at once, each with the MCP tools against the live
`agent.db`. The complete on-the-go loop is app-only: open session → work →
`/clear` to rotate or `/exit` to end.

- One-time install: the full-scope `/login` above, then the unit — the
  placeholder substitution makes it one line:
  ```bash
  sed "s/YOUR_USER/$USER/g" deploy/remote-control.service \
    | sudo tee /etc/systemd/system/remote-control.service > /dev/null
  sudo systemctl daemon-reload && sudo systemctl enable --now remote-control
  ```
  After that, SSH is only for the rare rescue.
- Capacity defaults to **3** concurrent sessions (`REMOTE_CONTROL_CAPACITY`
  in `agent_env.sh` to change): every session is a Node process, and 1 GB
  comfortably fits about two idle ones on top of the agent — the third
  leans on swap. Don't raise it further without resizing the VM; don't run
  heavy sessions while a coder build is mid-run.
- Sessions spawn in the checkout (`--spawn same-dir`, not `worktree`: the
  MCP server needs `./venv`, which is untracked and absent in worktrees).
  The usual rule applies — don't edit files there.
- Deploys do NOT restart this unit (sessions survive pushes); a unit restart
  or VM reboot ends all active sessions.

**Session hygiene** — sessions are disposable; durable state lives in
`agent.db`, `~/knowledge`, and git, never only in a conversation. Rotation
happens *inside* the session, no SSH ceremony: **`/clear`** = fresh
conversation in place (the swap command), **`/resume`** = reopen a previous
one, **`/exit`** when done — it ends the tmux session too (exec semantics),
freeing the RAM, so the next `kc` starts fresh by construction. Detach
(Ctrl-b d) only for work genuinely mid-flight: a session left detached for
days keeps stale context *and* idle Node RAM on a 1 GB box. `kcn` is the
outside-in reset for exactly that case.

Both run inside `tmux`, so the session survives a dropped connection:

```bash
kgo                      # start; type your task
# Ctrl-b then d          # detach — Claude keeps running on the VM
kgo                      # later, from anywhere: reattaches the same session
```

Why the scoping matters: this box holds live secrets (`token.json`,
`agent_env.sh`, the App `.pem`) and the running agent. `~/knowledge` is just
personal notes/documents — low blast radius. The checkout can't push regardless
(read-only deploy key), but a never-prompt session still shouldn't roam the
prod dir. For the balanced middle ground (allow/deny lists, safer but may stall
on the unexpected), see the `permissions` block pattern in a
`~/knowledge/.claude/settings.json`.

One caveat on the 1 GB box: a heavy `kgo` run shares RAM with the always-on
agent and could collide with a scheduled digest (morning/evening/Sunday). Light
work over `~/knowledge` is a non-issue; don't kick off something huge right
before a digest fires.

## 9. Reaching the box from your phone (SSH over Tailscale)

The VM has **no inbound public access** — it's outbound-only, reached over
[Tailscale](https://tailscale.com) (already used for the dashboard). SSH from a
phone rides the same tailnet.

**One-time, on the VM** (if Tailscale isn't already installed for the dashboard):

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up            # opens a login URL; authenticate with your account
tailscale ip -4              # note the 100.x.y.z tailnet address
```

Make sure a normal Linux user + SSH key exists (GCP's `gcloud compute ssh` set
this up in step 2; the phone reuses that same account).

**On the phone:**

1. Install **Tailscale** (App Store / Play Store), sign in with the **same
   account** as the VM, and toggle it on. The phone is now on the tailnet.
2. Install an SSH client — **Termius** (iOS/Android) or **Blink** (iOS) are the
   common picks.
3. Add a host:
   - **Host:** the VM's tailnet name (e.g. `autonomous-agent`) or its
     `100.x.y.z` IP from `tailscale ip -4`.
   - **Port:** `22`
   - **User:** your Linux user on the VM.
   - **Key:** generate a keypair in the SSH app, then append its **public** key
     to `~/.ssh/authorized_keys` on the VM (paste it over your existing laptop
     SSH session). Password auth is off by default on GCP images — use a key.
4. Connect. Then run **`kgo`**, **`k`**, or **`kc`** and you're driving Claude
   from your phone. Detach with Ctrl-b d (Termius has an on-screen Ctrl key);
   the session keeps running on the VM after you background the app.

**Zero-typing shortcuts** — make login *be* the session, so one command (or
one tap) lands you inside Claude:

- **Laptop** — alias the whole hop in your shell rc:
  ```bash
  alias kc="gcloud compute ssh autonomous-agent --project=<project> \
    --zone=<zone> --ssh-flag=-t --command='bash -ic kc'"
  ```
  (`-t` allocates the tty tmux needs; `bash -ic` loads `.bashrc` so the
  helper exists.)
- **Phone** — in Termius, set the host's **startup command** (or a snippet)
  to `bash -ic kc`: tapping the host connects and drops you straight into
  the session. Same trick works for `k`/`kgo` over `~/knowledge`.

Notes:
- MagicDNS (Tailscale setting) lets you use the plain hostname instead of the
  `100.x` IP — convenient on mobile.
- No firewall/port-forward changes are ever needed: Tailscale is a mesh, so
  the outbound-only, no-public-inbound posture (ADR-0001) is preserved.

## Staying free — quick reference

| Rule | Why |
|------|-----|
| Keep the machine type `e2-micro` | Anything bigger bills normally |
| Stay in `us-west1` / `us-central1` / `us-east1` | Free tier is region-locked |
| Keep the boot disk `pd-standard`, ≤ 30 GB | `pd-balanced` and extra GB are billed |
| One free VM per **billing account** | A second instance bills, even in another project |
| Don't attach GPUs, don't use Spot | Never covered by free tier |
| Snapshots are **not** free | Back up by re-running this guide + copying the SQLite DB and secrets |
