# Finance (`finance.py`)

Reads the user's finance Google Sheet and reports the numbers on demand
("Finance pulse"). Deliberately dumb: the sheet owns all formulas and
derivations; the agent just displays label/value pairs, so the sheet can
evolve without code changes.

## Interface

- `run_finance_task(task_id) -> message` — worker entry. Resolves the
  sheet from the `finance_sheet` fact (read directly from the DB for the
  task's chat), reads the Summary tab, formats the pulse. No sheet
  configured → fails with a prompt to send the URL.
- `read_summary(spreadsheet_id) -> [(label, value)]` — also used by
  `dashboard.py` for the Finance card (5-minute cache lives on the
  dashboard side).
- `sheet_id_from(text)` — accepts a bare spreadsheet id or any
  docs.google.com URL form.

## Sheet contract

A tab named `Summary`, column A = label, column B = value, any rows
(read range `Summary!A1:B30`, blank labels skipped). Example rows:
Net worth, Monthly income, Monthly spend, Daily change.

## Setup flow

First finance question → the orchestrator asks for the Sheet URL →
dispatches with `sheet` set → `handle_message` stores it as the
`finance_sheet` fact → subsequent asks just dispatch.

## Auth

Same `token.json` / `get_credentials()` as the secretary. The credential
carries both grants — `calendar.events` + `spreadsheets.readonly` (the
dual-scope re-auth flow is `test/google_reauth.py`, laptop-only). Read-only
by scope, so the agent can never modify the sheet.

## Depends on

`task_store`, `secretary.get_credentials`, `google-api-python-client`.
