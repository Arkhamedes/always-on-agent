# ADR-0007: Sensitive documents live in Google Drive; the agent gets metadata-only access

- **Status:** proposed
- **Date:** 2026-07-07

## Context

The user wants the agent to "hold" identity documents (passport, ID card)
and credentials, reachable from anywhere. Two hard facts bound the design:
**Telegram bot traffic is not end-to-end encrypted** (anything the bot
sends or receives transits Telegram's servers in a form Telegram can
read), and anything stored on the VM is plaintext to whoever holds the
box. The knowledge base (ADR-0003) is a *retrieval* system — its whole job
is to surface stored text into model prompts and chat replies — which is
exactly the wrong property for secrets.

Google Drive already holds the user's files behind their Google account
(strong auth, encrypted at rest, access-controlled sharing), and the
agent already carries a Google token (`token.json`, ADR-0002).

## Decision

- **Identity documents live in Google Drive, never in the knowledge
  base.** They are not uploaded through Telegram at all. The user keeps
  Drive organized with sensible names/folders — that is the design, not a
  workaround.
- **The agent gets `drive.metadata.readonly` and nothing more.** It can
  search file names/folders and return a `webViewLink`; it **cannot read
  file contents, ever**. A full VM compromise therefore leaks filenames,
  not documents. The librarian's new `drive` op finds a file and replies
  with the link only; opening it requires the user's own Google session
  (files keep default "Restricted" sharing). Telegram sees a useless URL
  and a filename, never bytes.
- **Passwords never enter the system in any form.** Actual secrets live in
  a proper end-to-end password manager on the user's phone (e.g.
  Bitwarden, free tier). The agent may hold *pointers* ("bank login → the
  Bitwarden entry named X") as ordinary knowledge notes — searchable and
  worthless to an attacker.
- Scope growth rides the existing single-token flow (ADR-0002):
  `test/google_reauth.py` re-mints `token.json` with the new scope on the
  laptop; until then the `drive` op fails cleanly with a pointer to the
  re-auth script. The Drive API must be enabled once in the same Google
  Cloud project.

## Alternatives rejected

- An encrypted locker on the VM (`age`/GPG) with Telegram retrieval —
  every retrieval ships the decrypted document *through Telegram's
  servers*, and the key has to live somewhere the box can reach; strictly
  worse than a link that only the user's own Google session can open.
- A password-manager CLI on the VM (Bitwarden CLI / `pass`) — the master
  credential must either sit on the box (compromise reveals everything)
  or transit Telegram per request (leaks the master secret); retrieval
  still transits Telegram. No placement of the master key survives the
  threat model.
- The broader `drive.readonly` scope (content search, full-text `ask`
  over Drive) — would let a compromised box read every document; name
  search + a tidy Drive covers the actual need. Revisit only with a
  concrete use case that names cannot satisfy.
- Storing documents in the knowledge base with index exclusions — leaves
  plaintext bytes on the VM and one bug away from a model prompt;
  exclusion lists are a fragile negative control.

## Revisit trigger

Telegram bots gaining true end-to-end encryption (re-opens direct
delivery); a real need for content search over Drive files (the
`drive.readonly` discussion, with its blast-radius trade-off made
explicit); or the user wanting the agent to *send* documents somewhere,
which is a different risk conversation entirely.
