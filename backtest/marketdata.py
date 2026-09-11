"""
Bundles every price series the replay needs and answers point-in-time queries
(VIX at a timestamp, IV-rank on a day) without look-ahead.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from .data import candles as C


class MarketData:
    def __init__(self, base_interval: str = "5minute", vix_window_days: int = 365):
        self.base_interval = base_interval
        self.vix_window_days = vix_window_days

        self.index = C.CandleStore("nifty", base_interval)

        try:
            self.fut = C.load_timeframe("nifty_fut", base_interval, base_interval)
            self.fut_idx = self.fut.set_index("date")["close"]
        except FileNotFoundError:
            self.fut = None
            self.fut_idx = None

        self._vix5 = C.load_raw("vix", base_interval).set_index("date")["close"].sort_index()
        try:
            self._vixd = C.load_raw("vix", "day")
        except FileNotFoundError:
            self._vixd = self._vix5.resample("1D").last().dropna().rename("close").reset_index()
        self._vixd = self._vixd.sort_values("date").reset_index(drop=True)
        self._iv_rank_cache: dict[date, float | None] = {}

        daily = C.load_timeframe("nifty", "day", base_interval).copy()
        daily["d"] = pd.to_datetime(daily["date"]).dt.date
        self._daily = daily.sort_values("d").reset_index(drop=True)
        self._range_cache: dict[tuple, tuple[float, float] | None] = {}

    # ── VIX ────────────────────────────────────────────────────────────
    def vix_at(self, ts: datetime) -> float | None:
        s = self._vix5.loc[:ts]
        return float(s.iloc[-1]) if len(s) else None

    def iv_rank_on(self, d: date) -> float | None:
        if d in self._iv_rank_cache:
            return self._iv_rank_cache[d]
        cutoff = pd.Timestamp(d)
        hist = self._vixd[self._vixd["date"] < cutoff].tail(self.vix_window_days)
        if len(hist) < 20:
            self._iv_rank_cache[d] = None
            return None
        cur = hist["close"].iloc[-1]
        lo, hi = hist["close"].min(), hist["close"].max()
        rank = 50.0 if hi == lo else round((cur - lo) / (hi - lo) * 100, 1)
        self._iv_rank_cache[d] = rank
        return rank

    # ── curve (higher-timeframe range) ───────────────────────────────────
    def daily_range(self, day: date, lookback_days: int = 20) -> tuple[float, float] | None:
        """High/low over the `lookback_days` trading days strictly BEFORE `day`
        (today's not-yet-complete range is never included — no lookahead)."""
        key = (day, lookback_days)
        if key in self._range_cache:
            return self._range_cache[key]
        hist = self._daily[self._daily["d"] < day].tail(lookback_days)
        result = None if len(hist) < max(5, lookback_days // 2) else \
            (float(hist["low"].min()), float(hist["high"].max()))
        self._range_cache[key] = result
        return result

    # ── futures ────────────────────────────────────────────────────────
    def fut_price_at(self, ts: datetime, index_fallback: float) -> float:
        if self.fut_idx is None:
            return index_fallback
        s = self.fut_idx.loc[:ts]
        return float(s.iloc[-1]) if len(s) else index_fallback

    # ── trading calendar ───────────────────────────────────────────────
    def trading_days(self) -> list[date]:
        return self.index.trading_days()

    def day_bars(self, d: date) -> pd.DataFrame:
        return self.index.bars_for_day(d)

    def windows(self, now: datetime, days: int = 5) -> dict[str, list]:
        return {tf: self.index.window(tf, now, days) for tf in self.index.candles}
