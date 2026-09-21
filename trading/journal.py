import json
import threading
from datetime import datetime, date

from . import storage

JOURNAL_BLOB = "journal.json"

_lock = threading.Lock()


def _read_all():
    text = storage.read_text(JOURNAL_BLOB)
    if text is None:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return []


def record_entry(entry: dict):
    """entry should include at least: symbol, signal (dict), risk_decision (dict),
    execution (dict or None). timestamp is added automatically."""
    entry = dict(entry)
    entry["timestamp"] = datetime.now().isoformat()
    with _lock:
        entries = _read_all()
        entries.append(entry)
        storage.write_text(JOURNAL_BLOB, json.dumps(entries, indent=2))
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
