# ADR-0002: Mail watches and reminders piggyback on existing loops; one multi-scope Google token

- **Status:** accepted
- **Date:** 2026-07-06

## Context

Gmail watching (alert on keywords/senders) and timed reminders both imply
"check something periodically". The box is a 1 GB e2-micro already running
four threads (poll, brain, worker, scheduler); every additional loop or
dependency bites the RAM/CPU budget (see `AGENTS.md`, ADR-0001). Separately,
each new Google API could mean a new credential to mint, store, and rotate.

## Decision

- **No new poll loops.** Mail watches are checked only when the morning /
  evening digest already runs (`lifeos._mail_lines` →
  `mailwatch.check_watches`), or when the user explicitly dispatches a
  check. Reminders are delivered inside the EXISTING 60 s scheduler tick
  (`lifeos._deliver_due_reminders`), directly via the listener's Telegram
  sender so the FIFO worker queue can never delay them.
- **One token, many scopes.** All Google access (calendar events, free/busy,
  sheets, gmail read-only) rides the single `token.json` refresh token,
  re-minted with `test/google_reauth.py` on the laptop whenever scopes grow.
  Modules needing scopes beyond their historical grant load the token
  WITHOUT a scope filter and fail cleanly (caught, user-visible pointer to
  the re-auth script) while an older, narrower token is in place — code can
  deploy before the re-auth happens, in expand-contract spirit.
- **Read-only mail.** The agent never sends, labels, or deletes email;
  `gmail.readonly` is the ceiling until an ADR says otherwise.

## Alternatives rejected

- Dedicated mail-poll thread or shorter interval — more wakeups, more RAM
  pressure, and twice-daily visibility already matches the digest cadence
  the user asked for.
- Gmail push notifications (Pub/Sub watch API) — needs an inbound endpoint
  or a Pub/Sub subscription poller; violates outbound-only, adds GCP moving
  parts to the free tier.
- Reminders as `tasks` rows through the worker — a long coder build would
  sit in front of a time-critical ping; the scheduler tick already exists
  and is idle-cheap.
- Per-API OAuth tokens — more secrets to place by hand on the box, no
  isolation benefit for a single-user system.

## Revisit trigger

Reminders needing sub-minute precision or recurrence; the user wanting
near-real-time mail alerts (that is the Pub/Sub discussion); any need to
ACT on email (send/label), which changes the scope ceiling and the risk
profile; the box outgrowing the e2-micro.
