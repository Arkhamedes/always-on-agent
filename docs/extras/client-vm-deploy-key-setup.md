# Client VM setup — deploy key + minimal instance

How to stand up a second, minimal instance of this repo on a client's own VM.
The client's profile is **Claude Code sessions + knowledge base + research +
calendar** — no Telegram, no listener/worker/scheduler, no dashboard. His
interface is Claude Code itself (interactive sessions in tmux, or cloud
sessions on claude.ai/code for plain repo work).

Roles below: **Client** = owns and operates the VM. **Owner** (Bryan) = owns
this GitHub repo. The owner never needs access to the client's GCP project or
VM; the only thing that crosses between them is one SSH **public** key.

---

## 1. Machine specs

| Resource | Minimum | Recommended | Notes |
|----------|---------|-------------|-------|
| CPU | 1 vCPU | 2 vCPU | I/O-bound; x86-64 or ARM64 both fine |
| RAM | 2 GB | **4 GB** | The peak is an interactive Claude Code session (Node holding large context) next to a git clone. 1 GB free-tier works only with a 4 GB swap file and feels tight for interactive use. Add swap regardless. |
| Disk | 25 GB | 30 GB | OS + Python + Node + Claude Code ≈ 10 GB; repos are transient temp clones |
| Network | Outbound HTTPS only | — | **No inbound ports, no static IP needed.** Everything (GitHub, Anthropic, Google) is outbound. |
| OS | Ubuntu/Debian LTS | Ubuntu 24.04 LTS | — |

GCP sizing: `e2-small` (2 GB) is the sensible floor, `e2-medium` (4 GB) the
comfortable choice. The always-free `e2-micro` (1 GB) is possible with swap
but not recommended for a primarily-interactive instance.

Accounts the client needs before starting:
- A **Claude subscription** (Max recommended) — his own login; usage bills to him.
- A **Google account** with Calendar, plus a Google Cloud OAuth client for the
  token flow (or the owner shares an OAuth client ID/secret — the resulting
  refresh token is still the client's own).
- **No GitHub account required** — that is what the deploy key is for.

---

## 2. Deploy key — client side (on his VM)

Generate a keypair dedicated to this repo. The private half never leaves his
machine; the public half is not a secret and can be sent over any chat.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N "" -C "client-vm deploy key"
cat ~/.ssh/github_deploy.pub    # send this single line to the owner
```

Then pin the key to GitHub in `~/.ssh/config`:

```
Host github.com
    IdentityFile ~/.ssh/github_deploy
    IdentitiesOnly yes
```

## 3. Deploy key — owner side (on github.com)

Repo → **Settings → Deploy keys → Add deploy key**:
- Title: `client-vm` (or the client's name)
- Key: the pasted public key
- **Leave "Allow write access" unchecked** — read-only.

Revocation is deleting this key from the same page; the client's VM loses
clone/pull access immediately and nothing else is affected.

## 4. Clone + runtime — client side

```bash
sudo apt update && sudo apt install -y git python3-venv tmux

git clone git@github.com:<owner>/<repo>.git autonomous-agent
cd autonomous-agent

python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# Node + Claude Code (nvm keeps it out of apt's hands)
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
export NVM_DIR="$HOME/.nvm" && . "$NVM_DIR/nvm.sh"
nvm install --lts
npm install -g @anthropic-ai/claude-code
```

## 5. Credentials — client side, his accounts only

> The repo's `setup.py` wizard covers this profile: run
> `./venv/bin/python setup.py` and answer **no** to "Set up the FULL
> instance?" — the minimal path walks exactly the credentials below, checks
> the §6 MCP wiring, and writes `agent_env.sh` only if something actually
> needs recording. The steps below are the manual reference for the same
> thing.

**Claude login** — on the VM: `claude` and follow the browser-URL login flow
(or `claude setup-token` on his laptop and transfer the resulting
`sk-ant-oat...` token). This is his subscription; **never set
`ANTHROPIC_API_KEY`** — it would override the subscription token and bill
metered API rates.

**Google Calendar token** — the OAuth consent flow needs a browser, so it runs
on his **laptop**, not the VM: run `test/google_reauth.py` there, then copy
the resulting `token.json` to the VM repo directory
(`scp token.json client-vm:~/autonomous-agent/`). It holds a refresh token, so
the VM never needs a browser. The Google Cloud project must be published "In
production" — Testing-mode refresh tokens expire after 7 days. Verify from the
VM afterwards with `./venv/bin/python test/gcal_smoke_test.py`.

**Knowledge base** — just a directory: `mkdir ~/knowledge`. Files saved there
are indexed by `librarian.py` (SQLite FTS5, no extra services).

## 6. What runs on this instance (and what doesn't)

| Piece | Client VM |
|-------|-----------|
| Claude Code in tmux (the interface) | ✅ `tmux new -s code` → `claude` — survives phone disconnects |
| Remote-control server (optional, recommended) | ✅ `deploy/remote-control.service`: the **claude.ai app creates sessions on his VM directly** — phone-first, no SSH per session. Needs his one-time full-scope `/login` (the app-login flow, not `setup-token`); install steps in the unit header. For a coding-first client this can be the whole day-to-day interface |
| `librarian.py` (knowledge base) | ✅ as a library — no daemon |
| `secretary.py` (calendar) | ✅ as a library — no daemon |
| Research / news | ✅ native Claude Code WebSearch — zero plumbing |
| MCP server exposing knowledge + calendar tools | ✅ `mcp_server.py`, spawned per session via the repo's `.mcp.json` — `claude mcp list` should show `agent: ✔ Connected` after `./venv/bin/pip install -r requirements.txt` (see `docs/subsystems/mcp.md`). Reminders tools exist but never deliver on this profile (delivery is the live service's Telegram ping) |
| `agent.service` (Telegram listener/worker/scheduler) | ❌ never installed |
| `dashboard.service` / frontend | ❌ never installed |
| GitHub App credentials | ❌ not needed — coding happens inside his own Claude Code sessions |

There is **nothing always-on to babysit**: no systemd service is required for
this profile. tmux keeps interactive sessions alive across disconnects.

## 7. Staying up to date

Hands-free (recommended): install the client-profile timer — a plain
fast-forward `git pull` every 15 minutes, nothing to restart because nothing
runs as a service on this profile (a new Claude Code session simply uses the
new code):

```bash
# edit User= and the two paths in deploy/client/clientpull.service first
sudo cp deploy/client/clientpull.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now clientpull.timer
```

Or manually, whenever: `cd ~/autonomous-agent && git pull`.

Either way, do **not** install `deploy/autodeploy.*` on this profile: that is
the owner VM's deploy path, and its final step restarts `agent`/`dashboard`
services that don't exist here — every run would error. Fixes flow one way:
client reports → owner commits to `main` → client's VM pulls it within
15 minutes.

## 8. Handover rules (owner)

- **Dry-run the handover first.** In a scratch directory on your laptop,
  clone the repo fresh and walk §4–§5 yourself end to end (venv,
  `pip install -r requirements.txt`, credentials, `claude mcp list`):

  ```bash
  cd ~/tmp && git clone git@github.com:<owner>/<repo>.git handover-rehearsal
  cd handover-rehearsal && python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
  # ...then follow §5; rm -rf the clone when done
  ```

  It costs minutes and catches anything that only works because of state in
  your live checkout (a full-instance rehearsal is the same recipe but ends
  with `./venv/bin/python setup.py` instead of §5 — see README → Setup).
- Hand over via **`git clone` only — never a copy of a working directory**.
  A live checkout contains untracked secrets (`token.json`, `*.pem`,
  `agent_env.sh`, `agent.db` with personal data). A fresh clone is clean.
- The deploy key gives repo **read** access only: the client sees all code,
  docs, and git history, but cannot push and has no GitHub-side presence.
- The two OAuth flows (Claude login, Google token) are done by the client on
  his own accounts — the owner should never handle those credentials.
