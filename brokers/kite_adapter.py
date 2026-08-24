import json
import logging
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

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

# Module-level instruments cache — shared across all KiteAdapter instances.
# instruments("NFO") fetches ~10k rows; caching avoids the 1-3s delay at approval time.
_instruments_cache: dict[str, tuple[float, list]] = {}
_INSTRUMENTS_TTL = 1800   # seconds — refresh every 30 min


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

    def capture_token_via_server(self, port: int = 5000, timeout: int = 180) -> str:
        """
        Option B — VPS auto token capture.
        Disabled by default (KITE_TOKEN_MODE = "manual" in config).

        Starts a local HTTP server, waits for Kite to redirect back with
        the request_token in the URL, captures it automatically.

        To enable:
          1. Set KITE_TOKEN_MODE = "auto" in config.py
          2. Update redirect URL in Kite developer console to:
                http://YOUR_VPS_IP:5000/
          3. Open firewall port 5000 on the VPS

        Returns the access_token after completing the session.
        """
        captured = {}

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                params = parse_qs(urlparse(self.path).query)
                if "request_token" in params:
                    captured["request_token"] = params["request_token"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h2>Token captured. You can close this tab.</h2>")
                threading.Thread(target=self.server.shutdown, daemon=True).start()

            def log_message(self, *args):
                pass  # suppress HTTP server logs

        server = HTTPServer(("0.0.0.0", port), _Handler)
        server.timeout = timeout
        logger.info("Waiting for Kite redirect on port %d (timeout: %ds)...", port, timeout)
        server.serve_forever()

        request_token = captured.get("request_token", "")
        if not request_token:
            raise TimeoutError("No request_token received within timeout window.")

        return self.generate_session(request_token)

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

    def _get_instruments(self, exchange: str) -> list:
        """Return instruments list, using a 30-min module-level cache to avoid per-approval delay."""
        now = time.time()
        cached = _instruments_cache.get(exchange)
        if cached and (now - cached[0]) < _INSTRUMENTS_TTL:
            return cached[1]
        data = self._kite.instruments(exchange)
        _instruments_cache[exchange] = (now, data)
        logger.info("Instruments cache refreshed for %s (%d records)", exchange, len(data))
        return data

    def prefetch_instruments(self) -> None:
        """Pre-warm the instruments cache. Call this right after login so approval is instant."""
        self._get_instruments("NFO")

    def get_options_contract(self, ltp: float, direction: str) -> dict:
        """Find 1-strike ITM weekly Nifty options contract. direction='demand'→CE, 'supply'→PE.
        ITM (delta ~0.6) reduces IV crush vs ATM — option tracks index move more closely."""
        from datetime import date, timedelta, datetime as dt
        option_type = "CE" if direction == "demand" else "PE"
        atm = round(ltp / 50) * 50
        # 1 strike ITM: for CE go 50pts below ATM, for PE go 50pts above ATM
        strike = (atm - 50) if option_type == "CE" else (atm + 50)

        # Next Tuesday expiry (NSE changed Nifty weekly expiry from Thursday to Tuesday)
        today = date.today()
        days_ahead = (1 - today.weekday()) % 7          # 1 = Tuesday
        # FIX B: skip expiries within 2 calendar days — extreme gamma/theta risk.
        # Previously only same-day (days_ahead==0) was skipped, but a contract
        # expiring tomorrow is nearly as bad (wide spreads, rapid time decay).
        if days_ahead <= 1:
            days_ahead += 7   # jump to the following week's Tuesday
        expiry = today + timedelta(days=days_ahead)

        instruments = self._get_instruments("NFO")

        def _norm_expiry(val):
            """Normalise kiteconnect expiry to date regardless of type."""
            if val is None:
                return None
            if hasattr(val, "date"):        # datetime → date
                return val.date()
            if isinstance(val, str):
                return date.fromisoformat(val[:10])
            return val                      # already a date

        # Try current week expiry first, then next week as fallback
        for expiry_try in [expiry, expiry + timedelta(days=7)]:
            for inst in instruments:
                if inst.get("name") != "NIFTY":
                    continue
                if inst.get("instrument_type") != option_type:
                    continue
                if inst.get("strike") != float(strike):
                    continue
                if _norm_expiry(inst.get("expiry")) == expiry_try:
                    return {
                        "symbol":      inst["tradingsymbol"],
                        "token":       inst["instrument_token"],
                        "strike":      strike,
                        "expiry":      expiry_try.isoformat(),
                        "option_type": option_type,
                        "lot_size":    int(inst.get("lot_size", config.NIFTY_LOT_SIZE)),
                    }
            logger.warning("NIFTY %s strike %s expiry %s not in NFO — trying next week", option_type, strike, expiry_try)

        # Diagnostic: log what IS available so we can debug the mismatch
        sample = [
            (inst.get("tradingsymbol"), inst.get("expiry"), type(inst.get("expiry")).__name__, inst.get("strike"))
            for inst in instruments
            if inst.get("name") == "NIFTY" and inst.get("instrument_type") == option_type
        ]
        expiries_found = sorted({_norm_expiry(inst.get("expiry")) for inst in instruments if inst.get("name") == "NIFTY"})
        logger.error("NIFTY %s contract not found. Looking for strike=%s expiry=%s or %s",
                     option_type, strike, expiry, expiry + timedelta(days=7))
        logger.error("Expiry dates found in NFO instruments for NIFTY: %s", expiries_found[:10])
        logger.error("Sample NIFTY %s instruments (first 5): %s", option_type, sample[:5])

        raise ValueError(
            f"NIFTY {option_type} strike {strike} not found for expiry {expiry} "
            f"or {expiry + timedelta(days=7)}. Check logs for available expiries."
        )

    def validate_entry(self, entry: float, stop_loss: float, zone_class: str) -> None:
        """Raise ValueError if current Nifty price has moved too far from the entry zone."""
        ltp = self.get_ltp(config.NIFTY_SYMBOL)
        tolerance = min(abs(entry - stop_loss) * 0.5, 20)   # 50% of SL distance, max 20 pts
        if zone_class == "demand" and ltp < entry - tolerance:
            raise ValueError(
                f"Nifty at {ltp:.2f} — fell {entry - ltp:.1f} pts below entry {entry:.2f}. Zone may be broken."
            )
        if zone_class == "supply" and ltp > entry + tolerance:
            raise ValueError(
                f"Nifty at {ltp:.2f} — rose {ltp - entry:.1f} pts above entry {entry:.2f}. Zone may be broken."
            )
        logger.info("Entry validation passed: LTP=%.2f entry=%.2f zone=%s", ltp, entry, zone_class)

    def get_lot_size(self) -> int:
        """Return current Nifty lot size from Kite instruments (auto-updates when NSE revises)."""
        try:
            for inst in self._get_instruments("NFO"):
                if inst.get("name") == "NIFTY" and inst.get("instrument_type") in ("CE", "PE"):
                    return int(inst.get("lot_size", config.NIFTY_LOT_SIZE))
        except Exception:
            pass
        return config.NIFTY_LOT_SIZE   # fallback to hardcoded value

    def place_options_order(self, symbol: str, action: str, quantity: int) -> str:
        """Place MIS limit order at current option LTP ± buffer. Falls back to market if LTP unavailable."""
        tx = (self._kite.TRANSACTION_TYPE_BUY
              if action == "BUY" else self._kite.TRANSACTION_TYPE_SELL)

        order_type = self._kite.ORDER_TYPE_MARKET
        price = None
        try:
            opt_ltp = self._kite.ltp([f"NFO:{symbol}"])[f"NFO:{symbol}"]["last_price"]
            if opt_ltp > 0:
                # BUY: 2 pts above LTP to ensure fill; SELL: 2 pts below
                raw = (opt_ltp + 2) if action == "BUY" else max(opt_ltp - 2, 0.05)
                price = round(round(raw / 0.05) * 0.05, 2)  # round to tick size 0.05
                order_type = self._kite.ORDER_TYPE_LIMIT
        except Exception as e:
            logger.warning("Could not fetch option LTP for %s — using market order: %s", symbol, e)

        kwargs = dict(
            variety=self._kite.VARIETY_REGULAR,
            exchange=self._kite.EXCHANGE_NFO,
            tradingsymbol=symbol,
            transaction_type=tx,
            quantity=quantity,
            product=self._kite.PRODUCT_MIS,
            order_type=order_type,
        )
        if price is not None:
            kwargs["price"] = price

        order_id = self._kite.place_order(**kwargs)
        logger.info("Placed %s %s order for %s qty=%d price=%s → order_id=%s",
                    action, order_type, symbol, quantity, price, order_id)
        return str(order_id)

    def get_order_fill_price(self, order_id: str, retries: int = 3, wait: float = 2.0) -> float:
        """Return average fill price for a completed order. Returns 0.0 if not yet filled."""
        for attempt in range(retries):
            try:
                history = self._kite.order_history(order_id)
                for h in reversed(history):
                    if h.get("status") == "COMPLETE":
                        price = float(h.get("average_price", 0))
                        if price > 0:
                            logger.info("Order %s fill price: %.2f", order_id, price)
                            return price
            except Exception as e:
                logger.warning("get_order_fill_price(%s) attempt %d: %s", order_id, attempt + 1, e)
            if attempt < retries - 1:
                time.sleep(wait)
        logger.warning("Fill price unavailable for order %s after %d attempts", order_id, retries)
        return 0.0

    def get_funds(self) -> dict:
        """Return available equity margin from Kite. Keys: cash, live_balance, used."""
        try:
            m = self._kite.margins(segment="equity")
            avail = m.get("available", {})
            used  = m.get("utilised", {})
            return {
                "cash":         avail.get("cash", 0),
                "live_balance": avail.get("live_balance", 0),
                "used":         used.get("debits", 0),
                "ok":           True,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

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
