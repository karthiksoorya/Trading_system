"""Deterministic offline reflection from explicit completed-trade assessments."""

from dataclasses import dataclass
from enum import Enum
import math


class ReflectionClassification(str, Enum):
    ZONE_CORRECT_OPTION_CORRECT = "ZONE_CORRECT_OPTION_CORRECT"
    ZONE_CORRECT_OPTION_WRONG = "ZONE_CORRECT_OPTION_WRONG"
    ZONE_WRONG_OPTION_PROFIT = "ZONE_WRONG_OPTION_PROFIT"
    ZONE_WRONG_OPTION_LOSS = "ZONE_WRONG_OPTION_LOSS"
    LATE_ENTRY = "LATE_ENTRY"
    WRONG_STRIKE = "WRONG_STRIKE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, kw_only=True)
class TradeReflectionInput:
    completed: bool
    zone_correct: bool | None = None
    option_correct: bool | None = None
    option_pnl: float | None = None  # Net realized option P&L after costs.
    late_entry: bool = False
    wrong_strike: bool = False

    def __post_init__(self):
        for name in ("completed", "late_entry", "wrong_strike"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        for name in ("zone_correct", "option_correct"):
            if getattr(self, name) is not None and type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean or None")
        if self.option_pnl is not None and (
            type(self.option_pnl) not in (float, int) or not math.isfinite(self.option_pnl)
        ):
            raise ValueError("option_pnl must be finite or None")


def classify_trade(trade: TradeReflectionInput) -> ReflectionClassification:
    """Precedence: incomplete -> unknown; late entry; wrong strike; outcome.

    Correctness is a supplied review judgment, never inferred from profit alone.
    Missing required judgments and wrong-zone breakeven produce UNKNOWN.
    """
    if not isinstance(trade, TradeReflectionInput):
        raise TypeError("Structured TradeReflectionInput required")
    result = ReflectionClassification
    if not trade.completed:
        return result.UNKNOWN
    if trade.late_entry:
        return result.LATE_ENTRY
    if trade.wrong_strike:
        return result.WRONG_STRIKE
    if trade.zone_correct is True:
        if trade.option_correct is True:
            return result.ZONE_CORRECT_OPTION_CORRECT
        if trade.option_correct is False:
            return result.ZONE_CORRECT_OPTION_WRONG
    if trade.zone_correct is False and trade.option_pnl is not None:
        if trade.option_pnl > 0:
            return result.ZONE_WRONG_OPTION_PROFIT
        if trade.option_pnl < 0:
            return result.ZONE_WRONG_OPTION_LOSS
    return result.UNKNOWN
