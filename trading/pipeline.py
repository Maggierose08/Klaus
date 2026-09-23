from . import config
from . import journal
from . import executor
from .agents.research_price import research_price
from .agents.research_fundamentals import research_fundamentals
from .agents.research_sentiment import research_sentiment
from .agents.research_technical import research_technical
from .agents.synthesis import synthesize
from .agents.decision import decide

USE_SENTIMENT_AGENT = True  # Agent C is optional per spec; flip off to skip it


def run_trading_cycle(symbols=None):
    """Wires research -> synthesis -> decision -> risk check -> execute ->
    journal for each symbol. The risk check (risk_manager.validate_trade) is
    enforced inside executor.execute_trade itself, not just called here in
    sequence, so it can't be skipped by forgetting a step in this function."""
    symbols = symbols or config.WATCHLIST
    entries = []

    for symbol in symbols:
        try:
            price_data = research_price(symbol)
            fundamentals_data = research_fundamentals(symbol)
            sentiment_data = research_sentiment(symbol) if USE_SENTIMENT_AGENT else None
            technical_data = research_technical(symbol)

            synthesis_result = synthesize(
                symbol, price_data, fundamentals_data, sentiment_data, technical_data
            )
            signal = decide(synthesis_result)

            execution_result = executor.execute_trade(signal)

            entry = journal.record_entry({
                "symbol": symbol,
                "research": {
                    "price": price_data,
                    "fundamentals": fundamentals_data,
                    "sentiment": sentiment_data,
                    "technical": technical_data,
                },
                "synthesis": synthesis_result,
                "signal": signal.to_dict(),
                "risk_decision": {
                    "approved": execution_result.risk_decision.approved,
                    "reason": execution_result.risk_decision.reason,
                },
                "execution": None if signal.action == "hold" else {
                    "executed": execution_result.executed,
                    "order_id": execution_result.order_id,
                    "error": execution_result.error,
                },
            })
        except Exception as e:
            entry = journal.record_entry({
                "symbol": symbol,
                "error": str(e),
                "signal": None,
                "risk_decision": {"approved": False, "reason": f"pipeline error: {e}"},
                "execution": None,
            })

        entries.append(entry)

    return entries


if __name__ == "__main__":
    for entry in run_trading_cycle():
        signal = entry.get("signal") or {}
        action = signal.get("action", "error")
        approved = entry["risk_decision"]["approved"]
        print(f"{entry['symbol']}: {action} - risk approved={approved}")
