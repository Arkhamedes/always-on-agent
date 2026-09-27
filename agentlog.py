#!/usr/bin/env python3
"""
Tiny logging helper shared across the agent.

Two things:
  log(msg)        -- print a timestamped line, flushed to disk immediately
                     (so a hung process still records what it was doing).
  with timed(x):  -- log "x -- start" now and "x -- done in Ns" on exit. If the
                     wrapped call hangs, you get the "start" and never the "done"
                     -- which is exactly how you spot the freeze and time it.

Timestamps are UTC, matching the rest of the system. Output goes to stdout;
run_listener.sh redirects that to agent.log in the repo dir.
"""

import sys
import time
import datetime
from contextlib import contextmanager


def log(msg):
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


@contextmanager
def timed(label):
    log(f"{label} -- start")
    t0 = time.monotonic()
    try:
        yield
    finally:
        log(f"{label} -- done in {time.monotonic() - t0:.1f}s")