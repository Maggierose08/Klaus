"""Long-term memory for Klaus's web chat (trading/web.py's /chat endpoint).

Distilled facts/preferences/decisions - not full conversation transcripts -
persisted to GCS so Klaus has continuity across separate browser sessions
and devices, mirroring journal.py's append-under-lock pattern. Populated by
the `remember` chat tool (web.py's CHAT_TOOLS), which Klaus calls when
something in a conversation is worth carrying forward.
"""
import json
import threading
from datetime import datetime, timezone

from . import storage

MEMORY_BLOB = "chat_memory.json"

# Hard cap on the persisted store itself (not just what's injected into a
# given request's context) - oldest facts drop off first, same bounded-
# growth discipline as journal.py's callers use elsewhere (RECENT_ENTRIES_
# LIMIT, KLAUS_MAX_HISTORY_MESSAGES, etc.).
MAX_MEMORY_ENTRIES = 300

_lock = threading.Lock()


def _read_all():
    text = storage.read_text(MEMORY_BLOB)
    if text is None:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return []


def remember(fact: str):
    """Appends a distilled fact to persistent chat memory. No-op on an
    empty/whitespace-only fact."""
    fact = (fact or "").strip()
    if not fact:
        return None
    entry = {"fact": fact, "timestamp": datetime.now(timezone.utc).isoformat()}
    with _lock:
        entries = _read_all()
        entries.append(entry)
        if len(entries) > MAX_MEMORY_ENTRIES:
            entries = entries[-MAX_MEMORY_ENTRIES:]
        storage.write_text(MEMORY_BLOB, json.dumps(entries, indent=2))
    return entry


def read_all():
    with _lock:
        return _read_all()
