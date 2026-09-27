# Hosting Requirements — Personal Autonomous Agent

## What this system is (context for the discussion)

A personal, always-on autonomous agent that runs 24/7 and is driven over Telegram. It has two roles today: a **coder** (takes a plain-English task, edits code in a GitHub repo, and opens a pull request for human review — never merges autonomously) and a **secretary** (creates Google Calendar events). An LLM "orchestrator" decides what each incoming message means and dispatches work.

**The single most important fact for sizing: this box does NOT run any AI/ML inference locally.** The intelligence comes from Anthropic's cloud via the **Claude Code CLI** (headless `claude -p`), authenticated with a personal **Claude Max** subscription token. The box is a lightweight *orchestration client* that shells out to Claude Code and to git — the heavy model compute happens remotely on Anthropic's servers. **No GPU is needed. This is not an ML workload.**

## The hard constraint (the reason for choosing a new host)

- **Must run continuously, 24/7, with no idle-sleep / idle-suspend.** The current host (a managed cloud sandbox on its free tier) sleeps the machine when there's no interactive activity, which kills the agent. Any replacement must stay genuinely always-on. This is non-negotiable — it's the whole point of the move.
- **Must auto-recover:** restart the service automatically on crash, and come back on its own after a reboot/power event, resuming work without manual intervention.

## Workload profile

- Mostly **idle**: a Python process long-polls Telegram every ~30 seconds, waiting for messages.
- **Bursty** when a task runs: it spawns a Claude Code (Node.js) subprocess that holds conversation context (up to ~200k tokens), clones a small git repo into a temp dir, edits files, then commits/pushes via git. One task at a time (single background worker).
- Local state is a small **SQLite** database plus a text log file. Both grow slowly.

## Resource requirements

**CPU:** Light and I/O-bound (most "work" is waiting on remote API responses).
- Minimum: **1 core / vCPU / OCPU** genuinely suffices for the current single-worker design.
- Comfortable: **2 cores** — keeps the listener responsive while a task subprocess runs.
- Architecture-agnostic: **x86-64 or ARM64 both work** (Node.js, Python, and git all run on ARM64; the Claude Code CLI runs on both).

**RAM:** The binding resource. The peak moment is a Claude Code run (Node holding a large context) alongside a git clone and the Python worker.
- Absolute minimum: **2 GB** (workable for small repos, thin headroom; 1 GB risks out-of-memory on a big-context run).
- Recommended: **4 GB** (real headroom for larger repos and stability).
- Future-proof: **8 GB+** if ever running concurrent workers or larger repos.

**Storage:** Trivially met by any option.
- OS + Python + Node + Claude Code + tools: ~8–10 GB.
- Cloned repos are transient (temp dirs, cleaned up). SQLite DB and logs grow slowly (MB-scale for a long time; log rotation is a nice-to-have).
- **25–30 GB total is comfortable.**

**Network:**
- **Inbound: NONE required.** Telegram is used via long-polling (outbound), so there is **no need for a public IP, open ports, port forwarding, or a static IP.** (This is a big simplification for home hosting — CGNAT and dynamic IPs are non-issues.)
- **Outbound: HTTPS** to Anthropic's API, Telegram, GitHub, and Google Calendar.
- **Bandwidth: modest** — well under any typical cap. The largest component is API context transfer per task (MB-scale), plus tiny polling traffic and small git transfers. Egress limits are not a practical concern.
- A **stable connection** matters: a dropped link interrupts the poll (it reconnects, but frequent drops mean missed responsiveness).

## Software environment

- **Linux** (Ubuntu/Debian-class recommended).
- **Python 3** (virtual env) running the agent code.
- **Node.js** + the **Claude Code CLI**, authenticated via an OAuth token from the Claude Max plan (crucially: this avoids paying metered Anthropic API rates — the agent runs on the flat-rate Max subscription).
- **git** + a GitHub App credential (mints scoped per-task tokens to open PRs).
- **Secrets** as environment variables (bot token, OAuth token, GitHub App key path, Google Calendar token).
- A **process manager** — a `systemd` unit that starts the listener on boot and restarts it on crash (`Restart=always`).
- **Persistent local storage** for the SQLite DB, the Google token file, and logs.

## Reliability essentials (apply to either option)

- `systemd`: auto-start on boot + auto-restart on crash.
- Service resumes automatically after a reboot.
- A **heartbeat/alert** (already instrumented via the log) to notify if the agent goes silent.

---

## The two options to weigh

### Option A — VPS (rented cloud instance)

**Fit:** A small instance like Hetzner CX22 (2 vCPU / 4 GB / 40 GB, ~€4/mo) is an ideal match. DigitalOcean (~$6/mo) is a polished alternative. Oracle Cloud "Always Free" offers a genuinely free, always-on 2-OCPU / 12 GB ARM VM (the active workload sidesteps its idle-reclaim policy), but with real caveats: opaque/aggressive signup fraud-flagging, reports of abrupt account termination (mitigated by converting to Pay-As-You-Go with a card on file), and ARM capacity-provisioning friction.

**Pros:** Always-on by default; provider owns power, cooling, network, and hardware; trivial to resize; snapshots/backups; reachable from anywhere.

**Cons:** Ongoing monthly cost (except Oracle's free tier); data lives on a third party; provider Terms-of-Service / termination risk (especially Oracle's free tier).

### Option B — Self-hosted mini PC (own hardware at home)

**Fit:** A low-power mini PC (e.g., an Intel N100-class box, ~$150–300 new, or a used enterprise "tiny" desktop) with 8–16 GB RAM is inexpensive and more than capable. Idle power draw ~6–15 W (roughly $10–25/year in electricity).

**Pros:** One-time cost, no monthly fee; full ownership and control; no ToS/termination risk; data stays home; can host other workloads too; RAM is cheap to over-provision. **Bonus:** because no inbound connectivity is needed, home-network headaches (CGNAT, dynamic IP, port forwarding) don't apply.

**Cons:** You become the datacenter — home **power reliability** (an outage kills it; a UPS and a BIOS "auto power-on after AC loss" setting are important), home **internet reliability**, cooling/noise, and **hardware failure is yours to fix**; ongoing OS/security maintenance; upfront capital outlay.

### Key deciding factors for the deeper discussion

- **Home power + internet reliability** — frequent outages strongly favor a VPS.
- **Zero monthly cost (mini PC) vs. zero maintenance (VPS).**
- **Capital expense up front vs. small recurring operating cost.**
- **Future scope** — if the box will eventually host multiple agents/workloads, a mini PC amortizes better and RAM is cheap to add.
- **Risk tolerance** — third-party termination risk (notably Oracle's free tier) vs. owning and babysitting physical hardware.

*Note: the entire software stack is deliberately portable — moving between hosts requires only copying the code, the SQLite DB, the secrets, and the token files, then installing the runtime and a systemd unit. Host choice does not lock anything in.*
