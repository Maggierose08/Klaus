import time

import yfinance as yf

# Broad-market/index ETFs have no earnings, P/E, or analyst targets - Yahoo
# returns a 404 for their quoteSummary endpoint (printed by yfinance itself,
# not raised as an exception). Skip the fundamentals fetch for these rather
# than let that 404 print on every pipeline run.
KNOWN_ETFS = {"SPY", "QQQ", "DIA", "IWM", "VOO", "VTI", "IVV"}

# Yahoo's crumb-issuing endpoint intermittently 429s under load; yfinance
# then submits the literal error text as the crumb and the follow-up
# quoteSummary request comes back with an empty body (surfaces here as a
# JSONDecodeError: "Expecting value: line 1 column 1 (char 0)"). A short
# retry clears most of these since the rate limit is brief, not persistent.
_INFO_RETRY_ATTEMPTS = 2
_INFO_RETRY_DELAY_SECONDS = 2


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

    info, last_error = None, None
    for attempt in range(_INFO_RETRY_ATTEMPTS):
        try:
            info = ticker.info
            break
        except Exception as e:
            last_error = e
            if attempt < _INFO_RETRY_ATTEMPTS - 1:
                time.sleep(_INFO_RETRY_DELAY_SECONDS)

    if info is None:
        return {"symbol": symbol, "confidence_note": f"fundamentals unavailable: {last_error}"}

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
