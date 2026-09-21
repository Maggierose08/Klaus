import ipaddress
import json
import os
import re
import socket
from urllib.parse import urljoin, urlparse

import anthropic
import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, render_template_string, request

from . import codemode
from . import config
from . import data_client
from . import journal

app = Flask(__name__)
# Bounds request size (mainly for /chat image uploads) so a huge upload
# doesn't tie up the container; 413s are turned into JSON below.
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024

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

# Klaus's general-purpose personality, adapted from claus.py's SYSTEM_PROMPT
# for a text/web context: no "keep it short, this gets read aloud" limit.
# No code-mode trigger baked into the chat personality either - code mode
# lives in its own explicit card below (see codemode.py), not something
# /chat offers to slide into mid-conversation the way claus.py's voice
# version does.
KLAUS_SYSTEM_PROMPT = (
    "You are Klaus - same guy as always, just typing instead of talking "
    "right now. Blunt, deadpan, sarcastic - you don't sugarcoat anything "
    "and you're not impressed easily. Crude and a little rude is fine, "
    "keep it funny not mean, no slurs or actually hateful stuff. Drop a "
    "joke or a smartass remark when it fits, but still actually answer the "
    "question - don't let the bit get in the way of being useful. Use web "
    "search when you need current info you're not sure about. You can see "
    "images the user attaches and read the text of any web page pulled in "
    "below their message when relevant - just use it directly, don't "
    "narrate that you 'received' it or describe the mechanism."
)

# Content-block "image" media types the Anthropic API accepts.
ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}

# Caps the /chat message list server-side too (defense in depth - the
# frontend already trims its own history to roughly this size).
KLAUS_MAX_HISTORY_MESSAGES = 40

_URL_PATTERN = re.compile(r'https?://[^\s<>"\')]+')

URL_FETCH_TIMEOUT = 8
URL_FETCH_MAX_BYTES = 3 * 1024 * 1024
URL_TEXT_LIMIT = 6000


def _extract_first_url(text):
    if not text:
        return None
    match = _URL_PATTERN.search(text)
    return match.group(0).rstrip(".,;:!?") if match else None


def _is_public_http_url(url):
    """Rejects loopback/private/link-local targets - notably GCP's metadata
    server at 169.254.169.254, which would hand over the container's
    service-account credentials to anything that can make it fetch that
    URL. Every redirect hop gets re-checked by _fetch_url_text, not just
    the URL the user typed."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        for info in socket.getaddrinfo(parsed.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except (socket.gaierror, ValueError, UnicodeError):
        return False


def _fetch_url_text(url):
    """Fetches a page and returns (final_url, title, text). Follows
    redirects manually (capped) so each hop is validated by
    _is_public_http_url too - requests' built-in redirect following would
    skip that check and could be steered at an internal address."""
    for _ in range(5):
        if not _is_public_http_url(url):
            raise ValueError("that link points somewhere I'm not allowed to fetch")
        resp = requests.get(
            url, timeout=URL_FETCH_TIMEOUT, allow_redirects=False, stream=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; KlausBot/1.0)"},
        )
        if resp.is_redirect:
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                raise ValueError("that link redirected with no destination")
            url = urljoin(url, location)
            continue
        break
    else:
        raise ValueError("too many redirects")

    content_type = resp.headers.get("Content-Type", "")
    if "text/html" not in content_type and "text/plain" not in content_type:
        resp.close()
        raise ValueError(f"that's a {content_type or 'binary'} link, not something I can read")

    raw = resp.raw.read(URL_FETCH_MAX_BYTES + 1, decode_content=True)
    resp.close()
    raw = raw[:URL_FETCH_MAX_BYTES]

    if "text/plain" in content_type:
        text = raw.decode(resp.encoding or "utf-8", errors="replace")
        title = None
    else:
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        title = soup.title.string.strip() if soup.title and soup.title.string else None
        text = soup.get_text(separator=" ", strip=True)

    return url, title, text[:URL_TEXT_LIMIT]


def _augment_messages_with_url(messages):
    """If the latest user message contains a URL, fetches it and returns a
    copy of `messages` with the page text appended to that turn - used only
    for this one Claude call. The fetched text is never handed back to the
    client to store, so a long page doesn't get re-sent (and re-billed) on
    every follow-up turn; only Klaus's resulting answer persists in the
    conversation from there. Returns (messages_for_claude, fetched_info),
    where fetched_info is None if no URL was found."""
    if not messages or messages[-1].get("role") != "user":
        return messages, None

    content = messages[-1].get("content")
    if isinstance(content, str):
        text_parts = [content]
    elif isinstance(content, list):
        text_parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    else:
        text_parts = []

    url = _extract_first_url(" ".join(text_parts))
    if not url:
        return messages, None

    try:
        final_url, title, text = _fetch_url_text(url)
    except Exception as e:
        note = f"\n\n[Tried to fetch {url} but couldn't: {e}]"
        fetched_info = None
    else:
        titled = f" ({title})" if title else ""
        note = f"\n\n[Fetched content from {final_url}{titled}:\n{text}]"
        fetched_info = {"url": final_url, "title": title}

    if isinstance(content, str):
        new_content = content + note
    else:
        new_content = list(content) + [{"type": "text", "text": note.strip()}]

    augmented = list(messages[:-1]) + [{"role": "user", "content": new_content}]
    return augmented, fetched_info


@app.errorhandler(413)
def _request_too_large(e):
    return jsonify({"error": "That's too big to send (max ~12MB)."}), 413

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

  /* Klaus chat card */
  .chatlog {
    display: flex;
    flex-direction: column;
    gap: 10px;
    max-height: 420px;
    overflow-y: auto;
    margin-bottom: 12px;
    min-width: 0;
  }
  .bubble {
    max-width: 85%;
    padding: 10px 13px;
    border-radius: 14px;
    font-size: 0.92rem;
    line-height: 1.45;
    white-space: pre-wrap;
    word-wrap: break-word;
  }
  .bubble.user {
    align-self: flex-end;
    background: var(--buy);
    color: #fff;
    border-bottom-right-radius: 4px;
  }
  .bubble.assistant {
    align-self: flex-start;
    background: var(--page-plane);
    border: 1px solid var(--hairline);
    color: var(--ink);
    border-bottom-left-radius: 4px;
  }
  .bubble.thinking { color: var(--ink-muted); font-style: italic; }
  .bubble.error { color: var(--bad-text); }
  .bubble img.attach {
    max-width: 100%;
    border-radius: 8px;
    margin-top: 6px;
    display: block;
  }
  .bubble .fetched-chip {
    display: block;
    margin-top: 6px;
    font-size: 0.75rem;
    opacity: 0.8;
  }
  .chat-composer { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
  .image-preview-wrap {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 8px;
    border: 1px solid var(--hairline);
    border-radius: 10px;
  }
  .image-preview-wrap img {
    width: 40px;
    height: 40px;
    object-fit: cover;
    border-radius: 6px;
  }
  .image-preview-wrap span { font-size: 0.8rem; color: var(--ink-muted); }
  .image-preview-wrap button {
    margin-left: auto;
    width: auto;
    padding: 4px 8px;
    background: none;
    border: none;
    color: var(--bad-text);
    font-size: 1rem;
    cursor: pointer;
  }
  .chat-input-row { display: flex; gap: 8px; align-items: flex-end; min-width: 0; }
  .chat-input-row textarea { flex: 1; min-height: 44px; }
  .chat-input-row button {
    width: auto;
    margin-top: 0;
    flex: none;
    cursor: pointer;
  }
  .attach-btn {
    width: 44px !important;
    height: 44px;
    border-radius: 10px;
    border: 1px solid var(--hairline);
    background: var(--surface);
    color: var(--ink);
    font-size: 1.15rem;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0;
  }
  .send-btn { padding: 0 20px; height: 44px; }

  /* Code mode card */
  .diffbox {
    max-height: 320px;
    overflow: auto;
    background: var(--page-plane);
    border: 1px solid var(--hairline);
    border-radius: 10px;
    padding: 12px;
    font-family: ui-monospace, "SF Mono", Consolas, monospace;
    font-size: 0.78rem;
    white-space: pre-wrap;
    word-break: break-word;
    margin-top: 12px;
  }
  .confirm-row { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
  .confirm-row input {
    flex: 1;
    min-width: 100px;
    padding: 10px;
    font-size: 16px;
    border: 1px solid var(--hairline);
    border-radius: 10px;
    background: var(--surface);
    color: var(--ink);
  }
  .confirm-row button { width: auto; margin-top: 0; padding: 10px 16px; }
  .codelog { margin-top: 16px; display: flex; flex-direction: column; gap: 10px; }
  .codelog-entry {
    border-top: 1px solid var(--hairline);
    padding-top: 10px;
    font-size: 0.85rem;
  }
  .codelog-entry:first-child { border-top: none; padding-top: 0; }
  .codelog-meta {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    color: var(--ink-muted);
    font-size: 0.78rem;
    margin-bottom: 4px;
  }
  .codelog-status { font-weight: 600; text-transform: capitalize; }
  .codelog-status.merged { color: var(--good-text); }
  .codelog-status.error, .codelog-status.rejected { color: var(--bad-text); }
  .undo-btn {
    width: auto;
    margin-top: 6px;
    padding: 6px 12px;
    font-size: 0.8rem;
    font-weight: 600;
  }
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
    <h2>Ask about trading</h2>
    <textarea id="question" placeholder="e.g. how's trading going today?"></textarea>
    <button id="submit">Ask</button>
    <div class="hint">Enter to submit &middot; Shift+Enter for a new line</div>
    <div id="answer" class="answer"></div>
  </div>

  <div class="card">
    <h2>Chat with Klaus</h2>
    <div class="chatlog" id="chatlog"></div>
    <div class="chat-composer">
      <div class="image-preview-wrap" id="imagePreviewWrap" style="display:none">
        <img id="imagePreview" alt="attached image">
        <span>Image attached</span>
        <button id="removeImageBtn" type="button" title="Remove image">&times;</button>
      </div>
      <div class="chat-input-row">
        <input type="file" id="imageInput" accept="image/*" hidden>
        <button class="attach-btn" id="attachBtn" type="button" title="Attach a screenshot">&#128247;</button>
        <textarea id="chatText" placeholder="Message Klaus..."></textarea>
        <button class="send-btn" id="chatSend" type="button">Send</button>
      </div>
      <div class="hint">Enter to send &middot; Shift+Enter for a new line &middot; images &amp; links work too</div>
    </div>
  </div>

  <div class="card">
    <h2>Code mode</h2>
    <textarea id="codeInstruction" placeholder="e.g. add a docstring to executor.py explaining the retry logic"></textarea>
    <button id="codeSubmit">Make the change</button>
    <div class="hint">Klaus edits on a branch and shows you the diff here first. Merging to main needs a code emailed to you, plus typing "run it".</div>
    <div id="codeStatus" class="answer"></div>
    <div id="codeDiffWrap" style="display:none">
      <pre id="codeDiff" class="diffbox"></pre>
      <div class="confirm-row">
        <input id="codeCode" placeholder="4-digit code" inputmode="numeric" maxlength="4">
        <input id="codePhrase" placeholder='type &quot;run it&quot;'>
        <button id="codeConfirmBtn">Confirm &amp; merge</button>
      </div>
    </div>
    <div id="codeLog" class="codelog"><div class="loading">Loading&hellip;</div></div>
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

  // --- Chat with Klaus: general Q&A, image upload, link reading ---
  const chatHistory = [];  // Anthropic-format messages, kept client-side only
  let pendingImage = null; // { mediaType, base64, dataUrl }
  const MAX_CHAT_HISTORY = 40;
  const MAX_IMAGE_DIMENSION = 1600;

  const chatlogEl = document.getElementById('chatlog');
  const chatTextEl = document.getElementById('chatText');
  const chatSendEl = document.getElementById('chatSend');
  const attachBtnEl = document.getElementById('attachBtn');
  const imageInputEl = document.getElementById('imageInput');
  const imagePreviewWrapEl = document.getElementById('imagePreviewWrap');
  const imagePreviewEl = document.getElementById('imagePreview');
  const removeImageBtnEl = document.getElementById('removeImageBtn');

  function appendBubble(roleClasses, text, imageDataUrl) {
    const div = document.createElement('div');
    div.className = 'bubble ' + roleClasses;
    div.textContent = text;
    if (imageDataUrl) {
      const img = document.createElement('img');
      img.className = 'attach';
      img.src = imageDataUrl;
      div.appendChild(img);
    }
    chatlogEl.appendChild(div);
    chatlogEl.scrollTop = chatlogEl.scrollHeight;
    return div;
  }

  function resizeImageIfNeeded(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(new Error('Could not read that file.'));
      reader.onload = () => {
        const dataUrl = reader.result;
        if (file.size <= 2 * 1024 * 1024) {
          resolve({ mediaType: file.type, base64: dataUrl.split(',')[1], dataUrl });
          return;
        }
        const img = new Image();
        img.onerror = () => reject(new Error('Could not read that image.'));
        img.onload = () => {
          let width = img.width, height = img.height;
          const scale = Math.min(1, MAX_IMAGE_DIMENSION / Math.max(width, height));
          width = Math.round(width * scale);
          height = Math.round(height * scale);
          const canvas = document.createElement('canvas');
          canvas.width = width;
          canvas.height = height;
          canvas.getContext('2d').drawImage(img, 0, 0, width, height);
          const outUrl = canvas.toDataURL('image/jpeg', 0.85);
          resolve({ mediaType: 'image/jpeg', base64: outUrl.split(',')[1], dataUrl: outUrl });
        };
        img.src = dataUrl;
      };
      reader.readAsDataURL(file);
    });
  }

  attachBtnEl.addEventListener('click', () => imageInputEl.click());
  imageInputEl.addEventListener('change', async () => {
    const file = imageInputEl.files[0];
    imageInputEl.value = '';
    if (!file) return;
    try {
      pendingImage = await resizeImageIfNeeded(file);
      imagePreviewEl.src = pendingImage.dataUrl;
      imagePreviewWrapEl.style.display = 'flex';
    } catch (err) {
      pendingImage = null;
      alert(err.message || 'Could not attach that image.');
    }
  });
  removeImageBtnEl.addEventListener('click', () => {
    pendingImage = null;
    imagePreviewWrapEl.style.display = 'none';
  });

  async function sendChat() {
    const text = chatTextEl.value.trim();
    if (!text && !pendingImage) return;

    const contentBlocks = [];
    if (pendingImage) {
      contentBlocks.push({
        type: 'image',
        source: { type: 'base64', media_type: pendingImage.mediaType, data: pendingImage.base64 },
      });
    }
    contentBlocks.push({ type: 'text', text: text || '(see attached image)' });

    appendBubble('user', text || '(image attached)', pendingImage ? pendingImage.dataUrl : null);
    chatHistory.push({ role: 'user', content: contentBlocks });
    if (chatHistory.length > MAX_CHAT_HISTORY) {
      chatHistory.splice(0, chatHistory.length - MAX_CHAT_HISTORY);
    }

    chatTextEl.value = '';
    pendingImage = null;
    imagePreviewWrapEl.style.display = 'none';
    chatSendEl.disabled = true;
    const thinkingBubble = appendBubble('assistant thinking', 'Thinking...');

    try {
      const res = await fetch('/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: chatHistory }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Something went wrong.');

      thinkingBubble.classList.remove('thinking');
      thinkingBubble.textContent = data.answer;
      if (data.fetched) {
        const chip = document.createElement('span');
        chip.className = 'fetched-chip';
        chip.textContent = '🔗 read ' + (data.fetched.title || data.fetched.url);
        thinkingBubble.appendChild(chip);
      }
      chatHistory.push({ role: 'assistant', content: [{ type: 'text', text: data.answer }] });
    } catch (err) {
      thinkingBubble.classList.remove('thinking');
      thinkingBubble.classList.add('error');
      thinkingBubble.textContent = err.message || 'Something went wrong.';
      chatHistory.pop(); // drop the failed user turn so a retry doesn't duplicate it
    } finally {
      chatSendEl.disabled = false;
    }
  }

  chatSendEl.addEventListener('click', sendChat);
  chatTextEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendChat();
    }
  });

  // --- Code mode ---
  const codeInstructionEl = document.getElementById('codeInstruction');
  const codeSubmitEl = document.getElementById('codeSubmit');
  const codeStatusEl = document.getElementById('codeStatus');
  const codeDiffWrapEl = document.getElementById('codeDiffWrap');
  const codeDiffEl = document.getElementById('codeDiff');
  const codeCodeEl = document.getElementById('codeCode');
  const codePhraseEl = document.getElementById('codePhrase');
  const codeConfirmBtnEl = document.getElementById('codeConfirmBtn');
  const codeLogEl = document.getElementById('codeLog');
  let codePollTimer = null;
  let currentRequestId = null;

  function showCodeStatus(text, isError) {
    codeStatusEl.style.display = 'block';
    codeStatusEl.classList.toggle('error', !!isError);
    codeStatusEl.textContent = text;
  }

  async function startCodeMode() {
    const instruction = codeInstructionEl.value.trim();
    if (!instruction) return;
    codeSubmitEl.disabled = true;
    codeDiffWrapEl.style.display = 'none';
    showCodeStatus('Working on it...', false);
    try {
      const res = await fetch('/codemode/request', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ instruction }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Something went wrong.');
      currentRequestId = data.request_id;
      pollCodeStatus();
    } catch (err) {
      showCodeStatus(err.message || 'Something went wrong.', true);
      codeSubmitEl.disabled = false;
    }
  }

  function pollCodeStatus() {
    if (codePollTimer) clearInterval(codePollTimer);
    codePollTimer = setInterval(async () => {
      let data, ok;
      try {
        const res = await fetch('/codemode/status/' + currentRequestId);
        data = await res.json();
        ok = res.ok;
      } catch (err) {
        return; // transient network hiccup - keep polling
      }
      if (!ok) {
        clearInterval(codePollTimer);
        showCodeStatus(data.error || 'That request expired.', true);
        codeSubmitEl.disabled = false;
        return;
      }
      if (data.status === 'running' || data.status === 'confirming') {
        showCodeStatus('Working on it...', false);
      } else if (data.status === 'diff_ready') {
        clearInterval(codePollTimer);
        codeSubmitEl.disabled = false;
        showCodeStatus(data.summary || 'Change ready for review. Check your email for the code.', false);
        codeDiffEl.textContent = data.diff || '(no diff)';
        codeDiffWrapEl.style.display = 'block';
      } else if (data.status === 'no_changes') {
        clearInterval(codePollTimer);
        codeSubmitEl.disabled = false;
        showCodeStatus(data.summary || "Didn't end up changing anything.", false);
      } else if (data.status === 'error') {
        clearInterval(codePollTimer);
        codeSubmitEl.disabled = false;
        showCodeStatus(data.error_message || 'Something went wrong.', true);
      }
    }, 3000);
  }

  async function confirmCodeMode() {
    const code = codeCodeEl.value.trim();
    const phrase = codePhraseEl.value.trim();
    if (!code || !phrase) return;
    codeConfirmBtnEl.disabled = true;
    try {
      const res = await fetch('/codemode/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ request_id: currentRequestId, code, phrase }),
      });
      const data = await res.json();
      showCodeStatus(data.message || (data.ok ? 'Done.' : 'Something went wrong.'), !data.ok);
      if (data.ok) {
        codeDiffWrapEl.style.display = 'none';
        codeInstructionEl.value = '';
        codeCodeEl.value = '';
        codePhraseEl.value = '';
        loadCodeLog();
      }
    } catch (err) {
      showCodeStatus(err.message || 'Something went wrong.', true);
    } finally {
      codeConfirmBtnEl.disabled = false;
    }
  }

  async function undoLastMerge(btn) {
    btn.disabled = true;
    btn.textContent = 'Undoing...';
    try {
      const res = await fetch('/codemode/undo', { method: 'POST' });
      const data = await res.json();
      alert(data.message || (data.ok ? 'Reverted.' : 'Could not undo.'));
      loadCodeLog();
    } catch (err) {
      alert('Could not undo.');
      btn.disabled = false;
      btn.textContent = 'Undo';
    }
  }

  // Builds each log row via textContent (not innerHTML string concatenation)
  // since e.instruction and e.files_changed are attacker-controllable -
  // anyone who can call /codemode/request chooses that text verbatim.
  function buildCodeLogEntry(e, canUndo) {
    const entry = document.createElement('div');
    entry.className = 'codelog-entry';

    const meta = document.createElement('div');
    meta.className = 'codelog-meta';
    const timeSpan = document.createElement('span');
    timeSpan.textContent = fmtTime(e.confirmed_at || e.requested_at);
    const statusSpan = document.createElement('span');
    statusSpan.className = 'codelog-status ' + e.status;
    statusSpan.textContent = e.status + (e.undone_at ? ' (undone)' : '');
    meta.appendChild(timeSpan);
    meta.appendChild(statusSpan);
    entry.appendChild(meta);

    const instructionDiv = document.createElement('div');
    instructionDiv.textContent = e.instruction || '';
    entry.appendChild(instructionDiv);

    const files = (e.files_changed || []).join(', ');
    if (files) {
      const filesDiv = document.createElement('div');
      filesDiv.className = 'hint';
      filesDiv.textContent = files;
      entry.appendChild(filesDiv);
    }

    if (canUndo) {
      const undoBtn = document.createElement('button');
      undoBtn.type = 'button';
      undoBtn.className = 'undo-btn';
      undoBtn.textContent = 'Undo';
      undoBtn.addEventListener('click', () => undoLastMerge(undoBtn));
      entry.appendChild(undoBtn);
    }

    return entry;
  }

  async function loadCodeLog() {
    try {
      const res = await fetch('/codemode/log');
      const data = await res.json();
      const entries = data.entries || [];
      codeLogEl.replaceChildren();
      if (!entries.length) {
        const empty = document.createElement('div');
        empty.className = 'empty';
        empty.textContent = 'No code-mode changes yet.';
        codeLogEl.appendChild(empty);
        return;
      }
      let undoShown = false;
      for (const e of entries) {
        const isRecentMerge = !undoShown && e.status === 'merged' && !e.undone_at;
        const withinWindow = isRecentMerge &&
          (Date.now() - new Date(e.confirmed_at).getTime()) < 24 * 3600 * 1000;
        if (isRecentMerge) undoShown = true; // only the single most recent merge is ever eligible
        codeLogEl.appendChild(buildCodeLogEntry(e, withinWindow));
      }
    } catch (err) {
      codeLogEl.replaceChildren();
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = 'Could not load the log.';
      codeLogEl.appendChild(empty);
    }
  }

  codeSubmitEl.addEventListener('click', startCodeMode);
  codeConfirmBtnEl.addEventListener('click', confirmCodeMode);
  loadCodeLog();
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
    (the default) means no gate, matching the original open behavior.

    /codemode/* is the one exception: it starts an agentic, Bash-capable
    Cloud Run Job against this repo with a push-capable GitHub token, a
    categorically bigger blast radius than the read-mostly chat/data routes
    this gate was designed around - so it refuses to run at all unless
    APP_PASSWORD is actually set, regardless of the rest of the app's
    auth mode."""
    password = os.environ.get("APP_PASSWORD")
    if request.path.startswith("/codemode/") and not password:
        return jsonify({"error": "Code mode requires APP_PASSWORD to be configured on this deployment."}), 503
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


@app.route("/chat", methods=["POST"])
def chat():
    """General-purpose Klaus chat: conversation, image attachments (vision),
    and link reading. Separate from /ask (trading-only Q&A above) by design -
    that endpoint and its behavior are unchanged."""
    body = request.get_json(silent=True) or {}
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return jsonify({"error": "No message."}), 400

    messages = messages[-KLAUS_MAX_HISTORY_MESSAGES:]

    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image":
                media_type = (block.get("source") or {}).get("media_type")
                if media_type not in ALLOWED_IMAGE_TYPES:
                    return jsonify({"error": f"Unsupported image type: {media_type}"}), 400

    augmented_messages, fetched = _augment_messages_with_url(messages)

    try:
        response = _get_client().messages.create(
            model=CHAT_MODEL,
            max_tokens=1024,
            system=KLAUS_SYSTEM_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            messages=augmented_messages,
        )
    except anthropic.APIError as e:
        return jsonify({"error": f"Claude request failed: {e}"}), 502

    answer = "".join(b.text for b in response.content if b.type == "text")
    return jsonify({"answer": answer, "fetched": fetched})


@app.route("/codemode/request", methods=["POST"])
def codemode_request():
    body = request.get_json(silent=True) or {}
    instruction = (body.get("instruction") or "").strip()
    if not instruction:
        return jsonify({"error": "Tell Klaus what to change first."}), 400

    try:
        request_id, branch = codemode.start_request(instruction)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409
    except Exception as e:
        return jsonify({"error": f"Couldn't start that: {e}"}), 502

    return jsonify({"request_id": request_id, "branch": branch})


@app.route("/codemode/status/<request_id>")
def codemode_status(request_id):
    pending = codemode.get_status(request_id)
    if pending is None:
        return jsonify({"error": "That request isn't pending anymore (it may have expired)."}), 404
    return jsonify(codemode.public_view(pending))


@app.route("/codemode/confirm", methods=["POST"])
def codemode_confirm():
    body = request.get_json(silent=True) or {}
    request_id = (body.get("request_id") or "").strip()
    code = (body.get("code") or "").strip()
    phrase = body.get("phrase") or ""
    if not request_id:
        return jsonify({"error": "Missing request id."}), 400

    ok, message, _entry = codemode.confirm_request(request_id, code, phrase)
    return jsonify({"ok": ok, "message": message}), (200 if ok else 400)


@app.route("/codemode/log")
def codemode_log():
    entries = codemode.read_log()
    return jsonify({"entries": list(reversed(entries))[:50]})


@app.route("/codemode/undo", methods=["POST"])
def codemode_undo():
    ok, message, _entry = codemode.undo_last_merge()
    return jsonify({"ok": ok, "message": message}), (200 if ok else 400)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
