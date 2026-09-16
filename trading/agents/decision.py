import json
import os

import anthropic

from .. import config
from ..strategy import Signal

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


SYSTEM_PROMPT = (
    "You are a trading decision agent for a PAPER TRADING account. Given a "
    "distilled research summary for one stock, decide buy, sell, or hold. "
    "Be conservative - prefer hold when the research is contradictory or "
    "low confidence. If buy or sell, propose a notional dollar amount to "
    f"trade (never more than ${config.MAX_POSITION_NOTIONAL_USD:.0f}). This "
    "decision still has to pass a separate mandatory risk check before "
    "anything executes, so focus on the investment reasoning, not risk limits. "
    'Respond as JSON only: {"action": "buy|sell|hold", "confidence": 0.0-1.0, '
    '"reasoning": "...", "notional_usd": number or null}'
)


def decide(synthesis_result) -> Signal:
    """Layer 3: formulates a concrete Signal (see strategy.py) from the
    Layer 2 research summary."""
    response = _get_client().messages.create(
        model="claude-sonnet-4-5",
        max_tokens=400,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(synthesis_result, default=str)}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {
            "action": "hold", "confidence": 0.0,
            "reasoning": "could not parse model output, defaulting to hold", "notional_usd": None,
        }

    action = parsed.get("action", "hold")
    if action not in ("buy", "sell", "hold"):
        action = "hold"

    confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.0) or 0.0)))

    notional = parsed.get("notional_usd")
    if action == "hold":
        notional = None
    else:
        if notional is None or notional <= 0:
            notional = config.MAX_POSITION_NOTIONAL_USD / 2
        notional = min(float(notional), config.MAX_POSITION_NOTIONAL_USD)

    return Signal(
        symbol=synthesis_result["symbol"],
        action=action,
        confidence=confidence,
        reasoning=parsed.get("reasoning", ""),
        notional_usd=notional,
    )
