from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import StockBarsRequest, NewsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from datetime import datetime, timedelta

from . import config

_trading_client = None
_stock_data_client = None
_news_client = None


def get_trading_client():
    global _trading_client
    if _trading_client is None:
        config.require_alpaca_credentials()
        _trading_client = TradingClient(
            config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY,
            paper=True, url_override=config.ALPACA_BASE_URL,
        )
    return _trading_client


def get_stock_data_client():
    global _stock_data_client
    if _stock_data_client is None:
        config.require_alpaca_credentials()
        _stock_data_client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
    return _stock_data_client


def get_news_client():
    global _news_client
    if _news_client is None:
        config.require_alpaca_credentials()
        _news_client = NewsClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
    return _news_client


def get_account_state():
    account = get_trading_client().get_account()
    return {
        "cash": float(account.cash),
        "equity": float(account.equity),
        "buying_power": float(account.buying_power),
        "portfolio_value": float(account.portfolio_value),
    }


def get_open_positions():
    positions = get_trading_client().get_all_positions()
    return [
        {
            "symbol": p.symbol,
            "qty": float(p.qty),
            "market_value": float(p.market_value),
            "unrealized_pl": float(p.unrealized_pl),
        }
        for p in positions
    ]


def get_daily_bars(symbol, days=60):
    client = get_stock_data_client()
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=datetime.now() - timedelta(days=days),
    )
    bars = client.get_stock_bars(request)
    return [
        {
            "date": bar.timestamp.date().isoformat(),
            "close": float(bar.close),
            "volume": float(bar.volume),
        }
        for bar in bars.data.get(symbol, [])
    ]


def get_recent_news(symbol, days=5, limit=10):
    client = get_news_client()
    request = NewsRequest(
        symbols=symbol,
        start=datetime.now() - timedelta(days=days),
        limit=limit,
    )
    news_set = client.get_news(request)
    seen_ids = set()
    result = []
    for items in news_set.data.values():
        for item in items:
            if item.id in seen_ids:
                continue
            seen_ids.add(item.id)
            result.append({
                "headline": item.headline,
                "source": item.source,
                "created_at": item.created_at.isoformat(),
            })
    return result


def place_market_order(symbol, side, notional_usd):
    """side: "buy" or "sell". Only ever called by executor.py, and only after
    risk_manager.py has approved the trade."""
    order = MarketOrderRequest(
        symbol=symbol,
        notional=round(notional_usd, 2),
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    )
    return get_trading_client().submit_order(order)
