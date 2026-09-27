#!/usr/bin/env python3
"""
Usage-limit detection -- turns a raw claude -p failure into one clear
Telegram line ("session limit reached -- resets 1:40am") instead of a
stdout/stderr dump. Leaf module: stdlib only, no project imports.

Errors arrive as whole exception texts that embed the CLI's output (every
claude helper raises RuntimeError with stdout+stderr in it), so detection
is substring-based and deliberately loose: the CLI's wording varies across
versions ("Claude AI usage limit reached", "5-hour limit reached (resets
1:40am)"). Extraction of the reset time is best-effort; the notice reads
fine without it.
"""

import re

_LIMIT = re.compile(r"usage limit|limit reached", re.IGNORECASE)
_RESET = re.compile(r"resets?\s*(?:at\s*)?([^\n∙•|\"'}\]]{1,30})",
                    re.IGNORECASE)


def notice(err):
    """One friendly line if `err` looks like a Claude usage-limit failure,
    else None (the caller falls back to its normal error message)."""
    text = str(err or "")
    if not _LIMIT.search(text):
        return None
    m = _RESET.search(text)
    when = f" -- resets {m.group(1).strip().rstrip('.,;)')}" if m else ""
    return (f"⚠️ Claude session limit reached{when}. I can't run "
            "model calls until the window resets -- send that again "
            "afterwards.")
