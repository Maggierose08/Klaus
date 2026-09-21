"""Entrypoint for the trading-codemode Cloud Run Job.

Clones this repo, creates a branch, runs the Claude Code CLI non-
interactively (same --permission-mode acceptEdits + --allowedTools/
--disallowedTools discipline as claus.py's run_claude_code, which is the
proven local version of this same pattern), pushes the branch, and writes
the result back into codemode/pending.json for trading-web to pick up.

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
    base.update(fields)
    base["status"] = status
    storage.write_text("codemode/pending.json", json.dumps(base, indent=2))


def main():
    if not REQUEST_ID or not INSTRUCTION or not BRANCH_NAME:
        print("Missing REQUEST_ID/INSTRUCTION/BRANCH_NAME env vars.")
        sys.exit(1)

    token = os.environ["GITHUB_TOKEN"]
    remote = f"https://x-access-token:{token}@github.com/{config.GITHUB_REPO}.git"

    with tempfile.TemporaryDirectory() as repo_dir:
        try:
            _run(["git", "clone", "--branch", "main", "--single-branch", remote, repo_dir], cwd=None, timeout=120)
            _run(["git", "config", "user.name", "Klaus Code Mode"], cwd=repo_dir)
            _run(["git", "config", "user.email", "klaus-codemode@local"], cwd=repo_dir)
            base_sha = _run(["git", "rev-parse", "HEAD"], cwd=repo_dir)
            _run(["git", "checkout", "-b", BRANCH_NAME], cwd=repo_dir)
        except Exception as e:
            _write_result("error", error_message=f"Couldn't set up the branch: {e}")
            return

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
                    "--allowedTools",
                    "Read,Edit,Write,Glob,Grep,"
                    "Bash(git *),Bash(pip install *),Bash(pytest*)",
                    "--disallowedTools",
                    "Bash(rm *),Bash(rmdir *),Bash(rd *),"
                    "Bash(del *),Bash(erase *),Bash(format *)",
                    "--permission-prompts", "none",
                ],
                cwd=repo_dir, capture_output=True, text=True, timeout=CLI_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            _write_result("error", error_message="Claude Code took too long and was stopped.")
            return

        if result.returncode != 0:
            _write_result("error", error_message=f"Claude Code hit an error: {_scrub_token(result.stderr.strip())}")
            return

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            _write_result("error", error_message="Claude Code returned output I couldn't parse.")
            return

        if data.get("permission_denials"):
            print(f"Claude Code permission denials: {data['permission_denials']}")

        if data.get("is_error"):
            _write_result("error", error_message="Claude Code hit an error making those changes.")
            return

        summary = (data.get("result") or "").strip()

        # Measured against base_sha (not just the working tree) so this is
        # correct whether the CLI left changes uncommitted or committed them
        # itself - either way, base_sha..HEAD is the true set of changes.
        try:
            status_output = _run(["git", "status", "--porcelain"], cwd=repo_dir)
            if status_output.strip():
                _run(["git", "add", "-A"], cwd=repo_dir)
                _run(["git", "commit", "-m", f"Code mode: {INSTRUCTION[:72]}"], cwd=repo_dir)

            diff_range = f"{base_sha}..HEAD"
            files_changed = _run(["git", "diff", "--name-only", diff_range], cwd=repo_dir).splitlines()
        except Exception as e:
            _write_result("error", error_message=str(e))
            return

        if not files_changed:
            _write_result("no_changes", summary=summary or "Didn't end up changing anything.")
            return

        try:
            diff_text = _run(["git", "diff", diff_range], cwd=repo_dir, timeout=60)
            _run(["git", "push", "origin", f"HEAD:{BRANCH_NAME}"], cwd=repo_dir, timeout=60)
        except Exception as e:
            _write_result("error", error_message=f"Couldn't push the branch: {e}")
            return

    _write_result(
        "diff_ready",
        diff=diff_text,
        files_changed=files_changed,
        summary=summary or "Done.",
        diff_ready_at=datetime.now(timezone.utc).isoformat(),
    )


if __name__ == "__main__":
    main()
