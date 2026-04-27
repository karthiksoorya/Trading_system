import json
import logging
from datetime import datetime, timedelta

from kiteconnect import KiteConnect

import config
from brokers.base import BrokerBase, Candle, DepthLevel, MarketDepth, Quote

logger = logging.getLogger(__name__)

# Kite uses numeric instrument tokens, not symbol strings, for historical data.
# Add options tokens dynamically via lookup_instrument_token() when needed.
_KNOWN_TOKENS: dict[str, int] = {
    "NSE:NIFTY 50":   256265,
    "NSE:INDIA VIX":  264969,
    "NSE:NIFTY BANK": 260105,
}


class KiteAdapter(BrokerBase):

    def __init__(self):
        self._kite = KiteConnect(api_key=config.KITE_API_KEY)
        self._token_loaded = False
        self._load_token()

    # ── Token Management ───────────────────────────────────────────────────

    def _load_token(self):
        if not config.TOKEN_FILE.exists():
            logger.warning("No token file found. Call generate_session() first.")
            return
        try:
            data = json.loads(config.TOKEN_FILE.read_text())
            token = data.get("access_token")
            saved_date = data.get("date")
            if token and saved_date == datetime.today().strftime("%Y-%m-%d"):
                self._kite.set_access_token(token)
                self._token_loaded = True
                logger.info("Kite access token loaded (valid for today).")
            else:
                logger.warning("Stored token is from a previous day. Re-login required.")
        except Exception as e:
            logger.error("Failed to load token file: %s", e)

    def _save_token(self, access_token: str):
        config.TOKEN_FILE.write_text(json.dumps({
            "access_token": access_token,
            "date": datetime.today().strftime("%Y-%m-%d"),
        }))

    def generate_login_url(self) -> str:
        """Step 1 of daily auth: open this URL in browser to get request_token."""
        return self._kite.login_url()

    def generate_session(self, request_token: str) -> str:
        """Step 2 of daily auth: exchange request_token for access_token."""
        session = self._kite.generate_session(
            request_token, api_secret=config.KITE_API_SECRET
        )
        access_token = session["access_token"]
        self._kite.set_access_token(access_token)
        self._save_token(access_token)
        self._token_loaded = True
        logger.info("Kite session established and token saved.")
        return access_token

    # ── BrokerBase interface ───────────────────────────────────────────────

    def get_ltp(self, symbol: str) -> float:
        data = self._kite.ltp([symbol])
        return data[symbol]["last_price"]

    def get_quote(self, symbol: str) -> Quote:
        raw = self._kite.quote([symbol])[symbol]
        ohlc = raw["ohlc"]
        depth_raw = raw.get("depth", {})
        depth = None
        if depth_raw:
            depth = MarketDepth(
                buy=[DepthLevel(**lvl) for lvl in depth_raw.get("buy", [])],
                sell=[DepthLevel(**lvl) for lvl in depth_raw.get("sell", [])],
            )
        return Quote(
            ltp=raw["last_price"],
            open=ohlc["open"],
            high=ohlc["high"],
            low=ohlc["low"],
            close=ohlc["close"],
            depth=depth,
        )

    def get_historical(self, symbol: str, interval: str, days: int) -> list[Candle]:
        token = self._resolve_token(symbol)
        to_date   = datetime.now()
        from_date = to_date - timedelta(days=days)
        raw = self._kite.historical_data(
            instrument_token=token,
            from_date=from_date.strftime("%Y-%m-%d %H:%M:%S"),
            to_date=to_date.strftime("%Y-%m-%d %H:%M:%S"),
            interval=interval,
            continuous=False,
            oi=False,
        )
        return [
            Candle(
                timestamp=c["date"],
                open=c["open"],
                high=c["high"],
                low=c["low"],
                close=c["close"],
                volume=c["volume"],
            )
            for c in raw
        ]

    def is_connected(self) -> bool:
        if not self._token_loaded:
            return False
        try:
            self._kite.profile()
            return True
        except Exception:
            return False

    # ── Helpers ────────────────────────────────────────────────────────────

    def _resolve_token(self, symbol: str) -> int:
        if symbol in _KNOWN_TOKENS:
            return _KNOWN_TOKENS[symbol]
        # Dynamic lookup for options/futures added in later phases
        raise ValueError(
            f"Instrument token unknown for '{symbol}'. "
            "Add to _KNOWN_TOKENS or use lookup_instrument_token()."
        )

    def lookup_instrument_token(self, exchange: str, tradingsymbol: str) -> int:
        """Fetch and cache instrument token by tradingsymbol (e.g. 'NIFTY24500CE')."""
        instruments = self._kite.instruments(exchange)
        for inst in instruments:
            if inst["tradingsymbol"] == tradingsymbol:
                key = f"{exchange}:{tradingsymbol}"
                _KNOWN_TOKENS[key] = inst["instrument_token"]
                return inst["instrument_token"]
        raise ValueError(f"Instrument not found: {exchange}:{tradingsymbol}")
