from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from datetime import timezone, timedelta
import math

IST = timezone(timedelta(hours=5, minutes=30))


class EntryValidationRejected(ValueError):
    """Validation failed before sending any order request to the broker."""


def broker_time(value) -> str | None:
    """Normalize broker timestamps; naive exchange timestamps are IST, never now."""
    if value in (None, ""):
        return None
    try:
        try:
            dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            dt = datetime.strptime(str(value), '%d-%b-%Y %H:%M:%S')
        return (dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)).isoformat()
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class UnderlyingObservation:
    price: float
    observed_at: str | None
    basis: str = "confirmation_quote"


@dataclass(frozen=True)
class ExecutionFill:
    execution_id: str
    quantity: int
    price: float
    timestamp: str | None
    timestamp_basis: str = "broker_exchange_execution"


@dataclass(frozen=True)
class OrderExecution:
    order_id: str
    status: str
    filled_quantity: int
    fills: tuple[ExecutionFill, ...] = ()
    side: str = ''
    symbol: str = ''
    requested_quantity: int = 0

    @property
    def terminal(self):
        return self.status in ("COMPLETE", "CANCELLED", "REJECTED")


def normalize_execution(order_id, raw, trades):
    if str(raw.get('order_id')) != str(order_id):
        raise ValueError('Order identity mismatch')
    fills = []
    for trade in trades:
        if str(trade.get('order_id')) != str(order_id):
            raise ValueError('Execution belongs to another order')
        identity = trade.get("trade_id")
        quantity = int(trade.get("quantity", 0))
        price = float(trade.get("average_price", trade.get("trade_price", 0)))
        if not identity or quantity <= 0 or not math.isfinite(price) or price <= 0:
            raise ValueError("Broker returned incomplete execution data")
        stamp = broker_time(trade.get("exchange_timestamp"))
        fills.append(ExecutionFill(str(identity), quantity, price, stamp,
                                   "broker_exchange_execution" if stamp else "unknown"))
    return OrderExecution(str(order_id), str(raw.get("status", "UNKNOWN")).upper(),
                          int(raw.get("filled_quantity", 0)), tuple(fills),
                          raw.get('transaction_type', ''),
                          str(raw.get('instrument_token') if '|' in str(raw.get('instrument_token', '')) else raw.get('tradingsymbol', raw.get('trading_symbol', ''))),
                          int(raw.get('quantity', 0)))


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

    def get_order_execution(self, order_id: str) -> OrderExecution:
        raise NotImplementedError("Broker does not expose execution details")

    def get_underlying_observation(self, symbol: str) -> UnderlyingObservation:
        # Request start is conservative: slow responses fail the freshness gate.
        requested_at = datetime.now(IST).isoformat()
        return UnderlyingObservation(self.get_ltp(symbol), requested_at)

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

    def get_options_ltp(self, instrument_key: str) -> float | None:
        """Live premium for an options contract. Returns None on failure.

        instrument_key is whatever format the broker stores in options_symbol:
          Kite   → "NIFTY24500CE"  (tradingsymbol, looked up as NFO:symbol)
          Upstox → "NSE_FO|37668"  (instrument_key from option chain)
        Default implementation returns None — override in each adapter.
        """
        return None
