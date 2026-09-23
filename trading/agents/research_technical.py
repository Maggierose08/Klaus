import statistics

from .. import data_client


def _sma(values, window):
    if len(values) < window:
        return None
    return statistics.mean(values[-window:])


def _rsi(closes, window=14):
    if len(closes) < window + 1:
        return None

    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = changes[-window:]
    gains = [c for c in recent if c > 0]
    losses = [-c for c in recent if c < 0]

    avg_gain = sum(gains) / window
    avg_loss = sum(losses) / window

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def research_technical(symbol, lookback_days=60):
    """Agent D: technical analysis - moving averages, momentum, support/
    resistance, and risk/reward, from Alpaca's own daily price/volume data."""
    bars = data_client.get_daily_bars(symbol, days=lookback_days)

    if len(bars) < 20:
        return {"symbol": symbol, "confidence_note": "insufficient price history for technical analysis"}

    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]
    current_price = closes[-1]

    sma_20 = _sma(closes, 20)
    sma_50 = _sma(closes, 50)
    if sma_20 is not None and sma_50 is not None:
        crossover = "bullish" if sma_20 > sma_50 else "bearish" if sma_20 < sma_50 else "flat"
    else:
        crossover = None

    rsi_14 = _rsi(closes, 14)
    if rsi_14 is None:
        rsi_signal = None
    elif rsi_14 > 70:
        rsi_signal = "overbought"
    elif rsi_14 < 30:
        rsi_signal = "oversold"
    else:
        rsi_signal = "neutral"

    sr_window = closes[-20:]
    resistance = max(sr_window)
    support = min(sr_window)

    dist_to_support = current_price - support
    dist_to_resistance = resistance - current_price
    if dist_to_support <= 0:
        risk_reward_ratio = None
    else:
        risk_reward_ratio = round(dist_to_resistance / dist_to_support, 2)

    price_trend_up = closes[-1] > closes[-6] if len(closes) > 5 else None
    avg_volume = statistics.mean(volumes[-20:])
    recent_avg_volume = statistics.mean(volumes[-5:]) if len(volumes) >= 5 else avg_volume
    volume_rising = recent_avg_volume > avg_volume * 1.1

    if price_trend_up is None:
        volume_confirmation = None
    elif price_trend_up and volume_rising:
        volume_confirmation = "bullish confirmation (price and volume both rising)"
    elif not price_trend_up and volume_rising:
        volume_confirmation = "bearish confirmation (price falling on rising volume)"
    elif price_trend_up and not volume_rising:
        volume_confirmation = "weak/unconfirmed rally (price rising on soft volume)"
    else:
        volume_confirmation = "weak/unconfirmed decline (price falling on soft volume)"

    bias_votes = []
    if crossover:
        bias_votes.append(crossover)
    if rsi_signal == "overbought":
        bias_votes.append("bearish")
    elif rsi_signal == "oversold":
        bias_votes.append("bullish")
    if volume_confirmation and "bullish" in volume_confirmation:
        bias_votes.append("bullish")
    elif volume_confirmation and "bearish" in volume_confirmation:
        bias_votes.append("bearish")

    if not bias_votes:
        technical_bias = "neutral"
    elif all(v == "bullish" for v in bias_votes):
        technical_bias = "bullish"
    elif all(v == "bearish" for v in bias_votes):
        technical_bias = "bearish"
    else:
        technical_bias = "mixed signals"

    return {
        "symbol": symbol,
        "current_price": round(current_price, 2),
        "sma_20": round(sma_20, 2) if sma_20 is not None else None,
        "sma_50": round(sma_50, 2) if sma_50 is not None else None,
        "sma_crossover": crossover,
        "rsi_14": round(rsi_14, 2) if rsi_14 is not None else None,
        "rsi_signal": rsi_signal,
        "support_20d": round(support, 2),
        "resistance_20d": round(resistance, 2),
        "risk_reward_ratio": risk_reward_ratio,
        "volume_confirmation": volume_confirmation,
        "technical_bias": technical_bias,
        "days_of_data": len(bars),
        "confidence_note": (
            "full history available" if len(bars) >= lookback_days * 0.8
            else f"limited history: only {len(bars)} of {lookback_days} requested days"
        ),
    }
