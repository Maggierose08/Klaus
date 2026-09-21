"""Klaus "code mode" for trading-web: lets a request from the web page make
a real code change to this repo, gated behind a review-and-confirm flow
before anything touches main.

The agentic part (running the Claude Code CLI with Bash access) happens in
a separate Cloud Run Job (trading-codemode, see codemode_job.py) - kept out
of this always-on service, which also holds live Alpaca credentials. This
module only ever runs plain, fixed-argument `git` commands (clone/merge/
revert/push) and talks to Cloud Run's control-plane API to start that job;
it never executes anything the model chose.

State lives in GCS (via storage.py), mirroring journal.py's pattern:
- codemode/pending.json: the in-flight request, if any (single-flight).
- codemode/log.json: append-only history of every request's outcome.
"""
import json
import os
import secrets
import smtplib
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import google.auth
import google.auth.transport.requests as gauth_requests
import requests

from . import config
from . import storage

PENDING_BLOB = "codemode/pending.json"
LOG_BLOB = "codemode/log.json"

CODE_TTL_MINUTES = 15
MAX_CODE_ATTEMPTS = 5
UNDO_WINDOW_HOURS = 24
# If the job never reports back, stop blocking new requests. Generous on
# purpose: the job's own worst case is CLI_TIMEOUT_SECONDS (600s) for the
# claude call alone, plus clone/checkout before it and status/add/commit/
# diff/push after it, plus Cloud Run scheduling/cold-start - keep this above
# the job's --task-timeout (see trading/DEPLOY.md) so a still-healthy job is
# never declared stale before Cloud Run itself would have killed it.
JOB_STALE_MINUTES = 20
# Merge/revert are a handful of local git subprocess calls (60-120s timeouts
# each) - much faster than the job above, so a much shorter bound is enough
# to recover from a crash/restart mid-merge without falsely clearing a merge
# that's genuinely still running.
CONFIRM_STALE_MINUTES = 10
CONFIRM_PHRASE = "run it"

# File-level match, not line-level: any diff touching these files is treated
# as risk-sensitive, on purpose - simpler and no false negatives.
RISK_SENSITIVE_FILES = {
    "trading/risk_manager.py",
    "trading/executor.py",
    "trading/config.py",
}

_lock = threading.Lock()


# --- time helpers ---

def _now():
    return datetime.now(timezone.utc)


def _now_iso():
    return _now().isoformat()


# --- pending-request state (unlocked helpers; callers hold _lock) ---

def _read_pending_raw():
    text = storage.read_text(PENDING_BLOB)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _write_pending_raw(pending):
    storage.write_text(PENDING_BLOB, json.dumps(pending, indent=2) if pending else "null")


def _effective_pending():
    """Reads the pending record, auto-clearing (and returning None for) a
    stale one: a job that never reported back, a diff_ready request whose
    confirmation code expired without anyone confirming or getting locked
    out, or a merge left stuck mid-flight by a crash/restart (status
    "confirming" - see confirm_request). Keeps a forgotten or interrupted
    request from blocking new ones forever."""
    pending = _read_pending_raw()
    if not pending:
        return None

    if pending["status"] == "running":
        created = datetime.fromisoformat(pending["created_at"])
        if _now() - created > timedelta(minutes=JOB_STALE_MINUTES):
            _write_pending_raw(None)
            return None
    elif pending["status"] == "diff_ready" and pending.get("code_expires_at"):
        if _now_iso() > pending["code_expires_at"]:
            _write_pending_raw(None)
            return None
    elif pending["status"] == "confirming" and pending.get("confirming_at"):
        confirming_since = datetime.fromisoformat(pending["confirming_at"])
        if _now() - confirming_since > timedelta(minutes=CONFIRM_STALE_MINUTES):
            _write_pending_raw(None)
            return None

    return pending


# --- log (mirrors journal.py's read-all/append/write-all-under-lock) ---

def _read_log_raw():
    text = storage.read_text(LOG_BLOB)
    if text is None:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return []


def read_log():
    with _lock:
        return _read_log_raw()


def _append_log(entry):
    with _lock:
        entries = _read_log_raw()
        entries.append(entry)
        storage.write_text(LOG_BLOB, json.dumps(entries, indent=2))
    return entry


def _log_outcome(pending, status, **fields):
    entry = {
        "request_id": pending["request_id"],
        "instruction": pending["instruction"],
        "branch": pending["branch"],
        "diff": pending.get("diff"),
        "files_changed": pending.get("files_changed") or [],
        "requested_at": pending["created_at"],
        "confirmed_at": _now_iso(),
        "status": status,
        "merge_commit_sha": None,
        "prior_main_sha": None,
        "risk_notified": False,
        "error_message": None,
        "undone_at": None,
    }
    entry.update(fields)
    return _append_log(entry)


def public_view(pending):
    """Strips the confirmation code before a pending record goes to the
    client - everything else is fine to show."""
    if pending is None:
        return None
    view = dict(pending)
    view.pop("code", None)
    return view


# --- secrets/tokens ---

def _scrub_token(text):
    """Removes GITHUB_TOKEN from error text before it's logged or shown on
    the page - subprocess/git error output can otherwise echo the
    token-in-URL remote verbatim (e.g. a 'repository not found' message)."""
    token = os.environ.get("GITHUB_TOKEN")
    if token and text:
        text = text.replace(token, "***")
    return text


def _github_remote():
    token = os.environ["GITHUB_TOKEN"]
    return f"https://x-access-token:{token}@github.com/{config.GITHUB_REPO}.git"


# --- Cloud Run Jobs trigger ---

def _access_token():
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(gauth_requests.Request())
    return credentials.token


def trigger_job(request_id, instruction, branch):
    """Starts the trading-codemode Cloud Run Job execution for this request.
    Fire-and-forget from here on - the job reports its own result by writing
    back into codemode/pending.json (see codemode_job.py), and trading-web
    just polls that blob (get_status), no Cloud Run Executions API needed."""
    url = (
        f"https://{config.GCP_REGION}-run.googleapis.com/v2/projects/"
        f"{config.GCP_PROJECT}/locations/{config.GCP_REGION}/jobs/"
        f"{config.CODEMODE_JOB_NAME}:run"
    )
    body = {
        "overrides": {
            "containerOverrides": [
                {
                    "env": [
                        {"name": "REQUEST_ID", "value": request_id},
                        {"name": "INSTRUCTION", "value": instruction},
                        {"name": "BRANCH_NAME", "value": branch},
                    ]
                }
            ]
        }
    }
    resp = requests.post(
        url, json=body,
        headers={"Authorization": f"Bearer {_access_token()}"},
        timeout=30,
    )
    resp.raise_for_status()


# --- plain git plumbing (fixed arguments only - no LLM-chosen commands) ---

def _run_git(args, cwd, timeout=60):
    result = subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(_scrub_token(result.stderr.strip() or result.stdout.strip()))
    return result.stdout.strip()


def _configure_git_identity(repo_dir):
    _run_git(["config", "user.name", "Klaus Code Mode"], cwd=repo_dir)
    _run_git(["config", "user.email", "klaus-codemode@local"], cwd=repo_dir)


def merge_branch_to_main(branch):
    """Fresh clone, merges `branch` into main with --no-ff (always a merge
    commit, so undo is a single `git revert -m 1`), pushes. Returns
    (merge_commit_sha, prior_main_sha)."""
    with tempfile.TemporaryDirectory() as repo_dir:
        remote = _github_remote()
        _run_git(["clone", "--branch", "main", "--single-branch", remote, repo_dir], cwd=None, timeout=120)
        _configure_git_identity(repo_dir)
        prior_sha = _run_git(["rev-parse", "HEAD"], cwd=repo_dir)
        _run_git(["fetch", "origin", branch], cwd=repo_dir, timeout=60)
        _run_git(["merge", "--no-ff", "-m", f"Code mode: merge {branch}", "FETCH_HEAD"], cwd=repo_dir)
        merge_sha = _run_git(["rev-parse", "HEAD"], cwd=repo_dir)
        _run_git(["push", "origin", "HEAD:main"], cwd=repo_dir, timeout=60)
    return merge_sha, prior_sha


def revert_commit(merge_commit_sha):
    with tempfile.TemporaryDirectory() as repo_dir:
        remote = _github_remote()
        _run_git(["clone", "--branch", "main", "--single-branch", remote, repo_dir], cwd=None, timeout=120)
        _configure_git_identity(repo_dir)
        _run_git(["revert", "--no-edit", "-m", "1", merge_commit_sha], cwd=repo_dir, timeout=60)
        _run_git(["push", "origin", "HEAD:main"], cwd=repo_dir, timeout=60)


def diff_touches_risk_files(files_changed):
    return any(f in RISK_SENSITIVE_FILES for f in (files_changed or []))


# --- email (reuses daily_summary.py's Gmail-SMTP-app-password pattern) ---

def _send_email(subject, body):
    gmail_address = os.environ["GMAIL_ADDRESS"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = gmail_address
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(gmail_address, gmail_app_password)
        smtp.send_message(msg)


def send_confirmation_code_email(branch, instruction, files_changed, code):
    body = (
        "Klaus wants to merge a code-mode change.\n\n"
        f"Branch: {branch}\n"
        f"Instruction: {instruction}\n"
        f"Files changed: {', '.join(files_changed) or '(none)'}\n\n"
        f"Confirmation code: {code}\n\n"
        f"Expires in {CODE_TTL_MINUTES} minutes. Review the diff on the "
        'trading-web page, then enter this code and type "run it" to merge '
        "to main."
    )
    _send_email(f"Klaus code-mode confirmation - {branch}", body)


def send_risk_notification_email(branch, instruction, diff, files_changed):
    body = (
        "Klaus just merged a code-mode change touching risk-sensitive "
        "files.\n\n"
        f"Branch: {branch}\n"
        f"Instruction: {instruction}\n"
        f"Files changed: {', '.join(files_changed)}\n\n"
        f"Diff:\n{diff}"
    )
    _send_email(f"[RISK] Klaus code-mode merged - {branch}", body)


# --- public request lifecycle ---

def start_request(instruction):
    """Creates a new pending code-mode request and kicks off the Cloud Run
    Job. Raises RuntimeError if one is already in flight."""
    with _lock:
        current = _effective_pending()
        if current and current["status"] in ("running", "diff_ready", "confirming"):
            raise RuntimeError(
                "A code-mode request is already in progress - finish or let "
                "it expire before starting another."
            )

        request_id = uuid.uuid4().hex[:10]
        branch = f"codemode/{_now().strftime('%Y%m%d-%H%M%S')}-{request_id[:6]}"
        pending = {
            "request_id": request_id,
            "instruction": instruction,
            "branch": branch,
            "status": "running",
            "diff": None,
            "files_changed": None,
            "summary": None,
            "error_message": None,
            "created_at": _now_iso(),
            "diff_ready_at": None,
            "confirming_at": None,
            "code": None,
            "code_expires_at": None,
            "attempts": 0,
        }
        _write_pending_raw(pending)

    try:
        trigger_job(request_id, instruction, branch)
    except Exception as e:
        with _lock:
            pending["status"] = "error"
            pending["error_message"] = f"Couldn't start the code-mode job: {e}"
            _write_pending_raw(pending)
        raise

    return request_id, branch


def get_status(request_id):
    """Reads the pending record for request_id. The first time it observes
    status flip to diff_ready, generates and emails the 4-digit confirmation
    code (guarded by _lock + the "code is None" check so a burst of
    concurrent status polls only sends one email)."""
    send_code = False
    with _lock:
        pending = _effective_pending()
        if not pending or pending.get("request_id") != request_id:
            return None

        if pending["status"] == "diff_ready" and pending.get("code") is None:
            pending["code"] = f"{secrets.randbelow(10000):04d}"
            pending["code_expires_at"] = (_now() + timedelta(minutes=CODE_TTL_MINUTES)).isoformat()
            pending["attempts"] = 0
            _write_pending_raw(pending)
            send_code = True

    if send_code:
        try:
            send_confirmation_code_email(
                pending["branch"], pending["instruction"],
                pending.get("files_changed") or [], pending["code"],
            )
        except Exception as e:
            print(f"Failed to send code-mode confirmation email: {e}")

    return pending


def confirm_request(request_id, code, phrase):
    """Validates the code + "run it" phrase, then merges to main. Returns
    (ok, message, log_entry_or_None)."""
    with _lock:
        pending = _effective_pending()
        if not pending or pending.get("request_id") != request_id:
            return False, "That request isn't pending anymore (it may have expired).", None
        if pending["status"] != "diff_ready":
            return False, "That change isn't ready to confirm (or was already confirmed).", None
        if (phrase or "").strip().lower() != CONFIRM_PHRASE:
            return False, 'Type "run it" exactly, along with the code.', None
        if code != pending.get("code"):
            pending["attempts"] += 1
            if pending["attempts"] >= MAX_CODE_ATTEMPTS:
                _write_pending_raw(None)
                return False, "Too many wrong codes. Start over.", None
            _write_pending_raw(pending)
            remaining = MAX_CODE_ATTEMPTS - pending["attempts"]
            return False, f"Wrong code ({remaining} attempt{'s' if remaining != 1 else ''} left).", None

        # Correct - claim it so a second concurrent confirm can't double-merge.
        pending["status"] = "confirming"
        pending["confirming_at"] = _now_iso()
        _write_pending_raw(pending)

    try:
        merge_sha, prior_sha = merge_branch_to_main(pending["branch"])
    except Exception as e:
        message = _scrub_token(str(e))
        entry = _log_outcome(pending, "error", error_message=message)
        with _lock:
            _write_pending_raw(None)
        return False, f"Merge failed: {message}", entry

    risk_hit = diff_touches_risk_files(pending.get("files_changed"))
    entry = _log_outcome(
        pending, "merged",
        merge_commit_sha=merge_sha, prior_main_sha=prior_sha, risk_notified=risk_hit,
    )

    with _lock:
        _write_pending_raw(None)

    if risk_hit:
        try:
            send_risk_notification_email(
                pending["branch"], pending["instruction"],
                pending.get("diff") or "", pending.get("files_changed") or [],
            )
        except Exception as e:
            print(f"Failed to send risk notification email: {e}")

    return True, "Merged to main - the deploy is on its way.", entry


def undo_last_merge():
    """Reverts the single most recent 'merged' log entry, if it's within the
    24h undo window and hasn't already been undone. Older merges are never
    eligible, even if the most recent one is now out of window."""
    entries = read_log()

    last_merged = None
    for e in reversed(entries):
        if e["status"] == "merged":
            last_merged = e
            break

    if last_merged is None:
        return False, "No merged change to undo.", None
    if last_merged.get("undone_at"):
        return False, "That change was already undone.", None

    confirmed_at = datetime.fromisoformat(last_merged["confirmed_at"])
    if _now() - confirmed_at > timedelta(hours=UNDO_WINDOW_HOURS):
        return False, f"That merge is more than {UNDO_WINDOW_HOURS}h old, can't undo it here.", None

    try:
        revert_commit(last_merged["merge_commit_sha"])
    except Exception as e:
        return False, f"Undo failed: {_scrub_token(str(e))}", None

    with _lock:
        entries = _read_log_raw()
        for e in entries:
            if e["request_id"] == last_merged["request_id"]:
                e["undone_at"] = _now_iso()
        storage.write_text(LOG_BLOB, json.dumps(entries, indent=2))

    return True, "Reverted - the deploy is on its way.", last_merged
