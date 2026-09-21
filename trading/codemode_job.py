"""Entrypoint for the trading-codemode Cloud Run Job.

Clones this repo, creates a branch, runs the Claude Code CLI non-
interactively (--permission-mode acceptEdits, same allowlist discipline as
claus.py's run_claude_code - but *without* Bash(git *): unlike the local
version, this job's remote is pre-authenticated with a push-capable token,
so letting the model run arbitrary git commands would let it push straight
to main itself, bypassing trading-web's entire diff-review/confirm flow.
Git stays entirely under this script's control), pushes the branch, and
writes the result back into codemode/pending.json for trading-web to pick
up.

Only ever started by trading.codemode.trigger_job() (via the Cloud Run Jobs
REST API), never invoked directly - reads its instructions from the
REQUEST_ID/INSTRUCTION/BRANCH_NAME env vars the job override sets.
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from . import config
from . import storage

REQUEST_ID = os.environ.get("REQUEST_ID")
INSTRUCTION = os.environ.get("INSTRUCTION")
BRANCH_NAME = os.environ.get("BRANCH_NAME")

CLI_TIMEOUT_SECONDS = 600


def _scrub_token(text):
    token = os.environ.get("GITHUB_TOKEN")
    return text.replace(token, "***") if token and text else text


def _run(args, cwd, timeout=60):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(_scrub_token(result.stderr.strip() or result.stdout.strip()))
    return result.stdout.strip()


def _write_result(status, **fields):
    """Merges onto whatever trading-web already wrote for this request
    (created_at, instruction, etc.) rather than clobbering it - trading-web
    owns the pending blob's schema and reads it right after this finishes."""
    existing = storage.read_text("codemode/pending.json")
    base = json.loads(existing) if existing else None
    if not base or base.get("request_id") != REQUEST_ID:
        # Pending request was cleared/expired/replaced under us - don't
        # resurrect it with a now-stale result.
        print(f"Pending request no longer matches {REQUEST_ID}, dropping result.")
        return
    if "summary" in fields and fields["summary"]:
        fields["summary"] = _scrub_token(fields["summary"])
    base.update(fields)
    base["status"] = status
    storage.write_text("codemode/pending.json", json.dumps(base, indent=2))


def _authed_url():
    token = os.environ["GITHUB_TOKEN"]
    return f"https://x-access-token:{token}@github.com/{config.GITHUB_REPO}.git"


def _bare_url():
    return f"https://github.com/{config.GITHUB_REPO}.git"


def _run_codemode(repo_dir):
    """Returns a result dict for _write_result, or raises on a failure that
    should map to a generic "error" outcome (callers of _run already scrub
    the token from messages; run_codemode's own error string can't contain
    it since it never touches the authenticated URL directly)."""
    # Clone with the token, then immediately strip it from the remote so the
    # agent's working directory - which it can Read - never has a live
    # credential sitting in .git/config while it runs. Re-authenticated only
    # for the push, after the CLI has finished.
    _run(["git", "clone", "--branch", "main", "--single-branch", _authed_url(), repo_dir], cwd=None, timeout=120)
    _run(["git", "remote", "set-url", "origin", _bare_url()], cwd=repo_dir)
    _run(["git", "config", "user.name", "Klaus Code Mode"], cwd=repo_dir)
    _run(["git", "config", "user.email", "klaus-codemode@local"], cwd=repo_dir)
    base_sha = _run(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    _run(["git", "checkout", "-b", BRANCH_NAME], cwd=repo_dir)

    prompt = (
        f"{INSTRUCTION}. After making the changes, respond with ONLY a "
        "short 1-2 sentence plain-English summary of what you changed."
    )
    try:
        result = subprocess.run(
            [
                "claude", "-p", prompt,
                "--output-format", "json",
                "--model", "sonnet",
                "--permission-mode", "acceptEdits",
                # No Bash(git *) here on purpose - see module docstring.
                "--allowedTools",
                "Read,Edit,Write,Glob,Grep,"
                "Bash(pip install *),Bash(pytest*)",
                "--disallowedTools",
                "Bash(rm *),Bash(rmdir *),Bash(rd *),"
                "Bash(del *),Bash(erase *),Bash(format *)",
                "--permission-prompts", "none",
            ],
            cwd=repo_dir, capture_output=True, text=True, timeout=CLI_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return {"status": "error", "error_message": "Can't find the Claude Code CLI in this job image."}
    except subprocess.TimeoutExpired:
        return {"status": "error", "error_message": "Claude Code took too long and was stopped."}

    if result.returncode != 0:
        return {"status": "error", "error_message": f"Claude Code hit an error: {_scrub_token(result.stderr.strip())}"}

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"status": "error", "error_message": "Claude Code returned output I couldn't parse."}

    if data.get("permission_denials"):
        print(f"Claude Code permission denials: {data['permission_denials']}")

    if data.get("is_error"):
        return {"status": "error", "error_message": "Claude Code hit an error making those changes."}

    summary = (data.get("result") or "").strip()

    # Measured against base_sha (not just the working tree) so this is
    # correct whether the CLI left changes uncommitted or committed them
    # itself - either way, base_sha..HEAD is the true set of changes.
    status_output = _run(["git", "status", "--porcelain"], cwd=repo_dir)
    if status_output.strip():
        _run(["git", "add", "-A"], cwd=repo_dir)
        _run(["git", "commit", "-m", f"Code mode: {INSTRUCTION[:72]}"], cwd=repo_dir)

    diff_range = f"{base_sha}..HEAD"
    files_changed = _run(["git", "diff", "--name-only", diff_range], cwd=repo_dir).splitlines()

    if not files_changed:
        return {"status": "no_changes", "summary": summary or "Didn't end up changing anything."}

    diff_text = _run(["git", "diff", diff_range], cwd=repo_dir, timeout=60)

    # Re-authenticate only now, only for the push - the one git operation
    # this script (not the model) performs with the live token.
    _run(["git", "remote", "set-url", "origin", _authed_url()], cwd=repo_dir)
    _run(["git", "push", "origin", f"HEAD:{BRANCH_NAME}"], cwd=repo_dir, timeout=60)

    return {
        "status": "diff_ready",
        "diff": diff_text,
        "files_changed": files_changed,
        "summary": summary or "Done.",
        "diff_ready_at": datetime.now(timezone.utc).isoformat(),
    }


def main():
    if not REQUEST_ID or not INSTRUCTION or not BRANCH_NAME:
        print("Missing REQUEST_ID/INSTRUCTION/BRANCH_NAME env vars.")
        sys.exit(1)

    try:
        with tempfile.TemporaryDirectory() as repo_dir:
            outcome = _run_codemode(repo_dir)
    except Exception as e:
        # Last-resort safety net: anything unanticipated (a step above with
        # no try of its own, e.g. the clone/branch setup) still reports a
        # visible error instead of crashing the job silently and leaving
        # trading-web's pending record stuck at "running" until it times out.
        outcome = {"status": "error", "error_message": _scrub_token(str(e))}

    status = outcome.pop("status")
    _write_result(status, **outcome)


if __name__ == "__main__":
    main()
