import json
import os
import smtplib
from datetime import date
from email.message import EmailMessage

import anthropic

from . import journal
from . import data_client
from . import storage

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def is_trading_day(d: date = None):
    d = d or date.today()
    return d.weekday() < 5  # Mon-Fri; markets are closed Sat/Sun


def generate_daily_summary(target_date: date = None) -> str:
    """Layer 4: reads target_date's journal entries and produces a plain-
    English summary suitable for reading aloud through Klaus."""
    target_date = target_date or date.today()

    if not is_trading_day(target_date):
        return f"{target_date.strftime('%A')} was a weekend, markets were closed. Nothing to report."

    entries = journal.read_entries_for_date(target_date)
    if not entries:
        return f"No trading activity recorded for {target_date.isoformat()} yet."

    try:
        account_state = data_client.get_account_state()
    except Exception:
        account_state = None

    trades = [e for e in entries if e.get("signal") and e["signal"].get("action") != "hold"]
    rejected = [e for e in entries if not e.get("risk_decision", {}).get("approved")]

    payload = {
        "date": target_date.isoformat(),
        "total_symbols_reviewed": len(entries),
        "trades_attempted": [
            {
                "symbol": e["symbol"],
                "action": e["signal"]["action"],
                "notional_usd": e["signal"].get("notional_usd"),
                "executed": bool(e.get("execution") and e["execution"].get("executed")),
                "reasoning": e["signal"].get("reasoning"),
            }
            for e in trades
        ],
        "risk_manager_rejections": [
            {"symbol": e["symbol"], "reason": e["risk_decision"]["reason"]}
            for e in rejected
        ],
        "account_state": account_state,
    }

    prompt = (
        "Summarize today's PAPER trading activity in plain, spoken-friendly English "
        "for someone who will hear this read aloud by a voice assistant. Cover: what "
        "trades were made and why, whether they actually executed, the current paper "
        "account balance, and any risk-manager rejections (and why). Keep it "
        "conversational and under 150 words. Data:\n" + json.dumps(payload, default=str)
    )
    response = _get_client().messages.create(
        model="claude-sonnet-4-5",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in response.content if b.type == "text")


def _summary_blob(target_date: date):
    return f"summaries/{target_date.isoformat()}.txt"


def save_summary(target_date: date, text: str):
    path = _summary_blob(target_date)
    storage.write_text(path, text)
    return path


def get_todays_summary(regenerate=False) -> str:
    """Used by Klaus on-demand ("how'd trading go today"). Returns today's
    cached summary if the scheduled weekday run already produced one;
    otherwise generates fresh (e.g. asked mid-day, or no scheduled task set up)."""
    today = date.today()
    if not regenerate:
        cached = storage.read_text(_summary_blob(today))
        if cached is not None:
            return cached
    return generate_daily_summary(today)


def send_summary_email(target_date: date, text: str):
    """Emails the summary to GMAIL_ADDRESS via Gmail SMTP using an app
    password (GMAIL_APP_PASSWORD), since regular account passwords don't
    work with SMTP auth once 2FA is on."""
    gmail_address = os.environ["GMAIL_ADDRESS"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]

    msg = EmailMessage()
    msg["Subject"] = f"Trading summary - {target_date.isoformat()}"
    msg["From"] = gmail_address
    msg["To"] = gmail_address
    msg.set_content(text)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(gmail_address, gmail_app_password)
        smtp.send_message(msg)


def run_scheduled_summary():
    """Entry point for the weekday-only scheduled task."""
    today = date.today()
    if not is_trading_day(today):
        print(f"{today.isoformat()} ({today.strftime('%A')}) is a weekend, skipping daily summary.")
        return
    summary = generate_daily_summary(today)
    path = save_summary(today, summary)
    print(f"Saved daily summary to {path}")
    print(summary)

    try:
        send_summary_email(today, summary)
        print(f"Emailed daily summary to {os.environ.get('GMAIL_ADDRESS')}")
    except Exception as e:
        print(f"Failed to email daily summary: {e}")


if __name__ == "__main__":
    run_scheduled_summary()
