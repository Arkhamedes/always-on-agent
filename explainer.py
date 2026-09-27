#!/usr/bin/env python3
"""
The explainer -- understanding on demand, as a role.

The reviewer answers "should this merge?"; the explainer answers "what IS
this?". Two shapes, one role (ADR-0008):

  {"repo": "<owner/name>", "pr": 15, "question": "<optional focus>"}
      -> what the PR does, why, how it works, and where to look hardest --
         a briefing BEFORE the human reviews it from a phone.
  {"repo": "<owner/name>", "question": "how does X work?"}
      -> a walkthrough of how something works in that codebase, grounded in
         the actual files, over a shallow clone of the default branch.

Either shape takes "doc": true -- the opt-in deep dive. Instead of a ~200-
word chat message, the run produces a SELF-CONTAINED dark-theme HTML file
delivered as a Telegram document (phones render HTML in the browser; a .md
attachment would open as raw text). The worker sees the returned envelope
{"text", "filename", "document"} and ships it via send_document.

One read-only plan-mode `claude -p` per task inside a checkout of the code,
with the reviewer's hostile-input discipline (the code under explanation is
untrusted content): no Bash, no web tools, read-only token stripped from the
checkout before the model runs. Output is prose (or the HTML file) for the
user -- an explanation, not a verdict. It never posts, comments, or reviews
anywhere.
"""

import re
import sys
import json
import tempfile
import subprocess

from github_app import token_for
from reviewer import pr_checkout
from task_store import get_task, update_task
from agentlog import log
from claude_ops import run_claude
import persona
import usage_limit

MAX_TURNS = 15            # enough to read conventions + the code itself
TIMEOUT_SECONDS = 600
DOC_TIMEOUT = 900         # doc mode also WRITES a whole document
DIFF_CAP = 60_000         # chars of diff embedded in a PR prompt
MESSAGE_CAP = 3900        # Telegram hard limit is 4096

PR_PROMPT = """You are explaining a pull request to the repository's owner, who will review it on a PHONE and wants to genuinely understand it before judging it. The repository is checked out at the PR head in the current directory.

PR #{pr} of {repo}: {title}
Base branch: {base}
PR description:
{body}
{focus}
Ground yourself first: if CLAUDE.md or AGENTS.md exists at the repo root, read it; read the changed files (and their callers) as needed. You are read-only; modify nothing.

Explain, in this order:
1. WHAT changes, in one or two sentences.
2. WHY -- the problem or goal, as the code and description show it.
3. HOW: lead with ONE arrow chain mapping the change through its components in call order (plain arrows on one line, like "listener.py -> decide() -> tasks table -> worker.py"; NO box-drawing art, NO markdown, NO code fences -- Telegram renders none of them). Then prose that only annotates what that map already names -- never introduce structure the map didn't show. Describe each piece by its interface (what goes in, what comes out), not its internals, and anchor every behavior you describe to its exact file (and function).
4. WHERE TO LOOK HARDEST: the riskiest or least obvious parts a careful reviewer should stare at. Not a verdict, not findings -- just orientation.
5. End with one "Stop here --" line naming exactly what you left one level down, so the cutoff reads as a decision. Offer to go deeper.

Rules:
- PLAIN TEXT only, no markdown formatting (this goes to a Telegram message). Aim for ~250 words -- short by construction beats thorough; depth is available on request.
- Do not build the explanation around a worked scenario; state what the change does directly.
- Explain like a sharp colleague talking, not a changelog.

THE CHANGE (unified diff{truncated}):
{diff}"""

REPO_PROMPT = """You are explaining part of a codebase to its owner, over Telegram. The repository {repo} is checked out (default branch) in the current directory.

Their question: {question}

Ground yourself first: if CLAUDE.md or AGENTS.md exists at the repo root, read it; then read the source files the question touches. You are read-only; modify nothing.

Answer in this format (interface-level, map-first, short):
1. LEAD WITH THE MAP: one arrow chain naming the components/files in call or ownership order (like "listener.py -> decide() -> tasks table -> worker.py -> secretary.py"). Plain arrows on one line -- NO box-drawing art, NO markdown, NO code fences (Telegram renders none of them).
2. Then prose that only annotates what the map already names -- never introduce structure the map didn't show. Describe each piece by its interface (what goes in, what comes out), never its internal logic.
3. Anchor every behavior you describe to its exact file (and function). Where code and docs disagree, trust the code and say so.
4. Do not build the answer around a worked scenario. Two exceptions: an algorithm-shaped question gets exactly ONE minimal input->output line right under its contract (like "f([100,50,200], 2) -> [0,0,200]"); a concurrency/invariant question gets ONE short numbered two-actor trace of the decisive race -- that is what makes the guarantee checkable.
5. END WITH THE BOUNDARY: a final "Stop here --" line naming exactly what you left one level down and why, so the cutoff reads as a decision. Offer to go deeper.

Rules:
- Aim for ~200 words -- short by construction beats thorough; depth is available on request.
- If the question doesn't match anything in this repo, say what you looked for and where -- do not improvise an answer.
- PLAIN TEXT only (this goes to a Telegram message). Explain like a sharp colleague talking, not documentation."""

# Doc mode: the opt-in "go deeper" path (ADR-0008). HTML, not markdown --
# phones render an .html attachment in the browser; a .md one opens as raw
# text. Self-contained because Telegram delivers one lone file.
DOC_FORMAT = """Produce a SELF-CONTAINED HTML DOCUMENT -- it will be sent to the owner as a Telegram file attachment and opened in a phone browser.

Requirements:
- Reply with ONLY the HTML document, starting at <!doctype html>. No preamble, no code fences, nothing after the closing html tag.
- Fully self-contained: inline CSS in one style block; NO external scripts, stylesheets, fonts, or images. Simple inline SVG is fine for a flow diagram.
- Dark theme, phone-first: dark background, light text, 16px+ base font, max-width around 700px centered, generous line-height.
- Structure: a title heading, a short table of contents, then NUMBERED sections. Section 1 is the component map (the arrow-chain flow, as styled text or inline SVG). The last section is "What this leaves out" -- the depth boundary, stated as a decision.
- Same grounding discipline as the short form: describe interfaces first, anchor every behavior to its exact file (and function), put code excerpts in pre blocks where they carry the point, never introduce structure the map didn't show.
- Depth is the point: go one level past the interface where it earns its keep, but write it ONCE and move on -- no length cap, but aim for something a careful reader finishes in about five minutes."""

PR_DOC_PROMPT = """You are writing an in-depth explainer of a pull request for the repository's owner, who asked for the long-form version. The repository is checked out at the PR head in the current directory.

PR #{pr} of {repo}: {title}
Base branch: {base}
PR description:
{body}
{focus}
Ground yourself first: if CLAUDE.md or AGENTS.md exists at the repo root, read it; read the changed files (and their callers) as needed. You are read-only; modify nothing.

Cover: what changes and why; the component map of the change; how it works end to end, one level deeper than a summary; and where a careful reviewer should look hardest (orientation, not a verdict).

{doc_format}

THE CHANGE (unified diff{truncated}):
{diff}"""

REPO_DOC_PROMPT = """You are writing an in-depth explainer about part of a codebase for its owner, who asked for the long-form version. The repository {repo} is checked out (default branch) in the current directory.

Their question: {question}

Ground yourself first: if CLAUDE.md or AGENTS.md exists at the repo root, read it; then read the source files the question touches. You are read-only; modify nothing. Where code and docs disagree, trust the code and say so. If the question doesn't match anything in this repo, say what you looked for and where -- do not improvise.

{doc_format}"""


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True)


def _claude_explain(work_dir, prompt, label, timeout=TIMEOUT_SECONDS):
    """One read-only explanation run; returns plain prose. Same guardrails as
    reviewer._claude_review -- prompt via stdin (argv byte limit on big
    diffs), no Bash and no web tools (the checkout is untrusted content).
    Process-group registration/kill lives in claude_ops.run_claude."""
    data = run_claude(
        ["--permission-mode", "plan",
         "--disallowedTools", "Bash", "WebFetch", "WebSearch",
         "--max-turns", str(MAX_TURNS)],
        cwd=work_dir, prompt=prompt, prompt_via_stdin=True,
        timeout=timeout, label=label, role="explainer")
    if data.get("is_error"):
        raise RuntimeError(f"explain [{label}] did not finish cleanly:\n{data}")
    text = (data.get("result") or "").strip()
    if not text:
        raise RuntimeError(f"explain [{label}] returned an empty answer")
    return text


def explain_pr(repo, pr_number, question=None, doc=False):
    """Explain what a PR does and how, over its checked-out head.
    Returns (explanation, meta) -- explanation is chat prose, or a full
    HTML document when doc=True."""
    with pr_checkout(repo, pr_number) as (work, meta, diff):
        truncated = ""
        if len(diff) > DIFF_CAP:
            diff = diff[:DIFF_CAP]
            truncated = ", TRUNCATED -- read the files for the rest"
        focus = (f"The owner asked specifically: {question}\n"
                 if question else "")
        fields = dict(
            pr=pr_number, repo=repo,
            title=meta.get("title") or "(no title)",
            base=meta.get("base", {}).get("ref", "?"),
            body=(meta.get("body") or "(none)")[:2000],
            focus=focus, diff=diff, truncated=truncated)
        if doc:
            prompt = PR_DOC_PROMPT.format(doc_format=DOC_FORMAT, **fields)
            label = f"explain-pr-doc #{pr_number}"
        else:
            prompt = PR_PROMPT.format(**fields)
            label = f"explain-pr #{pr_number}"
        prompt += persona.line()
        timeout = DOC_TIMEOUT if doc else TIMEOUT_SECONDS
        return _claude_explain(work, prompt, label, timeout=timeout), meta


def explain_repo(repo, question, doc=False):
    """Explain how something works in a repo, over a shallow clone of its
    default branch. Returns chat prose, or a full HTML document when
    doc=True."""
    clone_url = f"https://github.com/{repo}.git"
    try:
        # Least privilege, like the reviewer: explaining needs read only.
        token = token_for(repo, {"contents": "read"})
        clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    except Exception as e:
        log(f"explainer: no App token for {repo} ({e}); trying public access")
    with tempfile.TemporaryDirectory() as work:
        _git(["clone", "-q", "--depth", "1", clone_url, work], cwd=".")
        # The token must not sit in .git/config while the model reads an
        # untrusted checkout; nothing is fetched after this point.
        _git(["remote", "remove", "origin"], cwd=work)
        if doc:
            prompt = REPO_DOC_PROMPT.format(repo=repo, question=question,
                                            doc_format=DOC_FORMAT)
            label = "explain-repo-doc"
        else:
            prompt = REPO_PROMPT.format(repo=repo, question=question)
            label = "explain-repo"
        prompt += persona.line()
        timeout = DOC_TIMEOUT if doc else TIMEOUT_SECONDS
        return _claude_explain(work, prompt, label, timeout=timeout)


def _extract_html(text):
    """The HTML document inside a model reply, or None. Tolerates a stray
    fence or preamble; trims anything after the closing tag."""
    t = text.strip()
    low = t.lower()
    start = low.find("<!doctype")
    if start == -1:
        start = low.find("<html")
    if start == -1:
        return None
    end = low.rfind("</html>")
    return t[start:end + len("</html>")] if end != -1 else t[start:]


def _doc_name(hint):
    """A filename for the delivered document, from the PR/question hint."""
    slug = re.sub(r"[^a-z0-9]+", "-", (hint or "").lower()).strip("-")[:60]
    return f"{slug or 'explanation'}.html"


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


def run_explainer_task(task_id):
    """Process one explainer task. Same state shape as the other workers."""
    task = get_task(task_id)
    if not task:
        return f"Task {task_id} not found."

    update_task(task_id, status="running", inc_attempts=True)
    try:
        op = json.loads(task["instruction"] or "{}")
        repo = op.get("repo")
        question = (op.get("question") or "").strip()
        doc = bool(op.get("doc"))
        pr_digits = re.search(r"\d+", str(op.get("pr") or ""))
        if not repo or not (pr_digits or question):
            raise ValueError("I need a repo and either a PR number or a "
                             "question (e.g. 'explain PR 15 on owner/name').")
        if pr_digits:
            hint = f"pr-{pr_digits.group()}-{repo}"
            answer, _meta = explain_pr(repo, int(pr_digits.group()),
                                       question or None, doc=doc)
        else:
            hint = question
            answer = explain_repo(repo, question, doc=doc)
    except Exception as e:
        err = _redact(str(e))[:1200]
        update_task(task_id, status="failed", result={"error": err})
        return _cap(usage_limit.notice(e) or f"Explaining failed: {err}")

    if doc:
        html = _extract_html(answer)
        if html:
            fname = _doc_name(hint)
            update_task(task_id, status="done",
                        result={"summary": f"doc {fname} ({len(html)} chars)"})
            return {"text": "Deep dive attached -- open it in your browser.",
                    "filename": fname, "document": html.encode("utf-8")}
        # No usable HTML came back -- degrade to whatever prose it wrote
        # rather than failing a finished run.

    update_task(task_id, status="done", result={"summary": answer[:300]})
    return _cap(answer)


def main():
    from coding_agent import load_env   # deferred: keeps import cost off the box
    load_env()
    args = [a for a in sys.argv[1:] if a != "--doc"]
    doc = "--doc" in sys.argv[1:]
    if len(args) < 2:
        sys.exit("Usage: python explainer.py [--doc] <owner/repo> "
                 "<PR number | question ...>")
    repo, rest = args[0], " ".join(args[1:]).strip()
    if rest.isdigit():
        answer, _meta = explain_pr(repo, int(rest), doc=doc)
    else:
        answer = explain_repo(repo, rest, doc=doc)
    if doc:
        html = _extract_html(answer)
        if not html:
            sys.exit("no HTML document in the reply:\n" + answer[:500])
        out = _doc_name(rest if not rest.isdigit() else f"pr-{rest}-{repo}")
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"wrote {out} ({len(html)} chars)")
    else:
        print(answer)


if __name__ == "__main__":
    main()
