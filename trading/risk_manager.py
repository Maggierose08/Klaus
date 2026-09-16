from dataclasses import dataclass

from . import config
from .strategy import Signal
from . import journal


@dataclass
class RiskDecision:
    approved: bool
    reason: str


def validate_trade(signal: Signal, account_state: dict = None, positions: list = None) -> RiskDecision:
    """The mandatory, non-bypassable gate. executor.py refuses to place any
    order unless it's handed a RiskDecision with approved=True that came from
    here (see executor.execute_trade). Every Signal from Layer 3, including
    "hold", is expected to be passed through this function before anything
    else happens with it."""

    if signal.action == "hold":
        return RiskDecision(True, "hold signal, nothing to execute")

    if signal.symbol not in config.WATCHLIST:
        return RiskDecision(False, f"{signal.symbol} is not in the watchlist")

    if signal.notional_usd > config.MAX_POSITION_NOTIONAL_USD:
        return RiskDecision(
            False,
            f"requested ${signal.notional_usd:.2f} exceeds max position size "
            f"${config.MAX_POSITION_NOTIONAL_USD:.2f}",
        )

    approved_today = journal.count_approved_trades_today()
    if approved_today >= config.MAX_DAILY_TRADES:
        return RiskDecision(
            False, f"already made {approved_today} trades today (max {config.MAX_DAILY_TRADES})"
        )

    positions = positions if positions is not None else []
    existing_position = next((p for p in positions if p["symbol"] == signal.symbol), None)

    if signal.action == "sell" and existing_position is None:
        return RiskDecision(False, f"no existing position in {signal.symbol} to sell (no shorting)")

    if signal.action == "buy":
        deployed = sum(p["market_value"] for p in positions)
        if deployed + signal.notional_usd > config.MAX_PORTFOLIO_NOTIONAL_USD:
            return RiskDecision(
                False,
                f"would push total deployed capital to ${deployed + signal.notional_usd:.2f}, "
                f"over the ${config.MAX_PORTFOLIO_NOTIONAL_USD:.2f} portfolio cap",
            )
        if account_state is not None and signal.notional_usd > account_state["buying_power"]:
            return RiskDecision(
                False,
                f"${signal.notional_usd:.2f} exceeds available buying power "
                f"(${account_state['buying_power']:.2f})",
            )

    return RiskDecision(True, "within all risk limits")
