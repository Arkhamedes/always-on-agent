#!/usr/bin/env python3
"""
The research workers: live web answers over Telegram.

Two roles, one module, no new infrastructure:
  - researcher: "what are the world cup matches tomorrow?" -> a direct answer
                with source links.
  - news:       a digest of current top stories (>= `count`, default 10), each
                a short summary + link, optionally focused on a topic ("tech
                news", "different ones than before").

Implementation: the same headless Claude Code CLI the rest of the system runs
on, with its BUILT-IN WebSearch/WebFetch tools enabled (--tools selects them,
--allowedTools auto-permits them headlessly). No Playwright, no scraping stack,
no RAG index -- nothing to maintain on a 1 GB box, and it bills to the flat
Max plan like every other call.

The instruction is JSON from the orchestrator:
  {"kind": "search", "query": "<self-contained question>"}
  {"kind": "news", "topic": "<focus, or 'top world news'>", "count": 10}
"""

import os
import json

from task_store import get_task, update_task
from claude_ops import run_claude
import persona
import usage_limit

MAX_TURNS = 15            # a digest can take several searches
TIMEOUT_SECONDS = 420
MESSAGE_CAP = 3900        # Telegram hard limit is 4096

SEARCH_PROMPT = """Use web search to answer the user's question:

{query}

Rules:
- Search the web; do not answer from memory alone. Prefer fresh, authoritative sources.
- Reply with the answer first, concise and direct, then a short "Sources:" list of bare URLs.
- PLAIN TEXT only -- no markdown formatting (this goes to a plain Telegram message).
- If the answer genuinely can't be found, say so and show what you checked."""

NEWS_PROMPT = """Compile a news digest: {topic}

Rules:
- Use web search across SEVERAL major reputable outlets (e.g. Reuters, AP, BBC, Al Jazeera, The Guardian, CNBC) -- not one outlet's front page.
- At least {count} DISTINCT stories (no two items about the same event).
- Format each item exactly as:
  <n>. <headline> -- <one/two-sentence summary>
  <link>
- One blank line between items. PLAIN TEXT, no markdown.
- Lead with today's date on the first line. Keep the whole digest under 3500 characters."""


def _claude_web(prompt, label):
    """Headless Claude Code run with ONLY the web tools available."""
    data = run_claude(
        ["--tools", "WebSearch,WebFetch",
         "--allowedTools", "WebSearch", "WebFetch",
         "--max-turns", str(MAX_TURNS)],
        cwd=os.path.dirname(os.path.abspath(__file__)), prompt=prompt,
        timeout=TIMEOUT_SECONDS, label=label, role="researcher")
    if data.get("is_error"):
        raise RuntimeError(f"web {label} did not finish cleanly:\n{data}")
    return data.get("result", "").strip()


def _cap(text, limit=MESSAGE_CAP):
    """Fit Telegram's 4096-char limit without chopping a line (or URL) in half."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    return text[:cut if cut > 0 else limit].rstrip()


def web_search(query):
    return _claude_web(SEARCH_PROMPT.format(query=query) + persona.line(),
                       "search")


def news_digest(topic="top world news", count=10):
    return _claude_web(
        NEWS_PROMPT.format(topic=topic or "top world news", count=count)
        + persona.line(),
        "news")


def run_research_task(task_id):
    """Process one researcher/news task. Same state shape as the other workers."""
    task = get_task(task_id)
    if not task:
        return f"Task {task_id} not found."

    update_task(task_id, status="running", inc_attempts=True)
    try:
        op = json.loads(task["instruction"])
        if op.get("kind") == "news":
            answer = news_digest(op.get("topic"), int(op.get("count") or 10))
        else:
            answer = web_search(op.get("query", ""))
    except Exception as e:
        update_task(task_id, status="failed", result={"error": str(e)})
        return usage_limit.notice(e) or f"Web research failed: {e}"

    update_task(task_id, status="done", result={"summary": answer[:300]})
    return _cap(answer)
