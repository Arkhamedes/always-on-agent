#!/usr/bin/env python3
"""
The idea agent -- "what should we do next?", as a role.

The reviewer judges a change, the explainer teaches the code, the idea agent
SURVEYS a codebase for what is worth doing next. One read-only pass over a
shallow clone of the default branch, returning ideas grouped EXACTLY three
ways -- BUG FIXES, OPTIMIZE & CLEAN, FEATURES -- each anchored to the files
that motivated it. It exists because the owner steers projects from a phone:
he rarely reads the code himself, so the agent must be the one that notices
what the codebase needs.

  {"repo": "<owner/name>", "focus": "<optional angle>"}  -> short chat list
  {"repo": "<owner/name>", "doc": true}                  -> HTML deep report

Advisory only: it never edits, opens PRs, or files issues -- a promising
idea becomes a coder task only when the user dispatches one. Runs under the
reviewer/explainer hostile-input discipline (read-only plan-mode claude -p,
no Bash, no web tools, token stripped from the checkout), reusing the
explainer's run/clone/document plumbing.
"""

import sys
import json
import tempfile
import subprocess

import explainer
from github_app import token_for
from task_store import get_task, update_task
from agentlog import log
import persona
import usage_limit

IDEAS_PROMPT = """You are surveying a codebase for its owner, who steers this project from a PHONE and rarely gets to read the code himself -- you are his eyes on it. The repository {repo} is checked out (default branch) in the current directory.
{focus}
Ground yourself first: if CLAUDE.md or AGENTS.md exists at the repo root, read them -- they record the project's constraints and DELIBERATE decisions; never propose undoing what they document as chosen. Then read broadly: entry points, the largest modules, error paths, TODO/FIXME markers. You are read-only; modify nothing.

Report the best ideas in EXACTLY three groups, in this order:
BUG FIXES -- defects, races, unhandled failure paths you can point at in the code.
OPTIMIZE & CLEAN -- simplification, duplication, dead code, perf, wrong-altitude code.
FEATURES -- capabilities the codebase is clearly shaped for but doesn't have yet.

Rules:
- 2-3 ideas per group, ranked by value to the owner. ONE line each: "file.py: the idea -- why it matters". Anchor every idea to its exact file (and function); no generic advice that could apply to any repo.
- An idea must come from something you actually read, not from what repos usually need.
- PLAIN TEXT only, no markdown (this goes to a Telegram message). Under 250 words.
- End with one line offering next steps: any idea can become a coder task, or ask for the in-depth version."""

IDEAS_DOC_PROMPT = """You are writing an in-depth improvement survey of a codebase for its owner, who asked for the long-form version. The repository {repo} is checked out (default branch) in the current directory.
{focus}
Ground yourself first: if CLAUDE.md or AGENTS.md exists at the repo root, read them -- they record the project's constraints and DELIBERATE decisions; never propose undoing what they document as chosen. Read broadly and deeply: entry points, the largest modules, error paths, TODO/FIXME markers. You are read-only; modify nothing.

Structure the findings as EXACTLY three numbered sections after the component map -- BUG FIXES, OPTIMIZE & CLEAN, FEATURES. For each idea: the exact file (and function), the evidence you read, why it matters to the owner, a sketch of the approach, and a rough effort call (small / medium / large). Rank ideas within each section by value. An idea must come from something you actually read.

{doc_format}"""


def find_ideas(repo, focus=None, doc=False):
    """Survey a repo's default branch for improvement ideas. Returns chat
    prose, or a full HTML document when doc=True."""
    clone_url = f"https://github.com/{repo}.git"
    try:
        # Least privilege, like the explainer: surveying needs read only.
        token = token_for(repo, {"contents": "read"})
        clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    except Exception as e:
        log(f"idea agent: no App token for {repo} ({e}); trying public access")
    focus_line = (f"\nThe owner asked to focus on: {focus}\n" if focus else "")
    with tempfile.TemporaryDirectory() as work:
        subprocess.run(["git", "clone", "-q", "--depth", "1", clone_url, work],
                       check=True)
        # The token must not sit in .git/config while the model reads an
        # untrusted checkout; nothing is fetched after this point.
        subprocess.run(["git", "remote", "remove", "origin"], cwd=work,
                       check=True)
        if doc:
            prompt = IDEAS_DOC_PROMPT.format(
                repo=repo, focus=focus_line, doc_format=explainer.DOC_FORMAT)
            label = "ideas-doc"
        else:
            prompt = IDEAS_PROMPT.format(repo=repo, focus=focus_line)
            label = "ideas"
        prompt += persona.line()
        timeout = explainer.DOC_TIMEOUT if doc else explainer.TIMEOUT_SECONDS
        return explainer._claude_explain(work, prompt, label, timeout=timeout)


def run_ideas_task(task_id):
    """Process one idea-agent task. Same state shape as the other workers."""
    task = get_task(task_id)
    if not task:
        return f"Task {task_id} not found."

    update_task(task_id, status="running", inc_attempts=True)
    try:
        op = json.loads(task["instruction"] or "{}")
        repo = op.get("repo")
        if not repo:
            raise ValueError("I need a repo (owner/name) to survey.")
        doc = bool(op.get("doc"))
        answer = find_ideas(repo, op.get("focus") or None, doc=doc)
    except Exception as e:
        err = explainer._redact(str(e))[:1200]
        update_task(task_id, status="failed", result={"error": err})
        return explainer._cap(usage_limit.notice(e)
                              or f"Idea survey failed: {err}")

    if doc:
        html = explainer._extract_html(answer)
        if html:
            fname = explainer._doc_name(f"ideas-{repo}")
            update_task(task_id, status="done",
                        result={"summary": f"doc {fname} ({len(html)} chars)"})
            return {"text": "Improvement survey attached -- open it in your "
                            "browser.",
                    "filename": fname, "document": html.encode("utf-8")}
        # No usable HTML came back -- degrade to whatever prose it wrote
        # rather than failing a finished run.

    update_task(task_id, status="done", result={"summary": answer[:300]})
    return explainer._cap(answer)


def main():
    from coding_agent import load_env   # deferred: keeps import cost off the box
    load_env()
    args = [a for a in sys.argv[1:] if a != "--doc"]
    doc = "--doc" in sys.argv[1:]
    if not args:
        sys.exit("Usage: python idea_agent.py [--doc] <owner/repo> "
                 "[focus ...]")
    repo, focus = args[0], " ".join(args[1:]).strip() or None
    answer = find_ideas(repo, focus, doc=doc)
    if doc:
        html = explainer._extract_html(answer)
        if not html:
            sys.exit("no HTML document in the reply:\n" + answer[:500])
        out = explainer._doc_name(f"ideas-{repo}")
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"wrote {out} ({len(html)} chars)")
    else:
        print(answer)


if __name__ == "__main__":
    main()
