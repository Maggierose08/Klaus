import json
import os

import anthropic
from flask import Flask, Response, jsonify, render_template_string, request

from . import config
from . import data_client
from . import journal

app = Flask(__name__)

_client = None

# How many of the most recent journal entries to hand to Claude as context.
# Entries are appended chronologically, so the last N are the most recent.
RECENT_ENTRIES_LIMIT = 50

CHAT_MODEL = "claude-sonnet-4-5"

SYSTEM_PROMPT = (
    "You are a trading assistant answering questions about a PAPER trading "
    "account (not real money). You're given the current Alpaca account "
    "state, open positions, and recent trading journal entries as JSON. "
    "Answer the question naturally and concisely in plain English, the way "
    "you'd explain it to the account owner. If the data doesn't cover what "
    "was asked, say so honestly instead of guessing."
)

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Trading Assistant</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    display: flex;
    justify-content: center;
    align-items: flex-start;
    padding: 24px 16px;
    background: #f4f5f7;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    body { background: #17181c; }
    .card { background: #23252b; }
    textarea { background: #1a1b1f; color: #eee; border-color: #3a3c44; }
    .answer { background: #1a1b1f; border-color: #3a3c44; color: #eee; }
    h1 { color: #eee; }
    .error { color: #ff8a8a; }
  }
  .card {
    width: 100%;
    max-width: 520px;
    background: #fff;
    border-radius: 16px;
    padding: 24px;
    box-shadow: 0 2px 12px rgba(0,0,0,0.08);
  }
  h1 {
    margin: 0 0 4px;
    font-size: 1.3rem;
  }
  p.sub {
    margin: 0 0 20px;
    color: #888;
    font-size: 0.9rem;
  }
  textarea {
    width: 100%;
    min-height: 84px;
    padding: 12px;
    font-size: 16px;
    border: 1px solid #d5d7dc;
    border-radius: 10px;
    resize: vertical;
    font-family: inherit;
  }
  button {
    margin-top: 12px;
    width: 100%;
    padding: 14px;
    font-size: 16px;
    font-weight: 600;
    border: none;
    border-radius: 10px;
    background: #2563eb;
    color: #fff;
    cursor: pointer;
  }
  button:disabled { opacity: 0.6; cursor: default; }
  .answer {
    margin-top: 20px;
    padding: 14px;
    border-radius: 10px;
    background: #f7f8fa;
    border: 1px solid #eceef1;
    white-space: pre-wrap;
    line-height: 1.45;
    display: none;
  }
  .error { color: #b91c1c; }
  .hint { margin-top: 10px; font-size: 0.8rem; color: #999; }
</style>
</head>
<body>
<div class="card">
  <h1>Trading Assistant</h1>
  <p class="sub">Ask about your paper account, positions, or recent trades.</p>
  <textarea id="question" placeholder="e.g. how's trading going today?" autofocus></textarea>
  <button id="submit">Ask</button>
  <div class="hint">Enter to submit &middot; Shift+Enter for a new line</div>
  <div id="answer" class="answer"></div>
</div>
<script>
  const questionEl = document.getElementById('question');
  const answerEl = document.getElementById('answer');
  const submitEl = document.getElementById('submit');

  async function ask() {
    const question = questionEl.value.trim();
    if (!question) return;

    submitEl.disabled = true;
    submitEl.textContent = 'Thinking...';
    answerEl.style.display = 'block';
    answerEl.classList.remove('error');
    answerEl.textContent = '...';

    try {
      const res = await fetch('/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Something went wrong.');
      answerEl.textContent = data.answer;
    } catch (err) {
      answerEl.classList.add('error');
      answerEl.textContent = err.message || 'Something went wrong.';
    } finally {
      submitEl.disabled = false;
      submitEl.textContent = 'Ask';
    }
  }

  submitEl.addEventListener('click', ask);
  questionEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      ask();
    }
  });
</script>
</body>
</html>
"""


@app.before_request
def _require_app_password():
    """Optional gate: this service is meant to be reachable from a phone
    browser with no extra setup, so it's typically deployed with
    --allow-unauthenticated. Setting APP_PASSWORD adds a basic-auth prompt so
    a stranger who finds the URL can't read your account/journal data. Unset
    (the default) means no gate, matching the original open behavior."""
    password = os.environ.get("APP_PASSWORD")
    if not password or request.path == "/healthz":
        return None
    auth = request.authorization
    if auth is None or auth.password != password:
        return Response(
            "Authentication required.", 401,
            {"WWW-Authenticate": 'Basic realm="Trading Assistant"'},
        )
    return None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def _gather_context():
    """Best-effort snapshot of account state, positions, and recent journal
    activity. Any piece that fails to load is reported as an error string
    within the payload rather than failing the whole request, since Claude
    can still answer from whatever data is available."""
    try:
        account_state = data_client.get_account_state()
    except Exception as e:
        account_state = {"error": str(e)}

    try:
        positions = data_client.get_open_positions()
    except Exception as e:
        positions = {"error": str(e)}

    try:
        entries = journal.read_all()[-RECENT_ENTRIES_LIMIT:]
    except Exception as e:
        entries = {"error": str(e)}

    return {
        "account_state": account_state,
        "open_positions": positions,
        "recent_journal_entries": entries,
    }


@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/ask", methods=["POST"])
def ask():
    body = request.get_json(silent=True) or {}
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"error": "Ask a question first."}), 400

    context = _gather_context()
    prompt = (
        f"Question: {question}\n\n"
        "Data:\n" + json.dumps(context, default=str)
    )

    try:
        response = _get_client().messages.create(
            model=CHAT_MODEL,
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as e:
        return jsonify({"error": f"Claude request failed: {e}"}), 502

    answer = "".join(b.text for b in response.content if b.type == "text")
    return jsonify({"answer": answer})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
