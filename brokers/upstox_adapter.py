"""
Upstox API v2/v3 adapter.

Auth flow (same port-5000 auto-capture pattern as Kite):
  1. broker.generate_login_url() → open in browser
  2. User logs in → Upstox redirects to redirect_uri with ?code=xxx
  3. VPS captures the code (port 5000), calls broker.generate_session(code)
  4. access_token saved to .upstox_token (gitignored)

Key differences from Kite:
  - Instrument keys: "NSE_INDEX|Nifty 50" (pipe-separated, URL-encoded in paths)
  - Historical data: v3 endpoint supports 5min/15min (v2 does not)
  - Candles returned newest-first → reversed before return
  - Options contract lookup via option chain API (not instruments dump)
  - Order instrument_token uses "NSE_FO|<numeric>" format
"""

import json
import logging
import threading
import time
from datetime import datetime, date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse, quote

import requests

import config
from brokers.base import BrokerBase, Candle, Quote

logger = logging.getLogger(__name__)

_BASE = "https://api.upstox.com"
_HFT  = "https://api-hft.upstox.com"

# Maps the shared symbol strings used across the codebase → Upstox instrument keys
_SYMBOL_MAP: dict[str, str] = {
    "NSE:NIFTY 50":   "NSE_INDEX|Nifty 50",
    "NSE:INDIA VIX":  "NSE_INDEX|India VIX",
    "NSE:NIFTY BANK": "NSE_INDEX|Nifty Bank",
}

# Maps BrokerBase interval strings → (v3 unit, v3 count)
_INTERVAL_V3: dict[str, tuple[str, str]] = {
    "5minute":  ("minutes", "5"),
    "15minute": ("minutes", "15"),
    "60minute": ("minutes", "60"),
    "day":      ("days",    "1"),
    "week":     ("weeks",   "1"),
}


class UpstoxAdapter(BrokerBase):

    def __init__(self):
        self._access_token: str | None = None
        self._token_file = config.BASE_DIR / ".upstox_token"
        self._load_token()

    # ── Token Management ───────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Accept":        "application/json",
        }

    def _load_token(self):
        if not self._token_file.exists():
            logger.warning("No Upstox token file. Call generate_session() first.")
            return
        try:
            data = json.loads(self._token_file.read_text())
            if data.get("date") == datetime.today().strftime("%Y-%m-%d"):
                self._access_token = data["access_token"]
                logger.info("Upstox access token loaded (valid for today).")
            else:
                logger.warning("Stored Upstox token is from a previous day. Re-login required.")
        except Exception as e:
            logger.error("Failed to load Upstox token: %s", e)

    def _save_token(self, access_token: str):
        self._token_file.write_text(json.dumps({
            "access_token": access_token,
            "date":         datetime.today().strftime("%Y-%m-%d"),
        }))
        self._access_token = access_token

    def generate_login_url(self) -> str:
        params = urlencode({
            "client_id":     config.UPSTOX_API_KEY,
            "redirect_uri":  config.UPSTOX_REDIRECT_URI,
            "response_type": "code",
        })
        return f"{_BASE}/v2/login/authorization/dialog?{params}"

    def capture_token_via_server(self, port: int = 5000, timeout: int = 180) -> str:
        """Same auto-capture pattern as Kite — start HTTP server, wait for redirect."""
        captured: dict = {}

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                params = parse_qs(urlparse(self.path).query)
                if "code" in params:
                    captured["code"] = params["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h2>Upstox token captured. You can close this tab.</h2>")
                threading.Thread(target=self.server.shutdown, daemon=True).start()

            def log_message(self, *args):
                pass

        server = HTTPServer(("0.0.0.0", port), _Handler)
        server.timeout = timeout
        logger.info("Waiting for Upstox redirect on port %d (timeout: %ds)...", port, timeout)
        server.serve_forever()

        code = captured.get("code", "")
        if not code:
            raise TimeoutError("No auth code received within timeout window.")
        return self.generate_session(code)

    def generate_session(self, auth_code: str) -> str:
        """Exchange Upstox auth code for access_token."""
        resp = requests.post(
            f"{_BASE}/v2/login/authorization/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "code":          auth_code,
                "client_id":     config.UPSTOX_API_KEY,
                "client_secret": config.UPSTOX_API_SECRET,
                "redirect_uri":  config.UPSTOX_REDIRECT_URI,
                "grant_type":    "authorization_code",
            },
            timeout=10,
        )
        resp.raise_for_status()
        access_token = resp.json()["access_token"]
        self._save_token(access_token)
        logger.info("Upstox session established and token saved.")
        return access_token

    # ── BrokerBase interface ───────────────────────────────────────────────

    def _resolve(self, symbol: str) -> str:
        """Map shared symbol string → Upstox instrument key."""
        if symbol in _SYMBOL_MAP:
            return _SYMBOL_MAP[symbol]
        # For options instrument keys already in Upstox format (e.g. "NSE_FO|37668")
        if "|" in symbol:
            return symbol
        raise ValueError(
            f"Unknown symbol '{symbol}' for Upstox. "
            "Add to _SYMBOL_MAP or use the full NSE_FO|<token> key."
        )

    def get_ltp(self, symbol: str) -> float:
        key = self._resolve(symbol)
        resp = requests.get(
            f"{_BASE}/v2/market-quote/ltp",
            headers=self._headers(),
            params={"instrument_key": key},
            timeout=10,
        )
        resp.raise_for_status()
        return list(resp.json()["data"].values())[0]["last_price"]

    def get_options_ltp(self, instrument_key: str) -> float | None:
        """LTP for an options contract. instrument_key is "NSE_FO|<token>"."""
        try:
            resp = requests.get(
                f"{_BASE}/v2/market-quote/ltp",
                headers=self._headers(),
                params={"instrument_key": instrument_key},
                timeout=10,
            )
            resp.raise_for_status()
            return list(resp.json()["data"].values())[0]["last_price"]
        except Exception as e:
            logger.debug("Options LTP failed for %s: %s", instrument_key, e)
            return None

    def get_quote(self, symbol: str) -> Quote:
        key = self._resolve(symbol)
        resp = requests.get(
            f"{_BASE}/v2/market-quote/quotes",
            headers=self._headers(),
            params={"instrument_key": key},
            timeout=10,
        )
        resp.raise_for_status()
        q = list(resp.json()["data"].values())[0]
        ohlc = q.get("ohlc", {})
        return Quote(
            ltp=q["last_price"],
            open=ohlc.get("open", 0),
            high=ohlc.get("high", 0),
            low=ohlc.get("low", 0),
            close=ohlc.get("close", 0),
        )

    def get_historical(self, symbol: str, interval: str, days: int) -> list[Candle]:
        """
        Uses Upstox v3 historical candle API.
        v3 supports 5min/15min/60min/day; v2 only supports 30min/day/week/month.
        Candles are returned newest-first by Upstox — reversed before returning.
        """
        key  = self._resolve(symbol)
        unit, count = _INTERVAL_V3.get(interval, ("days", "1"))
        to_dt   = datetime.now()
        from_dt = to_dt - timedelta(days=days)
        to_str   = to_dt.strftime("%Y-%m-%d")
        from_str = from_dt.strftime("%Y-%m-%d")

        # Instrument key contains | and spaces → must be URL-encoded in path
        key_enc = quote(key, safe="")
        url = f"{_BASE}/v3/historical-candle/{key_enc}/{unit}/{count}/{to_str}/{from_str}"

        resp = requests.get(url, headers=self._headers(), timeout=10)
        resp.raise_for_status()
        raw_candles = resp.json()["data"]["candles"]  # newest-first

        result = []
        for c in reversed(raw_candles):   # oldest-first to match Kite convention
            # Format: [timestamp, open, high, low, close, volume, OI]
            ts = datetime.fromisoformat(c[0][:19])   # strip timezone offset
            result.append(Candle(
                timestamp=ts,
                open=float(c[1]),
                high=float(c[2]),
                low=float(c[3]),
                close=float(c[4]),
                volume=int(c[5]),
            ))
        return result

    def is_connected(self) -> bool:
        if not self._access_token:
            return False
        try:
            resp = requests.get(
                f"{_BASE}/v2/user/profile",
                headers=self._headers(),
                timeout=10,
            )
            return resp.status_code == 200
        except Exception:
            return False

    # ── Options contract lookup ────────────────────────────────────────────

    def get_options_contract(self, ltp: float, direction: str) -> dict:
        """Find 1-strike ITM Nifty weekly options via Upstox option chain API."""
        option_type = "CE" if direction == "demand" else "PE"
        atm    = round(ltp / 50) * 50
        strike = (atm - 50) if option_type == "CE" else (atm + 50)

        today      = date.today()
        days_ahead = (1 - today.weekday()) % 7     # next Tuesday
        if days_ahead <= 1:
            days_ahead += 7                         # skip expiry within 2 days
        expiry = today + timedelta(days=days_ahead)

        resp = requests.get(
            f"{_BASE}/v2/option/chain",
            headers=self._headers(),
            params={
                "instrument_key": "NSE_INDEX|Nifty 50",
                "expiry_date":    expiry.isoformat(),
            },
            timeout=10,
        )
        resp.raise_for_status()
        chain = resp.json().get("data", [])

        key_field = "call_options" if option_type == "CE" else "put_options"
        for item in chain:
            if item.get("strike_price") != strike:
                continue
            opt = item.get(key_field, {})
            instrument_key = opt.get("instrument_key", "")
            market         = opt.get("market_data", {})
            tradingsymbol  = market.get("tradingsymbol", instrument_key)
            lot_size       = market.get("lot_size", config.NIFTY_LOT_SIZE)
            return {
                "symbol":      instrument_key,   # used for place_options_order + options_symbol in DB
                "token":       instrument_key,
                "strike":      strike,
                "expiry":      expiry.isoformat(),
                "option_type": option_type,
                "lot_size":    int(lot_size),
                "tradingsymbol": tradingsymbol,  # for display / logging
            }

        raise ValueError(
            f"NIFTY {option_type} strike {strike} not found in Upstox option chain "
            f"for expiry {expiry}."
        )

    # ── Order placement ────────────────────────────────────────────────────

    def place_options_order(self, symbol: str, action: str, quantity: int) -> str:
        """
        Place MIS limit order via Upstox HFT endpoint.
        symbol = instrument_key from get_options_contract() e.g. "NSE_FO|37668"
        """
        # Fetch current LTP to set limit price
        ltp = self.get_options_ltp(symbol)
        if ltp and ltp > 0:
            raw   = (ltp + 2) if action == "BUY" else max(ltp - 2, 0.05)
            price = round(round(raw / 0.05) * 0.05, 2)
            order_type = "LIMIT"
        else:
            price      = 0
            order_type = "MARKET"
            logger.warning("Could not fetch LTP for %s — using MARKET order", symbol)

        body: dict = {
            "instrument_token": symbol,
            "transaction_type": action,
            "product":          "I",         # MIS / Intraday
            "order_type":       order_type,
            "quantity":         quantity,
            "validity":         "DAY",
            "is_amo":           False,
        }
        if order_type == "LIMIT":
            body["price"] = price

        resp = requests.post(
            f"{_HFT}/v2/order/place",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=body,
            timeout=10,
        )
        resp.raise_for_status()
        order_id = str(resp.json()["data"]["order_id"])
        logger.info("Placed %s %s order for %s qty=%d price=%s → order_id=%s",
                    action, order_type, symbol, quantity, price, order_id)
        return order_id

    def get_order_fill_price(self, order_id: str, retries: int = 3, wait: float = 2.0) -> float:
        """Return average fill price for a completed order. Returns 0.0 if not yet filled."""
        for attempt in range(retries):
            try:
                resp = requests.get(
                    f"{_BASE}/v2/order/history",
                    headers=self._headers(),
                    params={"order_id": order_id},
                    timeout=10,
                )
                resp.raise_for_status()
                for h in reversed(resp.json().get("data", [])):
                    if h.get("status") == "complete":
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

    def validate_entry(self, entry: float, stop_loss: float, zone_class: str) -> None:
        """Raise ValueError if current Nifty price has moved too far from the entry zone."""
        ltp       = self.get_ltp("NSE:NIFTY 50")
        tolerance = min(abs(entry - stop_loss) * 0.5, 20)
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
        """Return current Nifty lot size. Upstox option chain includes lot_size in market_data."""
        return config.NIFTY_LOT_SIZE

    def prefetch_instruments(self) -> None:
        """No-op for Upstox — instrument lookup is done via option chain API at trade time."""
        pass
