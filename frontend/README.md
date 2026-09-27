# Alfred frontend

The dashboard SPA. It talks to `../dashboard.py`'s JSON API and ships as
static files — **no Node runs on the VM, ever**.

## Change how it looks (no coding required)

Every color, spacing step, radius, font, and shadow lives in ONE file:

    src/theme.css        ← edit this, rebuild, done

Components never hard-code colors or sizes; they only reference the variables
defined there. The current look ("Personal OS") is dark-only; accent tints
and borders all derive from `--accent` via `color-mix`, so change `--accent`
and the whole app follows. After editing, rebuild + deploy (below).

Fonts (Hanken Grotesk + JetBrains Mono) load from Google Fonts via
`index.html` — the dashboard is only reachable when the box is online, so
the CDN dependency costs nothing extra.

## Change what's on the page

The card list lives in `src/App.tsx`, in the `CARDS` array at the top:

    const CARDS: CardDef[] = [
      { id: "todos", col: 1 },
      { id: "calendar", col: 1 },
      { id: "finance", col: 2 },
      ...
    ];

`col: 1 | 2` stacks a card into the left/right desktop column (a single
column on phones); cards without `col` fill full-width rows below. Reorder,
remove, or move cards by editing this array — one line per card.

## Add a new card

1. Create `src/cards/MyCard.tsx` — a component that takes typed props and
   renders (no fetching inside cards; data comes from `/api/state` via App).
2. Add its data to `State` in `src/api.ts` (and to `state()` in
   `../dashboard.py` if the backend doesn't send it yet).
3. Register it: add an entry to `CARDS` and a case in `card()` in `App.tsx`.

## Add a write action

1. Backend: add a branch in `handle_write()` in `../dashboard.py` mapping a
   path to an existing `lifeos.py` function (they're thin one-liners).
2. Frontend: call it through the optimistic `write()` helper in `App.tsx`
   (updates the UI instantly, reverts + toasts on failure).

## Build & deploy

    npm install          # first time only
    npm run build        # type-checks, then emits dist/

`dist/` is committed to git. Deploy = copy `dist/` (and `dashboard.py` if it
changed) to the VM at `~/autonomous-agent/frontend/dist`, then
`sudo systemctl restart dashboard`. The Python server serves `dist/` at `/`
automatically.

## Develop locally

    ssh -L 8766:127.0.0.1:8766 <the VM>    # tunnel the API
    npm run dev                             # Vite dev server, proxies /api
