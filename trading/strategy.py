from dataclasses import dataclass, asdict
from typing import Optional

VALID_ACTIONS = ("buy", "sell", "hold")


@dataclass
class Signal:
    """The format Layer 3 (decision.py) must produce and risk_manager.py /
    executor.py consume. `notional_usd` is None for "hold" signals."""

    symbol: str
    action: str
    confidence: float
    reasoning: str
    notional_usd: Optional[float] = None

    def __post_init__(self):
        if self.action not in VALID_ACTIONS:
            raise ValueError(f"Invalid action {self.action!r}, must be one of {VALID_ACTIONS}")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")
        if self.action != "hold" and (self.notional_usd is None or self.notional_usd <= 0):
            raise ValueError("notional_usd must be a positive number for buy/sell signals")

    def to_dict(self):
        return asdict(self)
