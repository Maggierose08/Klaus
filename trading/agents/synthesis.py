import json
import os

import anthropic

from ._parsing import strip_json_fence

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


SYSTEM_PROMPT = (
    "You are a research synthesis analyst. You'll be given structured data from "
    "up to three research agents (price/volume/volatility, fundamentals, and news "
    "sentiment) about one stock. Cross-reference them, explicitly flag any "
    "contradictions between them (e.g. strong price momentum but weak fundamentals, "
    "or bullish sentiment with no supporting price action), and produce ONE distilled "
    "summary a trading decision could be based on. Weight news sentiment as a minor "
    "signal compared to price and fundamentals data. "
    'Respond as JSON only: {"summary": "...", "contradictions": ["..."], '
    '"data_confidence": "high|medium|low"}'
)


def synthesize(symbol, price_data, fundamentals_data, sentiment_data=None):
    """Layer 2: cross-references Layer 1 outputs into one distilled summary."""
    payload = {
        "symbol": symbol,
        "price_and_volume": price_data,
        "fundamentals": fundamentals_data,
        "news_sentiment": sentiment_data,
    }
    response = _get_client().messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        parsed = json.loads(strip_json_fence(text))
    except json.JSONDecodeError:
        parsed = {"summary": text, "contradictions": [], "data_confidence": "low"}

    return {
        "symbol": symbol,
        "summary": parsed.get("summary", ""),
        "contradictions": parsed.get("contradictions", []),
        "data_confidence": parsed.get("data_confidence", "low"),
    }
