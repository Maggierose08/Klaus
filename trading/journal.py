import json
import os
import threading
from datetime import datetime, date

from .config import JOURNAL_PATH

_lock = threading.Lock()


def _read_all():
    if not os.path.exists(JOURNAL_PATH):
        return []
    try:
        with open(JOURNAL_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def record_entry(entry: dict):
    """entry should include at least: symbol, signal (dict), risk_decision (dict),
    execution (dict or None). timestamp is added automatically."""
    entry = dict(entry)
    entry["timestamp"] = datetime.now().isoformat()
    with _lock:
        entries = _read_all()
        entries.append(entry)
        with open(JOURNAL_PATH, "w") as f:
            json.dump(entries, f, indent=2)
    return entry


def read_all():
    with _lock:
        return _read_all()


def read_entries_for_date(target_date: date = None):
    target_date = target_date or date.today()
    return [
        e for e in read_all()
        if datetime.fromisoformat(e["timestamp"]).date() == target_date
    ]


def count_approved_trades_today():
    today_entries = read_entries_for_date()
    return sum(
        1 for e in today_entries
        if e.get("risk_decision", {}).get("approved") and e.get("signal", {}).get("action") != "hold"
    )
