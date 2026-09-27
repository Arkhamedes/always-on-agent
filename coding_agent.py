#!/usr/bin/env python3
"""
The coding agent worker -- now two-phase: plan, then execute.

Every coder task starts in PLAN MODE (read-only): Claude analyzes the codebase
and self-triages the change as trivial or complex.
  - trivial  -> it resumes that same session and executes immediately (context
                carried, no re-exploration), then opens/updates a PR.
  - complex  -> it returns the plan and STOPS. The orchestrator relays it for
                your approval; on approval, execute_approved_task() runs a fresh,
                plan-guided execution.
  - ambiguous -> (either triage) the plan phase returns open questions instead
                of guessing; the task parks as 'awaiting_clarification' and the
                orchestrator re-queues it with your answers folded in.

The plan approval is the guardrail for complex changes; trivial changes go
straight to a PR (still gated by your merge review).

Every execution ends with a SELF-REVIEW GATE before the PR opens
(`_review_gate`): reviewer.review_worktree() reads the uncommitted diff with
fresh eyes, blocking findings get one fix pass, then a re-review -- up to
MAX_REVIEW_ROUNDS reviews. Fail-open: a reviewer error never blocks the PR;
the outcome lands in the PR body and the Telegram reply either way.

ADR-0006 additions:
  - SPEC FILES: a task can carry spec_file (a knowledge-base filename); the
    full spec is read fresh at plan time AND execute time and appended to
    the task text -- the row stores the reference, never the content.
  - MILESTONES: complex plans also return 2-6 ordered milestones. Approved
    execution runs one bounded edit pass per milestone on the same tree
    (progress ping to Telegram after each), then the normal single
    self-review + PR at the _finish choke point. A pass that dies fails
    open: earlier milestones' work ships as a clearly-labeled partial PR
    (or the task fails outright if nothing had landed yet).
  - CI STATUS: after a PR opens/updates, _ci_line() polls the Checks API
    (bounded ~3 min, read-only, needs the App's Checks:Read permission) and
    appends passed/failed/still-running/none-configured to the Telegram
    reply. Purely informational -- it never blocks a PR.

CLI:    python coding_agent.py "the task to do"
Import: from coding_agent import load_env, process_task, execute_approved_task
"""

import os
import sys
import json
import time
import uuid
import tempfile
import subprocess

import requests

import envfile
import reviewer
import librarian
from github_app import token_for
from task_store import init_db, create_task, update_task, get_task
from agentlog import log
from claude_ops import run_claude

BASE = "main"
MAX_TURNS = 8
TIMEOUT_SECONDS = 600
MAX_REVIEW_ROUNDS = 2     # reviews per task; fix passes run between them
SPEC_MAX_CHARS = 60_000   # spec text fed into a phase prompt (ADR-0006)
MAX_MILESTONES = 6        # bounded passes chained per complex task (ADR-0006)
CI_WAIT_SECONDS = 180     # bounded post-PR poll of the Checks API
CI_POLL_SECONDS = 15
CI_START_GRACE = 60       # no check runs after this -> repo has no CI

PLAN_PROMPT = """{task}

You are in PLAN MODE (read-only -- do NOT modify files). Analyze this task against the codebase, then respond with ONLY a JSON object and nothing else:
{{"triage": "trivial" or "complex", "plan": "<concise plan: what will change, which files, and why>", "questions": ["<clarifying question>", ...], "milestones": ["<ordered self-contained step>", ...]}}

TRIVIAL = small, localized, low-risk (copy/text/UI tweak, one self-contained function, a config value).
COMPLEX = touches multiple files, changes data flow or interfaces, affects backend/architecture, or carries meaningful risk.
When unsure, choose complex.

MILESTONES: when triage is complex, ALSO split the plan into 2-{max_milestones} ordered milestones -- each one self-contained edit step that leaves the repo consistent (compiles, imports clean) on its own; later milestones may build on earlier ones. One imperative line each. Leave the list empty for trivial tasks or when the work is genuinely one indivisible step.

QUESTIONS: if the task is AMBIGUOUS -- a decision materially changes what gets built (e.g. "add auth": session or token based?) and the codebase doesn't answer it -- list the specific questions instead of guessing. Leave the list empty when the task is clear; do not pad it with nice-to-know questions. Output only the JSON."""

MILESTONE_PROMPT = """{task}

Follow this approved plan:
{plan}

The work is split into ordered milestones:
{milestone_list}

{done_note}Your job NOW: implement ONLY milestone {number} -- {milestone} -- by editing files. Stay within its scope; later milestones run as later passes. Do NOT run git or tests -- the system handles commits."""

REVIEW_FIX_PROMPT = """A code review of your uncommitted changes found blocking issues:

{findings}

Fix exactly these issues by editing files only. Keep the changes minimal -- do not refactor beyond the fixes, and do NOT run git or tests (the system handles commits)."""


def load_env(path="~/agent_env.sh"):
    envfile.load(path)


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True)


def has_changes(repo_dir: str) -> bool:
    out = subprocess.run(["git", "status", "--porcelain"],
                         cwd=repo_dir, capture_output=True, text=True, check=True)
    return bool(out.stdout.strip())


def _run_claude(extra_args, cwd, prompt, label="claude"):
    return run_claude(extra_args, cwd=cwd, prompt=prompt,
                      timeout=TIMEOUT_SECONDS, label=label, role="coder")


def _agent_summary(data) -> str:
    if data.get("subtype") != "success" or data.get("is_error"):
        raise RuntimeError(f"Claude Code did not finish cleanly:\n{data}")
    return data.get("result", "(no summary returned)")


def _load_spec(spec_file):
    """Full text of a knowledge-base spec, capped. A wrong filename fails
    the task immediately (with the name it looked for) rather than building
    the wrong thing from just the one-line instruction."""
    try:
        name, text = librarian.read_file(spec_file)
    except KeyError:
        raise RuntimeError(
            f"spec file '{spec_file}' not found in the knowledge base -- "
            "check the exact filename (recent uploads are listed in the "
            "orchestrator's context).")
    if not text:
        raise RuntimeError(f"spec file '{name}' isn't readable as text.")
    if len(text) > SPEC_MAX_CHARS:
        log(f"spec {name} truncated to {SPEC_MAX_CHARS} chars")
        text = text[:SPEC_MAX_CHARS]
    return text


def _with_spec(instruction, spec_file):
    """The task text a phase actually sees. The spec is read FRESH from the
    knowledge base each phase (the tasks row stores only the reference --
    ADR-0006), so a spec amended between clarification/approval rounds is
    picked up by the next phase."""
    if not spec_file:
        return instruction
    return (f"{instruction}\n\nFULL SPECIFICATION ({spec_file}) -- the "
            f"source of truth for this task:\n\n{_load_spec(spec_file)}")


def _ci_line(repo, sha):
    """One line describing the head commit's CI outcome: a bounded,
    read-only poll of the Checks API after the PR exists. Every failure
    mode degrades to a note -- CI informs the human merge review, it never
    blocks a PR (ADR-0006). Needs the App's 'Checks: Read' permission."""
    try:
        token = token_for(repo, {"checks": "read"})
    except Exception as e:
        log(f"ci: no checks-read token for {repo}: {e}")
        return ("CI: status unavailable -- grant the GitHub App the "
                "'Checks: Read' permission to see results here.")
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json"}
    url = f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs"
    start = time.time()
    while True:
        try:
            resp = requests.get(url, headers=headers,
                                params={"per_page": 50}, timeout=15)
            resp.raise_for_status()
            runs = resp.json().get("check_runs", [])
        except Exception as e:
            log(f"ci: check-runs read failed: {e}")
            return f"CI: couldn't read check status ({str(e)[:120]})."
        if runs and all(r.get("status") == "completed" for r in runs):
            bad = [r for r in runs if r.get("conclusion") not in
                   ("success", "neutral", "skipped")]
            if bad:
                names = ", ".join(r.get("name", "?") for r in bad[:5])
                return f"CI: FAILED -- {names} (details on the PR)."
            return f"CI: passed ({len(runs)} check(s))."
        elapsed = time.time() - start
        if not runs and elapsed >= CI_START_GRACE:
            return "CI: none configured on this repo (no checks started)."
        if elapsed >= CI_WAIT_SECONDS:
            return (f"CI: still running after {CI_WAIT_SECONDS // 60} min -- "
                    "results land on the PR's checks tab.")
        time.sleep(CI_POLL_SECONDS)


def _plan_phase(task_text, work):
    """Read-only analysis + triage. Returns (triage, plan, questions,
    milestones, session_id). Milestones only ever apply to complex tasks."""
    data = _run_claude(
        ["--permission-mode", "plan", "--max-turns", str(MAX_TURNS)],
        work, PLAN_PROMPT.format(task=task_text,
                                 max_milestones=MAX_MILESTONES), label="plan")
    parsed = reviewer.extract_json(data.get("result", ""))
    triage = (parsed.get("triage") or "complex").lower()
    if triage not in ("trivial", "complex"):
        triage = "complex"                       # safe default: show the plan
    questions = [q for q in (parsed.get("questions") or [])
                 if isinstance(q, str) and q.strip()]
    milestones = [m.strip() for m in (parsed.get("milestones") or [])
                  if isinstance(m, str) and m.strip()][:MAX_MILESTONES]
    if triage != "complex" or len(milestones) < 2:
        milestones = []          # one indivisible pass; no milestone loop
    log(f"triage -> {triage}"
        + (f", {len(questions)} question(s)" if questions else "")
        + (f", {len(milestones)} milestone(s)" if milestones else ""))
    plan = parsed.get("plan") or data.get("result", "")
    return triage, plan, questions, milestones, data.get("session_id")


def _notify(notify, text):
    """Progress ping to Telegram, best-effort -- a send hiccup must never
    kill a run mid-milestone."""
    if not notify:
        return
    try:
        notify(text)
    except Exception as e:
        log(f"notify failed (non-fatal): {e}")


def _changed_file_count(repo_dir):
    out = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir,
                         capture_output=True, text=True, check=True).stdout
    return len([ln for ln in out.splitlines() if ln.strip()])


def _run_milestones(work, task_text, plan, milestones, notify):
    """One bounded edit pass per milestone, sequentially, in the same
    working tree (ADR-0006: the long horizon comes from chaining bounded
    passes, never from raising a budget). Returns (summary, failed_at):
    failed_at is None, or the 1-based number of the milestone whose pass
    died -- earlier milestones' work stays in the tree (fail open; the
    caller ships it as a clearly-labeled partial PR)."""
    listed = "\n".join(f"{i}. {m}" for i, m in enumerate(milestones, start=1))
    summaries = []
    for i, m in enumerate(milestones, start=1):
        done_note = (f"Milestones 1-{i-1} are already implemented in this "
                     "working tree.\n\n" if i > 1 else "")
        prompt = MILESTONE_PROMPT.format(
            task=task_text, plan=plan, milestone_list=listed,
            done_note=done_note, number=i, milestone=m)
        try:
            data = _run_claude(
                ["--permission-mode", "acceptEdits", "--disallowedTools",
                 "Bash", "--max-turns", str(MAX_TURNS)],
                work, prompt, label=f"milestone-{i}")
            summaries.append(_agent_summary(data)[:500])
        except Exception as e:
            log(f"milestone {i}/{len(milestones)} failed: {e}")
            summaries.append(f"FAILED: {str(e)[:300]}")
            _notify(notify, f"Milestone {i}/{len(milestones)} failed -- "
                            "shipping what's already done for review.")
            return ("\n".join(f"Milestone {j}: {s}" for j, s
                              in enumerate(summaries, start=1)), i)
        _notify(notify, f"Milestone {i}/{len(milestones)} done -- {m[:80]} "
                        f"({_changed_file_count(work)} file(s) changed so far)")
    return ("\n".join(f"Milestone {j}: {s}" for j, s
                      in enumerate(summaries, start=1)), None)


def _review_gate(work, instruction):
    """Self-review loop before the PR opens: review the uncommitted diff,
    have Claude fix any blocking findings, re-review. Fail-open by design --
    a broken reviewer must never block delivery (the human merge review
    stays the final gate). Returns the gate state for _review_note()."""
    state = {"rounds": 0, "fixes": 0, "review": None, "error": None}
    try:
        diff = reviewer.staged_diff(work)
        for _ in range(MAX_REVIEW_ROUNDS):
            state["review"] = reviewer.review_worktree(work, instruction, diff)
            state["rounds"] += 1
            blocking = reviewer.blocking_findings(state["review"])
            log(f"self-review round {state['rounds']}: "
                f"{state['review']['verdict']}, {len(blocking)} blocking")
            if not blocking or state["rounds"] == MAX_REVIEW_ROUNDS:
                break
            findings = "\n".join(
                f"- {f.get('file', '?')}: {f.get('issue', '')}"
                + (f" Suggested fix: {f['suggestion']}"
                   if f.get("suggestion") else "")
                for f in blocking)
            _run_claude(
                ["--permission-mode", "acceptEdits", "--disallowedTools",
                 "Bash", "--max-turns", str(MAX_TURNS)],
                work, REVIEW_FIX_PROMPT.format(findings=findings),
                label=f"review-fix-{state['rounds']}")
            state["fixes"] += 1
            new_diff = reviewer.staged_diff(work)
            if new_diff == diff:    # the fix pass changed nothing; this
                break               # round's findings stand -- don't re-review
            diff = new_diff
    except Exception as e:
        log(f"self-review error (PR is not blocked): {e}")
        state["error"] = str(e)
    return state


def _review_note(state):
    """Render the self-review outcome as (PR-body section, Telegram line)."""
    if state["review"] is None:
        err = (state["error"] or "no review ran")[:500]
        return (f"\n\n### Agent self-review\nThe reviewer errored; this PR "
                f"is UNREVIEWED: {err}",
                "Self-review errored -- treat this PR as unreviewed.")
    review, rounds, fixes = state["review"], state["rounds"], state["fixes"]
    err_note = (f"\n(The review loop aborted early: {state['error'][:500]})"
                if state["error"] else "")
    blocking = reviewer.blocking_findings(review)
    if blocking:
        listed = "\n".join(f"- **{f.get('file', '?')}** -- "
                           f"{f.get('issue', '')[:300]}" for f in blocking)
        note = (f"\n\n### Agent self-review: UNRESOLVED\n{rounds} review "
                f"round(s), {fixes} fix pass(es); blocking findings remain:\n"
                f"{listed}{err_note}")
        line = (f"Self-review: {len(blocking)} blocking finding(s) remain -- "
                "details in the PR description.")
    else:
        others = [f for f in review.get("findings", [])
                  if f.get("severity") != "blocking"]
        listed = "\n".join(f"- [{f['severity']}] {f.get('file', '?')} -- "
                           f"{f.get('issue', '')[:300]}" for f in others[:8])
        note = (f"\n\n### Agent self-review: passed\n{rounds} review "
                f"round(s), {fixes} fix pass(es). "
                f"{review.get('summary', '')[:1000]}"
                + (f"\n\nNon-blocking notes:\n{listed}" if listed else "")
                + err_note)
        line = f"Self-review: passed ({rounds} round(s), {fixes} fix(es))."
    return note[:8000], line


def _finish(work, repo, base_branch, branch, instruction, title, summary,
            continue_branch):
    """Self-review, then commit, push, and open or update a PR. The gate
    lives HERE -- at the one choke point every PR passes through -- so no
    execution path can skip it. Returns an outcome dict."""
    if not has_changes(work):
        return {"outcome": "no_change", "summary": summary,
                "branch": branch, "pr_url": None}

    review = _review_gate(work, instruction)
    if not has_changes(work):   # the fix pass withdrew the whole change
        return {"outcome": "no_change", "summary": summary,
                "branch": branch, "pr_url": None,
                "review_line": "Self-review: the fix pass withdrew the whole "
                               "change; nothing was left to commit."}
    review_note, review_line = _review_note(review)

    _git(["config", "user.name", "coding-agent[bot]"], cwd=work)
    _git(["config", "user.email",
          "coding-agent@users.noreply.github.com"], cwd=work)
    _git(["add", "-A"], cwd=work)
    _git(["commit", "-m", (title or instruction)[:72]], cwd=work)
    _git(["push", "origin", branch], cwd=work)
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work,
        capture_output=True, text=True, check=True).stdout.strip()

    token = token_for(repo)
    owner = repo.split("/")[0]
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json"}

    if continue_branch:
        prs = requests.get(f"https://api.github.com/repos/{repo}/pulls",
                           headers=headers,
                           params={"head": f"{owner}:{branch}", "state": "open"})
        prs.raise_for_status()
        items = prs.json()
        if items and review_note:
            # Best-effort: append this update's self-review to the PR body
            # so the merge review sees it. Never blocks the PR (fail-open).
            try:
                requests.patch(
                    items[0]["url"], headers=headers,
                    json={"body": ((items[0].get("body") or "")
                                   + review_note)[:65000]})
            except Exception as e:
                log(f"PR body update failed (non-fatal): {e}")
        return {"outcome": "pr_open", "summary": summary, "branch": branch,
                "pr_url": items[0]["html_url"] if items else None,
                "continued": True, "review_line": review_line,
                "ci_line": _ci_line(repo, head_sha)}

    pr = requests.post(
        f"https://api.github.com/repos/{repo}/pulls",
        headers=headers,
        json={"title": title or instruction, "head": branch, "base": base_branch,
              "body": f"**Task:** {instruction}\n\n**Agent summary:**\n{summary}"
                      f"{review_note}\n\n"
                      "_Generated by the coding agent (Claude Code). "
                      "Review before merging._"})
    pr.raise_for_status()
    return {"outcome": "pr_open", "summary": summary, "branch": branch,
            "pr_url": pr.json()["html_url"], "continued": False,
            "review_line": review_line, "ci_line": _ci_line(repo, head_sha)}


def _do_coding_work(instruction, repo, base_branch, title=None,
                    continue_branch=None, spec_file=None):
    """Plan + triage. Trivial -> execute now (resume). Complex -> return plan."""
    task_text = _with_spec(instruction, spec_file)   # fail fast on a bad name
    clone_url = f"https://x-access-token:{token_for(repo)}@github.com/{repo}.git"
    with tempfile.TemporaryDirectory() as work:
        _git(["clone", clone_url, work], cwd=".")
        if continue_branch:
            _git(["checkout", continue_branch], cwd=work)

        triage, plan, questions, milestones, session_id = _plan_phase(
            task_text, work)
        if questions:                # ambiguity trumps triage: ask, don't guess
            return {"outcome": "awaiting_clarification", "plan": plan,
                    "questions": questions, "branch": None}
        if triage == "complex":
            return {"outcome": "awaiting_approval", "plan": plan,
                    "milestones": milestones, "branch": None}

        # Trivial: execute in the SAME session/clone (context carried).
        if continue_branch:
            branch = continue_branch
        else:
            branch = f"agent/task-{uuid.uuid4().hex[:8]}"
            _git(["checkout", "-b", branch], cwd=work)

        data = _run_claude(
            ["--resume", session_id, "--permission-mode", "acceptEdits",
             "--disallowedTools", "Bash", "--max-turns", str(MAX_TURNS)],
            work, "The plan is approved. Implement it by editing files only. "
                  "Do NOT run git or tests -- the system handles commits.",
            label="resume-execute")
        return _finish(work, repo, base_branch, branch, instruction, title,
                       _agent_summary(data), continue_branch)


def _execute_approved_plan(instruction, plan, repo, base_branch, title=None,
                           continue_branch=None, spec_file=None,
                           milestones=None, notify=None):
    """Complex task, post-approval: fresh clone, execute guided by the plan.
    With milestones (ADR-0006): one bounded pass per milestone on the same
    tree, a Telegram ping after each, then the single _finish choke point.
    A milestone pass that dies fails OPEN when earlier ones already built
    something -- that work ships as a clearly-labeled partial PR."""
    task_text = _with_spec(instruction, spec_file)   # re-read: specs can change
    clone_url = f"https://x-access-token:{token_for(repo)}@github.com/{repo}.git"
    with tempfile.TemporaryDirectory() as work:
        _git(["clone", clone_url, work], cwd=".")
        if continue_branch:
            _git(["checkout", continue_branch], cwd=work)
            branch = continue_branch
        else:
            branch = f"agent/task-{uuid.uuid4().hex[:8]}"
            _git(["checkout", "-b", branch], cwd=work)

        failed_at = None
        if milestones:
            summary, failed_at = _run_milestones(work, task_text, plan,
                                                 milestones, notify)
            if failed_at is not None and not has_changes(work):
                raise RuntimeError(
                    f"milestone {failed_at}/{len(milestones)} failed before "
                    f"any change landed:\n{summary}")
        else:
            prompt = (f"{task_text}\n\nFollow this approved plan:\n{plan}\n\n"
                      "Implement it by editing files only. Do NOT run git or "
                      "tests -- the system handles commits.")
            data = _run_claude(
                ["--permission-mode", "acceptEdits", "--disallowedTools",
                 "Bash", "--max-turns", str(MAX_TURNS)],
                work, prompt, label="fresh-execute")
            summary = _agent_summary(data)

        out = _finish(work, repo, base_branch, branch, instruction, title,
                      summary, continue_branch)
        if failed_at is not None:
            out["partial_note"] = (
                f"⚠ Only milestones 1-{failed_at - 1} of {len(milestones)} "
                f"completed (milestone {failed_at} failed) -- the PR holds "
                "what's done.")
        return out


def _outcome_message(out):
    line = out.get("review_line")
    if out["outcome"] == "no_change":
        return ("No file changes were needed."
                + (f"\n{line}" if line else "") + f"\n\n{out['summary']}")
    verb = "Updated PR" if out.get("continued") else "PR opened"
    url = out["pr_url"] or "(branch updated; no open PR found)"
    ci = out.get("ci_line")
    partial = out.get("partial_note")
    return (f"{verb}: {url}" + (f"\n{partial}" if partial else "")
            + (f"\n{line}" if line else "") + (f"\n{ci}" if ci else "")
            + f"\n\nSummary: {out['summary']}")


def process_task(task_id):
    """Run one NEW coder task: plan + triage, then trivial-execute or hold the
    plan for approval."""
    task = get_task(task_id)
    if not task:
        return f"Task {task_id} not found."
    if task["role"] != "coder":
        update_task(task_id, status="failed",
                    result={"error": f"no handler for role '{task['role']}'"})
        return f"No handler for role '{task['role']}' yet."

    update_task(task_id, status="running", inc_attempts=True)
    try:
        out = _do_coding_work(task["instruction"], task["repo"],
                              task["base_branch"], title=task.get("title"),
                              continue_branch=task.get("continue_branch"),
                              spec_file=task.get("spec_file"))
    except Exception as e:
        update_task(task_id, status="failed", result={"error": str(e)})
        return f"Task failed: {e}"

    if out["outcome"] == "awaiting_clarification":
        update_task(task_id, status="awaiting_clarification",
                    result={"plan": out["plan"], "questions": out["questions"]})
        qs = "\n".join(f"{i+1}. {q}" for i, q in enumerate(out["questions"]))
        return ("Before I start, I need a couple of details:\n\n"
                f"{qs}\n\nReply here and I'll get going.")

    if out["outcome"] == "awaiting_approval":
        milestones = out.get("milestones") or []
        update_task(task_id, status="awaiting_approval",
                    result={"plan": out["plan"], "milestones": milestones})
        steps = ""
        if milestones:
            listed = "\n".join(f"{i}. {m}"
                               for i, m in enumerate(milestones, start=1))
            steps = (f"\n\nI'd do it in {len(milestones)} steps (you'll get "
                     f"a ping after each):\n{listed}")
        return ("This one's a bigger change, so here's the plan before I touch "
                f"anything:\n\n{out['plan']}{steps}\n\nApprove it and I'll "
                "implement, or tell me what to change.")

    status = "done" if out["outcome"] == "no_change" else "pr_open"
    update_task(task_id, status=status,
                result={"pr_url": out.get("pr_url"), "summary": out["summary"],
                        "branch": out["branch"]})
    return _outcome_message(out)


def execute_approved_task(task_id, notify=None):
    """Run a previously-planned coder task after the user approved its plan.
    `notify(text)` (the worker's Telegram sender) receives per-milestone
    progress pings; None is fine (CLI runs)."""
    task = get_task(task_id)
    if not task:
        return f"Task {task_id} not found."

    plan, milestones = "", []
    try:
        stored = json.loads(task["result"] or "{}")
        plan = stored.get("plan", "")
        milestones = stored.get("milestones") or []
    except Exception:
        pass

    update_task(task_id, status="running", inc_attempts=True)
    try:
        out = _execute_approved_plan(
            task["instruction"], plan, task["repo"], task["base_branch"],
            title=task.get("title"), continue_branch=task.get("continue_branch"),
            spec_file=task.get("spec_file"), milestones=milestones,
            notify=notify)
    except Exception as e:
        update_task(task_id, status="failed", result={"error": str(e)})
        return f"Task failed: {e}"

    status = "done" if out["outcome"] == "no_change" else "pr_open"
    update_task(task_id, status=status,
                result={"pr_url": out.get("pr_url"), "summary": out["summary"],
                        "branch": out["branch"]})
    return _outcome_message(out)


def main():
    load_env()
    init_db()
    if len(sys.argv) < 2:
        sys.exit('Usage: python coding_agent.py "the task to do"')
    repo = os.environ.get("CODER_REPO", "")
    if not repo:
        sys.exit("CODER_REPO is not set (owner/name the coder targets by "
                 "default). Add it to agent_env.sh or run setup.py.")
    task_id = create_task(source="cli", repo=repo, instruction=sys.argv[1])
    print(process_task(task_id))


if __name__ == "__main__":
    main()