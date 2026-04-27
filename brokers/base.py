from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def total_range(self) -> float:
        return self.high - self.low

    @property
    def body_ratio(self) -> float:
        return self.body / self.total_range if self.total_range > 0 else 0

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open


@dataclass
class DepthLevel:
    price: float
    quantity: int
    orders: int


@dataclass
class MarketDepth:
    buy: list[DepthLevel] = field(default_factory=list)
    sell: list[DepthLevel] = field(default_factory=list)


@dataclass
class Quote:
    ltp: float
    open: float
    high: float
    low: float
    close: float
    depth: Optional[MarketDepth] = None


class BrokerBase(ABC):

    @abstractmethod
    def get_ltp(self, symbol: str) -> float:
        """Last traded price."""

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        """Full quote: OHLC + market depth."""

    @abstractmethod
    def get_historical(self, symbol: str, interval: str, days: int) -> list[Candle]:
        """OHLC candles for the past `days` calendar days.

        interval: "5minute" | "15minute" | "60minute" | "day"
        """

    @abstractmethod
    def is_connected(self) -> bool:
        """True if the broker session is valid."""
