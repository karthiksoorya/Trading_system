"""
Load cached parquet candles and serve them to the replay engine as the same
`brokers.base.Candle` objects the production engine consumes.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import datetime
from pathlib import Path

import pandas as pd

from brokers.base import Candle

from . import CACHE_DIR

# pandas resample rule per Kite interval label
_RULE = {"5minute": "5min", "15minute": "15min", "60minute": "60min", "day": "1D"}
# NSE cash session
SESSION_START = "09:15"
SESSION_END = "15:30"


def _path(name: str, interval: str) -> Path:
    return CACHE_DIR / f"{name}_{interval}.parquet"


def available() -> list[str]:
    return sorted(p.name for p in CACHE_DIR.glob("*.parquet"))


def to_naive_ist(s: pd.Series) -> pd.Series:
    """Kite candles come back tz-aware (+05:30); trade timestamps we build are
    tz-naive. Normalise everything to tz-naive IST wall-clock so comparisons work."""
    s = pd.to_datetime(s)
    if getattr(s.dt, "tz", None) is not None:
        s = s.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    return s


def load_raw(name: str, interval: str) -> pd.DataFrame:
    p = _path(name, interval)
    if not p.exists():
        raise FileNotFoundError(
            f"{p.name} not in cache. Run:  python -m backtest.data.fetch\n"
            f"  (cache currently has: {', '.join(available()) or 'nothing'})"
        )
    df = pd.read_parquet(p)
    df["date"] = to_naive_ist(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def _resample(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    rule = _RULE[interval]
    idx = df.set_index("date")
    out = idx.resample(rule, label="left", closed="left", origin="09:15").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"),
    ).dropna(subset=["open"])
    if interval != "day":
        out = out.between_time(SESSION_START, SESSION_END)
    return out.reset_index()


def load_timeframe(name: str, interval: str, base_interval: str = "5minute") -> pd.DataFrame:
    """Return candles at `interval`, resampling from `base_interval` if no direct file."""
    if _path(name, interval).exists():
        return load_raw(name, interval)
    return _resample(load_raw(name, base_interval), interval)


def to_candles(df: pd.DataFrame) -> list[Candle]:
    return [
        Candle(timestamp=r.date.to_pydatetime(), open=float(r.open), high=float(r.high),
               low=float(r.low), close=float(r.close), volume=int(r.volume or 0))
        for r in df.itertuples(index=False)
    ]


class CandleStore:
    """
    Holds the full history for every timeframe and hands the strategy a trailing
    window ending at a given timestamp — the offline equivalent of
    `broker.get_historical(symbol, tf, days=5)`.
    """

    def __init__(self, name: str = "nifty", base_interval: str = "5minute",
                 timeframes: tuple[str, ...] = ("5minute", "15minute", "60minute")):
        self.base_interval = base_interval
        self.frames: dict[str, pd.DataFrame] = {}
        self.candles: dict[str, list[Candle]] = {}
        self._ts: dict[str, list[float]] = {}          # epoch-seconds, sorted, for bisect
        for tf in timeframes:
            df = load_timeframe(name, tf, base_interval)
            self.frames[tf] = df
            self.candles[tf] = to_candles(df)
            self._ts[tf] = [c.timestamp.timestamp() for c in self.candles[tf]]

    def trading_days(self) -> list:
        d = self.frames[self.base_interval]["date"]
        return sorted(pd.Index(d.dt.date).unique().tolist())

    def window(self, tf: str, now: datetime, days: int = 5) -> list[Candle]:
        """Completed candles for `tf` within the `days` calendar days before `now`.

        A bar timestamped at its open is 'known' only once its close time <= now,
        i.e. open_ts <= now - tf_minutes*60.
        """
        ts = self._ts[tf]
        n = now.timestamp()
        lo_val = n - days * 86400
        hi_val = n - _TF_MINUTES.get(tf, 5) * 60
        lo = bisect_left(ts, lo_val)
        hi = bisect_right(ts, hi_val)
        return self.candles[tf][lo:hi]

    def bars_for_day(self, day) -> pd.DataFrame:
        df = self.frames[self.base_interval]
        return df[df["date"].dt.date == day].reset_index(drop=True)


_TF_MINUTES = {"5minute": 5, "15minute": 15, "60minute": 60}
