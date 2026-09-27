#!/usr/bin/env python3
"""
The reviewer -- code review as a first-class role.

One review core, two consumers:
  - The coder calls review_worktree() on its uncommitted changes BEFORE a PR
    opens (the self-review gate in coding_agent.py: review -> fix blocking
    findings -> re-review).
  - The worker routes 'reviewer' tasks to run_review_task(): the user points
    at ANY pull request ("review PR 15") and gets findings on Telegram --
    including PRs the coder did not open.

A review is one read-only plan-mode `claude -p` run inside a checkout of the
code under review: the diff is embedded in the prompt, and the model may
read surrounding files plus the target repo's own CLAUDE.md / AGENTS.md, so
it reviews against THAT repo's conventions. Output is strict JSON (verdict /
findings with severities / summary). Advisory only: this module never
merges, approves, closes, or pushes anything.

The instruction is JSON from the orchestrator:
  {"pr": 15, "repo": "<owner/name>"}
(per docs/data-model.md, tasks.repo is never read for non-coder roles).
"""

import re
import sys
import json
import tempfile
import subprocess
from contextlib import contextmanager

import requests

from github_app import token_for
from task_store import get_task, update_task
from agentlog import log
from claude_ops import run_claude

MAX_TURNS = 15            # enough to read conventions + surrounding code
TIMEOUT_SECONDS = 600
DIFF_CAP = 60_000         # chars of diff embedded in the prompt
MAX_REPORTED = 12         # findings shown in a Telegram report
MESSAGE_CAP = 3900        # Telegram hard limit is 4096

REVIEW_PROMPT = """You are reviewing a code change. The repository it belongs to is checked out in the current directory.

{context}

Ground rules:
- If CLAUDE.md or AGENTS.md exists at the repo root, read it FIRST and review against that repo's OWN stated conventions and invariants -- not generic style preferences.
- Read any surrounding source files you need for context. You are read-only; modify nothing.
- Review ONLY the change below; pre-existing problems in untouched code are out of scope unless the change makes them worse.

Hunt, in priority order:
1. Correctness: logic errors, broken invariants, unhandled failure paths in the changed code, or a change that does not do what its stated task/description says.
2. Violations of the repo's stated conventions (from its CLAUDE.md / AGENTS.md).
3. Security: committed secrets, injection, unsafe subprocess/eval, auth bypass.
4. Schema or public interfaces changed without the docs/migration discipline the repo requires.

Severities: "blocking" = would misbehave at runtime, violates a stated invariant, or is a security hole. "warning" = real but survivable. "nit" = style/polish. Style is NEVER blocking. A correct, convention-abiding change gets verdict "pass" with few or no findings -- do not invent findings to look thorough.

Respond with ONLY a JSON object and nothing else:
{{"verdict": "pass" or "fail", "summary": "<2-3 sentence overall assessment>", "findings": [{{"severity": "blocking" or "warning" or "nit", "file": "<path or path:line>", "issue": "<what is wrong>", "suggestion": "<how to fix, short>"}}]}}
"fail" if and only if at least one finding is blocking.

THE CHANGE (unified diff{truncated}):
{diff}"""

_SEVERITY_ORDER = {"blocking": 0, "warning": 1, "nit": 2}


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True)


def extract_json(text):
    """Best-effort JSON object from a model reply (also used by the coder's
    plan phase -- the single copy lives here; coding_agent imports us)."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
    s, e = t.find("{"), t.rfind("}")
    if s != -1 and e != -1:
        try:
            return json.loads(t[s:e + 1])
        except json.JSONDecodeError:
            pass
    return {}


def _prompt(context, diff):
    truncated = ""
    if len(diff) > DIFF_CAP:
        diff = diff[:DIFF_CAP]
        truncated = ", TRUNCATED -- read the files for the rest"
    return REVIEW_PROMPT.format(context=context, diff=diff, truncated=truncated)


def _claude_review(work_dir, prompt, label):
    """One read-only review run; returns the parsed verdict dict. The prompt
    (with up to DIFF_CAP chars of diff) goes via stdin -- as an argv entry a
    multibyte diff can blow Linux's per-argument byte limit. No Bash and no
    web tools: the diff is untrusted third-party content, and an injected
    instruction must find no channel to act or exfiltrate through."""
    data = run_claude(
        ["--permission-mode", "plan",
         "--disallowedTools", "Bash", "WebFetch", "WebSearch",
         "--max-turns", str(MAX_TURNS)],
        cwd=work_dir, prompt=prompt, prompt_via_stdin=True,
        timeout=TIMEOUT_SECONDS, label=label, role="reviewer")
    if data.get("is_error"):
        raise RuntimeError(f"review [{label}] did not finish cleanly:\n{data}")
    parsed = extract_json(data.get("result", ""))
    if parsed.get("verdict") not in ("pass", "fail"):
        raise RuntimeError(f"review [{label}] returned no usable verdict:\n"
                           f"{data.get('result', '')[:500]}")
    findings, off_enum = [], []
    for f in parsed.get("findings") or []:
        if not isinstance(f, dict):
            continue
        if f.get("severity") not in _SEVERITY_ORDER:
            f["severity"] = "warning"
            off_enum.append(f)
        findings.append(f)
    # Contract: fail <=> a blocking finding exists. If the model said fail
    # but nothing survived as blocking (an off-enum label like "critical"
    # got demoted), fail SAFE: re-promote rather than let the coder's gate
    # read a fail verdict as passed.
    if parsed["verdict"] == "fail" and not any(
            f["severity"] == "blocking" for f in findings):
        for f in off_enum or findings[:1]:
            f["severity"] = "blocking"
        if not findings:
            findings.append({"severity": "blocking", "file": "?",
                             "issue": parsed.get("summary") or
                                      "fail verdict with no findings",
                             "suggestion": ""})
    findings.sort(key=lambda f: _SEVERITY_ORDER[f["severity"]])
    parsed["findings"] = findings
    return parsed


def blocking_findings(review):
    return [f for f in review.get("findings", [])
            if f.get("severity") == "blocking"]


def staged_diff(work_dir):
    """Stage everything and return the staged diff (so new files show up).
    Harmless before the coder's _finish(), which re-stages anyway."""
    _git(["add", "-A"], cwd=work_dir)
    return subprocess.run(["git", "diff", "--cached"], cwd=work_dir,
                          capture_output=True, text=True, check=True).stdout


def review_worktree(work_dir, instruction, diff=None):
    """Review the working tree's uncommitted changes (the coder's pre-PR
    gate). Pass `diff` when the caller already has staged_diff() in hand."""
    if diff is None:
        diff = staged_diff(work_dir)
    if not diff.strip():
        return {"verdict": "pass", "findings": [],
                "summary": "Empty diff -- nothing to review."}
    context = ("The working tree holds UNCOMMITTED changes implementing this "
               f"task:\n{instruction}")
    return _claude_review(work_dir, _prompt(context, diff), "self-review")


@contextmanager
def pr_checkout(repo, pr_number):
    """Yield (work_dir, meta, diff) with the PR's head shallow-checked-out in
    a temp dir -- the shared plumbing under review_pr and the explainer
    (explainer.py imports it). Uses the GitHub App token when the App is
    installed on the repo; falls back to unauthenticated access so public
    repos work too."""
    headers = {"Accept": "application/vnd.github+json"}
    clone_url = f"https://github.com/{repo}.git"
    try:
        # Least privilege: reading a PR needs read only, never a write token.
        token = token_for(repo, {"contents": "read", "pull_requests": "read"})
        headers["Authorization"] = f"Bearer {token}"
        clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    except Exception as e:
        log(f"reviewer: no App token for {repo} ({e}); trying public access")

    api = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    meta_resp = requests.get(api, headers=headers)
    meta_resp.raise_for_status()
    meta = meta_resp.json()
    diff_resp = requests.get(
        api, headers={**headers, "Accept": "application/vnd.github.diff"})
    diff_resp.raise_for_status()

    with tempfile.TemporaryDirectory() as work:
        # One shallow fetch of the PR head instead of clone + fetch: a single
        # network round-trip and no full history on the 1 GB box (the diff
        # comes from the API; the tree is only read for context).
        _git(["init", "-q", work], cwd=".")
        _git(["remote", "add", "origin", clone_url], cwd=work)
        _git(["fetch", "-q", "--depth", "1", "origin",
              f"pull/{pr_number}/head"], cwd=work)
        _git(["checkout", "-q", "--detach", "FETCH_HEAD"], cwd=work)
        # The token must not sit in .git/config while the model reads an
        # untrusted checkout; nothing is fetched after this point.
        _git(["remote", "remove", "origin"], cwd=work)
        yield work, meta, diff_resp.text


def review_pr(repo, pr_number):
    """Check out `repo` at the PR's head and review the PR's diff.
    Returns (review, meta)."""
    with pr_checkout(repo, pr_number) as (work, meta, diff):
        context = (
            f"You are reviewing pull request #{pr_number} of {repo}: "
            f"{meta.get('title') or '(no title)'}\n"
            f"Base branch: {meta.get('base', {}).get('ref', '?')}\n"
            f"PR description:\n{(meta.get('body') or '(none)')[:2000]}")
        return _claude_review(work, _prompt(context, diff),
                              f"pr-review #{pr_number}"), meta


def format_report(review, repo, pr_number, meta):
    """Render a review as a plain-text Telegram report."""
    findings = review.get("findings", [])
    n_blocking = len(blocking_findings(review))
    verdict = ("LOOKS GOOD" if review["verdict"] == "pass"
               else f"{n_blocking} BLOCKING finding(s)")
    lines = [f"Review of {repo} PR #{pr_number} -- "
             f"{meta.get('title') or '(no title)'}",
             f"Verdict: {verdict}", "", review.get("summary", "").strip()]
    if findings:
        lines.append("")
        for f in findings[:MAX_REPORTED]:
            entry = f"- [{f['severity']}] {f.get('file', '?')}: {f.get('issue', '')}"
            if f.get("suggestion"):
                entry += f" Fix: {f['suggestion']}"
            lines.append(entry)
        if len(findings) > MAX_REPORTED:
            lines.append(f"(+{len(findings) - MAX_REPORTED} more)")
    lines += ["", "Advisory only -- I never merge or approve; the call is yours."]
    return _cap("\n".join(lines))


def _cap(text, limit=MESSAGE_CAP):
    """Fit Telegram's 4096-char limit without chopping a line in half."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    return text[:cut if cut > 0 else limit].rstrip()


def _redact(text):
    """Strip embedded x-access-token credentials from error text (a failed
    git command's exception echoes its full URL)."""
    return re.sub(r"x-access-token:[^@\s]+@", "x-access-token:***@", text)


def run_review_task(task_id):
    """Process one reviewer task. Same state shape as the other workers."""
    task = get_task(task_id)
    if not task:
        return f"Task {task_id} not found."

    update_task(task_id, status="running", inc_attempts=True)
    try:
        op = json.loads(task["instruction"] or "{}")
        repo = op.get("repo")
        pr_digits = re.search(r"\d+", str(op.get("pr") or ""))
        if not repo or not pr_digits:
            raise ValueError("I need a repo and a PR number "
                             "(e.g. 'review PR 15 on owner/name').")
        pr_number = int(pr_digits.group())
        review, meta = review_pr(repo, pr_number)
    except Exception as e:
        err = _redact(str(e))[:1200]
        update_task(task_id, status="failed", result={"error": err})
        return _cap(f"PR review failed: {err}")

    update_task(task_id, status="done",
                result={"verdict": review["verdict"],
                        "blocking": len(blocking_findings(review)),
                        "summary": review.get("summary", "")[:300]})
    return format_report(review, repo, pr_number, meta)


def main():
    from coding_agent import load_env   # deferred: coding_agent imports us
    load_env()
    if len(sys.argv) < 3:
        sys.exit("Usage: python reviewer.py <owner/repo> <PR number>")
    repo, pr_number = sys.argv[1], int(sys.argv[2])
    review, meta = review_pr(repo, pr_number)
    print(format_report(review, repo, pr_number, meta))


if __name__ == "__main__":
    main()
