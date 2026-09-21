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

# How many recent trades the web page's "Recent trades" list shows.
RECENT_TRADES_LIMIT = 8

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
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
<title>Trading Assistant</title>
<style>
  :root {
    color-scheme: light dark;
    --surface:      #fcfcfb;
    --page-plane:   #f9f9f7;
    --ink:          #0b0b0b;
    --ink-2:        #52514e;
    --ink-muted:    #898781;
    --hairline:     #e1e0d9;
    --border:       rgba(11,11,11,0.10);
    --good:         #0ca30c;
    --good-text:    #006300;
    --bad:          #d03b3b;
    --bad-text:     #d03b3b;
    --buy:          #2a78d6;
    --sell:         #eb6834;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --surface:      #1a1a19;
      --page-plane:   #0d0d0d;
      --ink:          #ffffff;
      --ink-2:        #c3c2b7;
      --ink-muted:    #898781;
      --hairline:     #2c2c2a;
      --border:       rgba(255,255,255,0.10);
      --good:         #0ca30c;
      --good-text:    #0ca30c;
      --bad:          #d03b3b;
      --bad-text:     #e66767;
      --buy:          #3987e5;
      --sell:         #d95926;
    }
  }
  * { box-sizing: border-box; }
  html, body { overflow-x: hidden; }
  body {
    margin: 0;
    min-height: 100vh;
    display: flex;
    justify-content: center;
    background: var(--page-plane);
    color: var(--ink);
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .page {
    width: 100%;
    max-width: 560px;
    min-width: 0;
    padding: max(20px, env(safe-area-inset-top)) 16px max(28px, env(safe-area-inset-bottom));
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .card { min-width: 0; }
  header.pagehead { padding: 4px 4px 0; }
  h1 {
    margin: 0 0 2px;
    font-size: 1.375rem;
    font-weight: 700;
    letter-spacing: -0.01em;
  }
  p.sub {
    margin: 0;
    color: var(--ink-2);
    font-size: 0.9rem;
  }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 18px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
  }
  h2 {
    margin: 0 0 12px;
    font-size: 0.95rem;
    font-weight: 700;
    color: var(--ink);
  }
  h2 .count { color: var(--ink-muted); font-weight: 500; }

  /* KPI stat tiles */
  .kpis {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 10px;
  }
  .kpi {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 12px 10px;
    min-width: 0;
  }
  .kpi-label {
    font-size: 0.72rem;
    color: var(--ink-muted);
    margin-bottom: 4px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .kpi-value {
    font-size: 1.15rem;
    font-weight: 700;
    line-height: 1.15;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .kpi-value.up { color: var(--good-text); }
  .kpi-value.down { color: var(--bad-text); }

  /* Positions bar list */
  .barlist { display: flex; flex-direction: column; gap: 12px; }
  .posrow { min-width: 0; }
  .posrow-top {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    font-size: 0.88rem;
    margin-bottom: 5px;
  }
  .possym { font-weight: 600; }
  .posmeta { color: var(--ink-muted); font-size: 0.78rem; margin-left: 6px; }
  .posval { font-weight: 600; font-variant-numeric: tabular-nums; }
  .posval.up { color: var(--good-text); }
  .posval.down { color: var(--bad-text); }
  .posbar-track {
    height: 8px;
    border-radius: 4px;
    background: var(--hairline);
    overflow: hidden;
  }
  .posbar-fill {
    height: 100%;
    border-radius: 4px;
    min-width: 3px;
  }
  .posbar-fill.up { background: var(--good); }
  .posbar-fill.down { background: var(--bad); }

  /* Recent trades list */
  .tradelist { display: flex; flex-direction: column; }
  .traderow {
    min-width: 0;
    padding: 10px 0;
    border-top: 1px solid var(--hairline);
  }
  .traderow:first-child { border-top: none; padding-top: 0; }
  .traderow-top {
    display: flex;
    align-items: center;
    gap: 7px;
    font-size: 0.88rem;
  }
  .tradedot {
    width: 8px; height: 8px; border-radius: 50%;
    flex: none;
    background: var(--ink-muted);
  }
  .tradedot.buy { background: var(--buy); }
  .tradedot.sell { background: var(--sell); }
  .tradesym { font-weight: 600; }
  .tradeaction { color: var(--ink-2); }
  .tradetime {
    margin-left: auto;
    color: var(--ink-muted);
    font-size: 0.78rem;
    white-space: nowrap;
  }
  .traderow-bottom {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-top: 4px;
    font-size: 0.82rem;
    padding-left: 15px;
  }
  .tradenotional { color: var(--ink-2); font-variant-numeric: tabular-nums; }
  .tradestatus { display: inline-flex; align-items: center; gap: 4px; }
  .tradestatus.good { color: var(--good-text); }
  .tradestatus.bad { color: var(--bad-text); }
  .tradestatus.muted { color: var(--ink-muted); }

  .empty, .loading {
    color: var(--ink-muted);
    font-size: 0.88rem;
    padding: 4px 0;
  }

  /* Ask card */
  textarea {
    width: 100%;
    min-height: 84px;
    padding: 12px;
    font-size: 16px;
    border: 1px solid var(--hairline);
    border-radius: 10px;
    resize: vertical;
    font-family: inherit;
    background: var(--surface);
    color: var(--ink);
  }
  button {
    margin-top: 12px;
    width: 100%;
    padding: 14px;
    font-size: 16px;
    font-weight: 600;
    border: none;
    border-radius: 10px;
    background: var(--buy);
    color: #fff;
    cursor: pointer;
  }
  button:disabled { opacity: 0.6; cursor: default; }
  .answer {
    margin-top: 16px;
    padding: 14px;
    border-radius: 10px;
    background: var(--page-plane);
    border: 1px solid var(--hairline);
    white-space: pre-wrap;
    line-height: 1.5;
    font-size: 0.94rem;
    display: none;
  }
  .answer.error { color: var(--bad-text); }
  .hint { margin-top: 10px; font-size: 0.78rem; color: var(--ink-muted); }
</style>
</head>
<body>
<div class="page">
  <header class="pagehead">
    <h1>Trading Assistant</h1>
    <p class="sub">Paper account &middot; live overview &amp; assistant</p>
  </header>

  <div class="kpis" id="kpis">
    <div class="kpi"><div class="kpi-label">Portfolio</div><div class="kpi-value">&hellip;</div></div>
    <div class="kpi"><div class="kpi-label">Unrealized P&amp;L</div><div class="kpi-value">&hellip;</div></div>
    <div class="kpi"><div class="kpi-label">Cash</div><div class="kpi-value">&hellip;</div></div>
  </div>

  <div class="card">
    <h2>Open positions <span class="count" id="posCount"></span></h2>
    <div id="positionsList" class="barlist"><div class="loading">Loading&hellip;</div></div>
  </div>

  <div class="card">
    <h2>Recent trades</h2>
    <div id="tradesList" class="tradelist"><div class="loading">Loading&hellip;</div></div>
  </div>

  <div class="card">
    <h2>Ask</h2>
    <textarea id="question" placeholder="e.g. how's trading going today?"></textarea>
    <button id="submit">Ask</button>
    <div class="hint">Enter to submit &middot; Shift+Enter for a new line</div>
    <div id="answer" class="answer"></div>
  </div>
</div>
<script>
  function fmtMoney(n) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    const sign = n < 0 ? '-' : '';
    return sign + '$' + Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function fmtSigned(n) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    return (n >= 0 ? '+' : '-') + '$' + Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function fmtKpi(n, signed) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    const abs = Math.abs(n);
    let body;
    if (abs >= 1000000) body = (abs / 1000000).toFixed(1).replace(/\\.0$/, '') + 'M';
    else if (abs >= 10000) body = (abs / 1000).toFixed(1).replace(/\\.0$/, '') + 'K';
    else body = Math.round(abs).toLocaleString();
    const sign = signed ? (n >= 0 ? '+' : '-') : (n < 0 ? '-' : '');
    return sign + '$' + body;
  }
  function fmtTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ', ' +
      d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  }

  function renderKpis(account) {
    const kpisEl = document.getElementById('kpis');
    const tiles = kpisEl.querySelectorAll('.kpi-value');
    if (!account || account.error) {
      tiles.forEach(t => t.textContent = '—');
      return;
    }
    tiles[0].textContent = fmtKpi(account.portfolio_value, false);
    const pl = account.unrealized_pl;
    tiles[1].textContent = fmtKpi(pl, true);
    tiles[1].classList.toggle('up', pl >= 0);
    tiles[1].classList.toggle('down', pl < 0);
    tiles[2].textContent = fmtKpi(account.cash, false);
  }

  function renderPositions(positions) {
    const el = document.getElementById('positionsList');
    const countEl = document.getElementById('posCount');
    if (!positions || positions.error) {
      el.innerHTML = '<div class="empty">Could not load positions.</div>';
      countEl.textContent = '';
      return;
    }
    countEl.textContent = positions.length ? '(' + positions.length + ')' : '';
    if (!positions.length) {
      el.innerHTML = '<div class="empty">No open positions right now.</div>';
      return;
    }
    const maxAbs = Math.max(...positions.map(p => Math.abs(p.unrealized_pl || 0)), 1);
    el.innerHTML = positions.map(p => {
      const up = (p.unrealized_pl || 0) >= 0;
      const pct = Math.max(4, Math.round(Math.abs(p.unrealized_pl || 0) / maxAbs * 100));
      const dir = up ? 'up' : 'down';
      return (
        '<div class="posrow" title="' + p.symbol + ': ' + fmtSigned(p.unrealized_pl) + ' unrealized (market value ' + fmtMoney(p.market_value) + ')">' +
          '<div class="posrow-top">' +
            '<span><span class="possym">' + p.symbol + '</span><span class="posmeta">' + fmtMoney(p.market_value) + '</span></span>' +
            '<span class="posval ' + dir + '">' + fmtSigned(p.unrealized_pl) + '</span>' +
          '</div>' +
          '<div class="posbar-track"><div class="posbar-fill ' + dir + '" style="width:' + pct + '%"></div></div>' +
        '</div>'
      );
    }).join('');
  }

  function renderTrades(trades) {
    const el = document.getElementById('tradesList');
    if (!trades || trades.error) {
      el.innerHTML = '<div class="empty">Could not load recent trades.</div>';
      return;
    }
    if (!trades.length) {
      el.innerHTML = '<div class="empty">No trades recorded yet.</div>';
      return;
    }
    el.innerHTML = trades.map(t => {
      const action = t.action || 'hold';
      const dotClass = action === 'buy' ? 'buy' : (action === 'sell' ? 'sell' : '');
      const actionLabel = action.charAt(0).toUpperCase() + action.slice(1);
      let statusClass = 'muted', statusLabel = '— Held';
      if (t.error) { statusClass = 'bad'; statusLabel = '✕ Error'; }
      else if (action !== 'hold' && t.executed) { statusClass = 'good'; statusLabel = '✓ Executed'; }
      else if (action !== 'hold' && t.approved === false) { statusClass = 'bad'; statusLabel = '✕ Rejected'; }
      else if (action !== 'hold') { statusClass = 'muted'; statusLabel = '— Not executed'; }
      const notional = t.notional_usd ? '<span class="tradenotional">' + fmtMoney(t.notional_usd) + '</span>' : '';
      return (
        '<div class="traderow" title="' + (t.reason || '') + '">' +
          '<div class="traderow-top">' +
            '<span class="tradedot ' + dotClass + '"></span>' +
            '<span class="tradesym">' + t.symbol + '</span>' +
            '<span class="tradeaction">' + actionLabel + '</span>' +
            '<span class="tradetime">' + fmtTime(t.timestamp) + '</span>' +
          '</div>' +
          '<div class="traderow-bottom">' + notional +
            '<span class="tradestatus ' + statusClass + '">' + statusLabel + '</span>' +
          '</div>' +
        '</div>'
      );
    }).join('');
  }

  async function loadSnapshot() {
    try {
      const res = await fetch('/snapshot');
      const data = await res.json();
      renderKpis(data.account);
      renderPositions(data.positions);
      renderTrades(data.recent_trades);
    } catch (err) {
      renderKpis(null);
      renderPositions(null);
      renderTrades(null);
    }
  }
  loadSnapshot();

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


def _dashboard_snapshot():
    """Shaped, UI-ready data for the page's KPI tiles, positions bars, and
    recent-trades list. Kept separate from _gather_context (which hands the
    /ask assistant raw, unshaped data) since the two have different shapes
    and failure handling."""
    try:
        account = data_client.get_account_state()
        positions = data_client.get_open_positions()
        account["unrealized_pl"] = sum(p["unrealized_pl"] for p in positions)
    except Exception as e:
        account = {"error": str(e)}
        positions = {"error": str(e)}

    try:
        entries = journal.read_all()[-RECENT_TRADES_LIMIT:][::-1]
        trades = []
        for e in entries:
            signal = e.get("signal") or {}
            execution = e.get("execution") or {}
            risk_decision = e.get("risk_decision") or {}
            trades.append({
                "timestamp": e.get("timestamp"),
                "symbol": e.get("symbol"),
                "action": signal.get("action") or ("error" if e.get("error") else "hold"),
                "notional_usd": signal.get("notional_usd"),
                "approved": risk_decision.get("approved"),
                "reason": risk_decision.get("reason"),
                "executed": execution.get("executed"),
                "error": e.get("error") or execution.get("error"),
            })
    except Exception as e:
        trades = {"error": str(e)}

    return {"account": account, "positions": positions, "recent_trades": trades}


@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/snapshot")
def snapshot():
    return jsonify(_dashboard_snapshot())


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
