import json
import os

import anthropic

from .. import data_client
from ._parsing import strip_json_fence

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def research_sentiment(symbol, days=5, limit=10):
    """Agent C (optional): general sentiment from reputable financial news via
    Alpaca's News API (aggregates wire sources like Benzinga - not forums),
    used as a minor input, not a primary signal."""
    try:
        headlines = data_client.get_recent_news(symbol, days=days, limit=limit)
    except Exception as e:
        return {"symbol": symbol, "confidence_note": f"news unavailable: {e}"}

    if not headlines:
        return {
            "symbol": symbol, "sentiment_label": "neutral", "headline_count": 0,
            "confidence_note": "no recent news found - minor input only",
        }

    headline_text = "\n".join(f"- {h['headline']} ({h['source']})" for h in headlines)
    prompt = (
        f"Recent financial news headlines about {symbol}:\n{headline_text}\n\n"
        "Classify overall sentiment as bullish, bearish, or neutral with a one-sentence "
        'reason. Respond as JSON only: {"sentiment": "bullish|bearish|neutral", "reason": "..."}'
    )
    response = _get_client().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        parsed = json.loads(strip_json_fence(text))
        sentiment = parsed.get("sentiment", "neutral")
        reason = parsed.get("reason", "")
    except json.JSONDecodeError:
        sentiment, reason = "neutral", "could not parse model output"

    return {
        "symbol": symbol,
        "sentiment_label": sentiment,
        "sentiment_reason": reason,
        "headline_count": len(headlines),
        "key_headlines": [h["headline"] for h in headlines[:3]],
        "confidence_note": "minor input only - small sample of recent headlines, not a primary signal",
    }
