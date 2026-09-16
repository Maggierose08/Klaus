import statistics

from .. import data_client


def research_price(symbol, lookback_days=60):
    """Agent A: historical price/volume data and volatility metrics, from
    Alpaca's own market data - no forum scraping."""
    bars = data_client.get_daily_bars(symbol, days=lookback_days)

    if len(bars) < 2:
        return {"symbol": symbol, "confidence_note": "insufficient price history from Alpaca"}

    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]
    returns = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]

    pct_change_5d = (closes[-1] - closes[-6]) / closes[-6] * 100 if len(closes) > 5 else None
    pct_change_20d = (closes[-1] - closes[-21]) / closes[-21] * 100 if len(closes) > 20 else None

    vol_window = returns[-20:] if len(returns) >= 20 else returns
    volatility_20d = statistics.pstdev(vol_window) * 100 if len(vol_window) >= 2 else None

    avg_volume = statistics.mean(volumes[-20:]) if len(volumes) >= 20 else statistics.mean(volumes)
    recent_avg_volume = statistics.mean(volumes[-5:]) if len(volumes) >= 5 else avg_volume
    if recent_avg_volume > avg_volume * 1.1:
        volume_trend = "above average"
    elif recent_avg_volume < avg_volume * 0.9:
        volume_trend = "below average"
    else:
        volume_trend = "normal"

    return {
        "symbol": symbol,
        "current_price": round(closes[-1], 2),
        "pct_change_5d": round(pct_change_5d, 2) if pct_change_5d is not None else None,
        "pct_change_20d": round(pct_change_20d, 2) if pct_change_20d is not None else None,
        "volatility_20d_pct": round(volatility_20d, 2) if volatility_20d is not None else None,
        "avg_volume_20d": round(avg_volume),
        "volume_trend": volume_trend,
        "days_of_data": len(bars),
        "confidence_note": (
            "full history available" if len(bars) >= lookback_days * 0.8
            else f"limited history: only {len(bars)} of {lookback_days} requested days"
        ),
    }
