#!/usr/bin/env python3
"""
Persona -- one optional AGENT_PERSONA env var (agent_env.sh) that gives the
agent's user-facing voices a character. Unset = neutral voice everywhere.

Leaf module: stdlib only, no project imports (same property as envfile.py).

Prompt builders append line() to user-facing model calls: orchestrator
replies, the morning digest, the psychologist, researcher/news, the
librarian's ask, and the explainer (chat and doc modes). The persona colors
TONE ONLY -- required output structure (the orchestrator's JSON action),
format/length rules, and facts must never change with it, and the line says
so explicitly.

Deliberately persona-free: the coder and reviewer (PR text is a public,
professional artifact) and the hardcoded plain-text fallbacks (a sudden
neutral voice doubles as a model-failure signal).
"""

import os

PERSONA_ENV = "AGENT_PERSONA"


def line():
    """One instruction block adopting the configured voice, or '' if unset."""
    p = os.environ.get(PERSONA_ENV, "").strip()
    if not p:
        return ""
    return (
        "\n\nVOICE: Write all user-facing text in the voice of "
        f"{p} -- their diction, mannerisms, and attitude. The persona "
        "changes tone ONLY; every rule above still binds exactly: required "
        "output structure (including JSON shapes), format and length "
        "limits, facts, numbers, dates, names, and links.")
