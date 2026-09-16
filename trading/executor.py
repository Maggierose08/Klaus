from dataclasses import dataclass
from typing import Optional

from . import data_client
from . import risk_manager
from .strategy import Signal
from .risk_manager import RiskDecision


@dataclass
class ExecutionResult:
    executed: bool
    risk_decision: RiskDecision
    order_id: Optional[str] = None
    error: Optional[str] = None


def execute_trade(signal: Signal) -> ExecutionResult:
    """The only place in this codebase that calls data_client.place_market_order.
    risk_manager.validate_trade() is called HERE, internally, rather than trusted
    from the caller - so there is no code path to a live order that skips it,
    regardless of what pipeline.py (or anything else) does or forgets to do."""

    account_state = data_client.get_account_state()
    positions = data_client.get_open_positions()

    risk_decision = risk_manager.validate_trade(signal, account_state, positions)

    if not risk_decision.approved or signal.action == "hold":
        return ExecutionResult(executed=False, risk_decision=risk_decision)

    try:
        order = data_client.place_market_order(signal.symbol, signal.action, signal.notional_usd)
        return ExecutionResult(executed=True, risk_decision=risk_decision, order_id=str(order.id))
    except Exception as e:
        return ExecutionResult(executed=False, risk_decision=risk_decision, error=str(e))
