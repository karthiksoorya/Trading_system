import config
from brokers.base import Candle


def is_boring(candle: Candle) -> bool:
    """Body < 50% of total range — balance / accumulation."""
    return candle.body_ratio < config.EXCITING_CANDLE_BODY_RATIO


def is_exciting(candle: Candle) -> bool:
    """Body >= 50% of total range — imbalance / directional move."""
    return candle.body_ratio >= config.EXCITING_CANDLE_BODY_RATIO


def classify(candle: Candle) -> str:
    return "exciting" if is_exciting(candle) else "boring"
