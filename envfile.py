#!/usr/bin/env python3
"""
Env-file loading -- the ONE place that folds agent_env.sh-style files into
os.environ and enforces the metered-key rule (ANTHROPIC_API_KEY must never
shadow the Max OAuth token).

Leaf module: stdlib only, no project imports. That property is load-bearing:
stage.py must load env files and pin AGENT_DB_PATH before importing any
module that reads the environment at import time (task_store's DB_PATH).
"""

import os
import subprocess


def load(*paths):
    """Source each existing shell file into os.environ (later files win),
    then drop ANTHROPIC_API_KEY."""
    for path in paths:
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            continue
        out = subprocess.run(["bash", "-c", f"source '{path}' && env -0"],
                             capture_output=True, check=True).stdout
        for entry in out.split(b"\x00"):
            if not entry:
                continue
            key, _, val = entry.partition(b"=")
            os.environ[key.decode(errors="replace")] = \
                val.decode(errors="replace")
    os.environ.pop("ANTHROPIC_API_KEY", None)
