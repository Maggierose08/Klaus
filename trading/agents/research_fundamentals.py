import yfinance as yf

# Broad-market/index ETFs have no earnings, P/E, or analyst targets - Yahoo
# returns a 404 for their quoteSummary endpoint (printed by yfinance itself,
# not raised as an exception). Skip the fundamentals fetch for these rather
# than let that 404 print on every pipeline run.
KNOWN_ETFS = {"SPY", "QQQ", "DIA", "IWM", "VOO", "VTI", "IVV"}


def research_fundamentals(symbol):
    """Agent B: earnings/fundamentals via Yahoo Finance (free, no forum
    scraping). Alpaca's own fundamentals endpoints require a higher-tier
    subscription not available on a basic paper account, so this uses
    yfinance instead."""
    if symbol.upper() in KNOWN_ETFS:
        return {
            "symbol": symbol,
            "confidence_note": "ETF - no company fundamentals; rely on price/sentiment signals instead",
        }

    ticker = yf.Ticker(symbol)

    try:
        info = ticker.info
    except Exception as e:
        return {"symbol": symbol, "confidence_note": f"fundamentals unavailable: {e}"}

    if not info or (info.get("trailingPE") is None and info.get("marketCap") is None):
        return {"symbol": symbol, "confidence_note": "no fundamentals data returned for this symbol"}

    next_earnings_date = None
    try:
        calendar = ticker.calendar
        earnings_dates = calendar.get("Earnings Date") if isinstance(calendar, dict) else None
        if earnings_dates:
            next_earnings_date = str(earnings_dates[0])
    except Exception:
        pass

    return {
        "symbol": symbol,
        "sector": info.get("sector"),
        "market_cap": info.get("marketCap"),
        "trailing_pe": info.get("trailingPE"),
        "forward_pe": info.get("forwardPE"),
        "trailing_eps": info.get("trailingEps"),
        "forward_eps": info.get("forwardEps"),
        "analyst_target_mean_price": info.get("targetMeanPrice"),
        "analyst_recommendation": info.get("recommendationKey"),
        "next_earnings_date": next_earnings_date,
        "confidence_note": "Yahoo Finance data - fields may be partially missing depending on the symbol",
    }
