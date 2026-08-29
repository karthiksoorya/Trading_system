"""
Pull historical candles from Kite Connect and cache them locally.

Run:
    python -m backtest.data.fetch --years 3
    python -m backtest.data.fetch --check          # just test API access

Needs a *valid* Kite access token in ./.kite_token (log in via the dashboard
first — the token is only good for the current trading day). Historical data
also requires the "Historical data" add-on to be enabled on the Kite Connect
app; if it isn't, Kite raises PermissionException and this script says so.

Output: backtest/data/cache/<name>_<interval>.parquet with columns
    date, open, high, low, close, volume
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from . import CACHE_DIR

BASE_DIR = Path(__file__).resolve().parents[2]
TOKEN_FILE = BASE_DIR / ".kite_token"

# Well-known Kite instrument tokens (stable, exchange-assigned).
TOKENS = {
    "nifty":   256265,     # NSE:NIFTY 50 index
    "vix":     264969,     # NSE:INDIA VIX
}

# Kite's per-request span limit by interval (calendar days).
MAX_SPAN_DAYS = {
    "minute": 60, "3minute": 100, "5minute": 100, "10minute": 100,
    "15minute": 200, "30minute": 200, "60minute": 400, "day": 2000,
}

# Kite historical API: ~3 req/s. Stay well under.
REQUEST_PAUSE = 0.4


def _load_kite():
    from kiteconnect import KiteConnect
    import config

    if not TOKEN_FILE.exists():
        sys.exit(f"No token file at {TOKEN_FILE}. Log in to Kite via the dashboard first.")
    tok = json.loads(TOKEN_FILE.read_text())
    access_token = tok.get("access_token")
    saved = tok.get("date")
    today = datetime.today().strftime("%Y-%m-%d")
    if saved != today:
        print(f"⚠  Token in .kite_token is dated {saved}, today is {today}.")
        print("   Kite tokens expire daily — if the calls below fail with TokenException,")
        print("   log in again via the dashboard and re-run.\n")
    kite = KiteConnect(api_key=config.KITE_API_KEY, timeout=15)
    kite.set_access_token(access_token)
    return kite


def _resolve_futures_token(kite, continuous: bool):
    """Front-month NIFTY futures instrument token from the NFO dump."""
    insts = kite.instruments("NFO")
    futs = [i for i in insts if i.get("name") == "NIFTY" and i.get("instrument_type") == "FUT"]
    if not futs:
        return None, None
    futs.sort(key=lambda i: i["expiry"])
    front = futs[0]
    return front["instrument_token"], front["tradingsymbol"]


def _fetch_range(kite, token: int, interval: str, start: date, end: date,
                 continuous: bool = False) -> pd.DataFrame:
    span = MAX_SPAN_DAYS.get(interval, 60)
    frames = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=span - 1), end)
        for attempt in range(4):
            try:
                raw = kite.historical_data(
                    instrument_token=token,
                    from_date=cur.strftime("%Y-%m-%d"),
                    to_date=chunk_end.strftime("%Y-%m-%d"),
                    interval=interval,
                    continuous=continuous,
                    oi=False,
                )
                if raw:
                    frames.append(pd.DataFrame(raw))
                break
            except Exception as e:
                name = type(e).__name__
                if name in ("PermissionException", "TokenException", "InputException"):
                    raise                       # not transient — don't retry
                wait = 2 ** attempt
                print(f"    {name} on {cur}..{chunk_end}, retry in {wait}s: {e}")
                time.sleep(wait)
        else:
            print(f"    giving up on {cur}..{chunk_end}")
        time.sleep(REQUEST_PAUSE)
        cur = chunk_end + timedelta(days=1)

    return _dedup(frames)


def _dedup(frames) -> pd.DataFrame:

    if not frames:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    if getattr(df["date"].dt, "tz", None) is not None:          # store tz-naive IST
        df["date"] = df["date"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    df = df.drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
    return df[["date", "open", "high", "low", "close", "volume"]]


def _save(df: pd.DataFrame, name: str, interval: str) -> Path:
    path = CACHE_DIR / f"{name}_{interval}.parquet"
    df.to_parquet(path, index=False)
    span = f"{df['date'].min()} -> {df['date'].max()}" if len(df) else "empty"
    print(f"  saved {len(df):>7,} rows  {name}_{interval}  ({span})")
    return path


def check_access(kite) -> bool:
    """One tiny historical request to see whether the add-on is enabled."""
    try:
        kite.historical_data(
            instrument_token=TOKENS["nifty"],
            from_date=(date.today() - timedelta(days=5)).strftime("%Y-%m-%d"),
            to_date=date.today().strftime("%Y-%m-%d"),
            interval="day", continuous=False, oi=False,
        )
        print("[ok] Historical data API is accessible.")
        return True
    except Exception as e:
        name = type(e).__name__
        if name == "PermissionException":
            print("[X] PermissionException - this Kite Connect app cannot pull historical data.")
            print("    Check the app's subscription / enable the historical-data entitlement")
            print("    at https://developers.kite.trade , or point the loader at another source.")
        elif name == "TokenException":
            print("[X] TokenException - the access token is stale. Log in again,")
            print("    then re-run this command.")
        else:
            print(f"[X] {name}: {e}")
        return False


def main():
    ap = argparse.ArgumentParser(description="Fetch Kite historical candles for the backtest.")
    ap.add_argument("--years", type=float, default=3.0, help="how far back to fetch (default 3)")
    ap.add_argument("--interval", default="5minute", help="candle interval for index/futures")
    ap.add_argument("--check", action="store_true", help="only test API access, fetch nothing")
    ap.add_argument("--no-futures", action="store_true", help="skip the futures series")
    args = ap.parse_args()

    kite = _load_kite()

    if args.check:
        sys.exit(0 if check_access(kite) else 1)

    if not check_access(kite):
        sys.exit(1)

    flag = CACHE_DIR / "_SYNTHETIC.flag"
    if flag.exists():
        flag.unlink()
        print("(removed stale _SYNTHETIC.flag — replacing toy data with real data)\n")

    end = date.today()
    start = end - timedelta(days=int(args.years * 365))
    print(f"\nFetching {start} -> {end}\n")

    # Index — entry timeframe + the higher TFs the strategy needs for confluence/trend
    for interval in dict.fromkeys([args.interval, "15minute", "60minute"]):
        df = _fetch_range(kite, TOKENS["nifty"], interval, start, end)
        _save(df, "nifty", interval)

    # VIX — 5min for intraday IV, day for IV-rank 52-week range
    df = _fetch_range(kite, TOKENS["vix"], args.interval, start, end)
    _save(df, "vix", args.interval)
    df = _fetch_range(kite, TOKENS["vix"], "day", start, end)
    _save(df, "vix", "day")

    # Futures — Kite's continuous stitching doesn't work for intraday NIFTY FUT,
    # and expired-contract tokens drop out of the instruments dump, so we can only
    # get the current front month (~weeks of history). That's fine: for an intraday
    # trade the basis is ~constant, so futures P&L ≈ index-points P&L. The backtest
    # falls back to the index as the futures proxy wherever this file doesn't reach.
    if not args.no_futures:
        tok, sym = _resolve_futures_token(kite, continuous=False)
        if tok:
            print(f"  futures front-month: {sym} (token {tok}) — non-continuous")
            try:
                df = _fetch_range(kite, tok, args.interval, start, end, continuous=False)
                _save(df, "nifty_fut", args.interval)
                print("  (older history uses the index as the futures proxy — basis ~const intraday)")
            except Exception as e:
                print(f"  futures fetch failed ({type(e).__name__}) — index proxy will be used")
        else:
            print("  no NIFTY futures instrument found — index proxy will be used")

    print("\nDone. Cache dir:", CACHE_DIR)


if __name__ == "__main__":
    main()
