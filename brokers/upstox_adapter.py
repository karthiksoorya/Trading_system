from brokers.base import BrokerBase, Candle, Quote


class UpstoxAdapter(BrokerBase):
    """Placeholder — implement when Upstox API is enabled on the account."""

    def get_ltp(self, symbol: str) -> float:
        raise NotImplementedError("UpstoxAdapter not yet implemented.")

    def get_quote(self, symbol: str) -> Quote:
        raise NotImplementedError("UpstoxAdapter not yet implemented.")

    def get_historical(self, symbol: str, interval: str, days: int) -> list[Candle]:
        raise NotImplementedError("UpstoxAdapter not yet implemented.")

    def is_connected(self) -> bool:
        return False
