"""
Demand/Supply strategy for the backtest.

This is a faithful port of `scheduler._scan_core()` — the zone detection,
booster scoring, confluence and signal construction all call the *production*
`engine/` functions unchanged. Only the orchestration around them is
re-implemented, because the live version is welded to datetime.now(), the DB,
the broker and Telegram.

Every filter from _scan_core is reproduced and individually toggleable via
`DSParams`, so the research phase can measure what each one is worth.

Deliberately NOT ported (live-only, no effect on which zones qualify):
  - Telegram notification / approval
  - AUTO_FIRST / FULLY_AUTOMATED order placement
  - position sizing 'error' skip (backtest always trades 1 lot)
  - DB persistence
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time

import config
from engine.confluence import check_confluence
from engine.signals import Signal, generate_signal
from engine.zones import Zone, detect_zones, update_zone_state

TF_ORDER = ["5minute", "15minute", "60minute"]


@dataclass
class DSParams:
    entry_tf: str = "5minute"
    active_classes: tuple[str, ...] = ("demand", "supply")
    min_booster_score: float = 8.0
    min_confluence: int = 2
    zone_approach_points: float = 50.0
    sl_buffer_points: float = 5.0
    min_risk_points: float = 10.0          # skip signals whose |entry-SL| is below this (noise floor)
    max_risk_points: float = 0.0           # skip signals whose |entry-SL| is above this (0 = off)
    signal_expiry_minutes: int = 45       # a limit entry order lives this long, then cancels
    scan_start: time = time(9, 15)
    scan_end: time = time(13, 0)
    time_exit_hour: int = 13
    max_trades_per_day: int = 4

    # toggleable filters
    trend_filter: bool = True             # 60min trend must align with zone class
    ce_after_11: bool = True              # skip demand/CE before 11:00
    vix_direction_filter: bool = True     # skip demand/CE when VIX rising > 5% off day low
    vix_max: float = 20.0
    iv_rank_max: float = 60.0
    dedupe_same_zone: bool = True

    disabled_zone_types: tuple[str, ...] = ()

    def label(self) -> str:
        on = [k for k in ("trend_filter", "ce_after_11", "vix_direction_filter")
              if getattr(self, k)]
        risk = ""
        if self.min_risk_points or self.max_risk_points:
            risk = f" risk={self.min_risk_points or 0:g}-{self.max_risk_points or '∞'}"
        return f"DS[{self.entry_tf} conf>={self.min_confluence} score>={self.min_booster_score} " \
               f"approach={self.zone_approach_points}{risk} filters={'+'.join(on) or 'none'}]"


@dataclass
class DayState:
    vix_low: float = 0.0
    signaled: set = field(default_factory=set)
    trades_taken: int = 0


class DemandSupplyStrategy:
    def __init__(self, params: DSParams | None = None):
        self.p = params or DSParams()
        # generate_signal() reads these module globals directly
        config.SL_BUFFER_POINTS = self.p.sl_buffer_points
        config.MIN_BOOSTER_SCORE = self.p.min_booster_score
        self._day: date | None = None
        self.state = DayState()

    # ── per-day reset ────────────────────────────────────────────────────
    def new_day(self, d: date):
        self._day = d
        self.state = DayState()

    # ── main entry point — mirrors _scan_core ────────────────────────────
    def evaluate(self, now: datetime, windows: dict[str, list],
                 ltp: float, vix: float | None, iv_rank: float | None) -> list[Signal]:
        p = self.p
        t = now.time()
        if not (p.scan_start <= t <= p.scan_end):
            return []
        if p.time_exit_hour and now.hour >= p.time_exit_hour:
            return []
        if self.state.trades_taken >= p.max_trades_per_day:
            return []

        # VIX level + running-low direction (per day)
        vix_rising = False
        if vix is not None:
            if self.state.vix_low == 0.0:
                self.state.vix_low = vix
            elif vix < self.state.vix_low:
                self.state.vix_low = vix
            vix_rising = vix > self.state.vix_low * 1.05
            if vix > p.vix_max:
                return []

        # NOTE ON INDEXING vs _scan_core:
        # Live `broker.get_historical` returns the still-forming bar as candles[-1],
        # so _scan_core uses candles[:-1] for zones, candles[-4:-1] for "last 3",
        # c60[-2] for "last complete 60m bar". The backtest `windows` contain ONLY
        # completed bars, so every one of those indices shifts by one here.
        # Step 1 — valid zones per timeframe
        valid: dict[str, list[Zone]] = {}
        for tf in TF_ORDER:
            candles = windows.get(tf, [])
            if len(candles) < 3:
                valid[tf] = []
                continue
            zones = detect_zones(candles, tf)
            live_slice = candles[-20:]
            good = []
            for z in zones:
                update_zone_state(z, live_slice)
                if z.is_valid:
                    good.append(z)
            valid[tf] = good

        # Step 2 — signals from the entry TF only
        out: list[Signal] = []
        i = TF_ORDER.index(p.entry_tf)
        tf = p.entry_tf
        zones = valid.get(tf, [])
        candles = windows.get(tf, [])
        if not zones or not candles:
            return out
        higher = {htf: valid.get(htf, []) for htf in TF_ORDER[i + 1:]}

        for zone in zones:
            if zone.zone_class not in p.active_classes:
                continue
            if p.vix_direction_filter and zone.zone_class == "demand" and vix_rising:
                continue
            if p.ce_after_11 and zone.zone_class == "demand" and now.hour < 11:
                continue
            if iv_rank is not None and iv_rank > p.iv_rank_max:
                continue
            key = (zone.zone_class, zone.zone_type, tf, round(zone.proximal))
            if p.dedupe_same_zone and key in self.state.signaled:
                continue
            if zone.zone_type in p.disabled_zone_types:
                continue

            # Filter 1 — price proximity
            if abs(ltp - zone.proximal) > p.zone_approach_points:
                continue

            # Filter 2 — zone not violated in the last 3 completed bars
            recent_3 = candles[-3:]
            if zone.zone_class == "demand":
                if any(c.close < zone.distal for c in recent_3):
                    continue
            else:
                if any(c.close > zone.distal for c in recent_3):
                    continue

            # Filter 3 — 60min trend alignment
            if p.trend_filter:
                c60 = windows.get("60minute", [])
                if len(c60) >= 5 and p.entry_tf != "60minute":
                    now_c, prev_c = c60[-1].close, c60[-5].close
                    if now_c > prev_c * 1.002:
                        trend = "up"
                    elif now_c < prev_c * 0.998:
                        trend = "down"
                    else:
                        trend = "neutral"
                    if zone.zone_class == "demand" and trend == "down":
                        continue
                    if zone.zone_class == "supply" and trend == "up":
                        continue

            confluence = check_confluence(zone, higher)
            opposing_class = "supply" if zone.zone_class == "demand" else "demand"
            opposing = [z for z in zones if z.zone_class == opposing_class and z.is_valid]

            signal = generate_signal(
                zone=zone, ltp=ltp, prev_candles=candles[-10:],
                confluence=confluence, opposing_zones=opposing,
            )
            if signal is None:
                continue
            if signal.confluence.count < p.min_confluence:
                continue

            # Zone-risk band. A degenerate zone (tiny doji base) gives risk of a
            # couple of points → a 2R target only ~5 pts away that any bar clips
            # instantly; the "win" can't cover costs and options can't profit on
            # it. A huge zone gives a target 150+ pts away that rarely fills.
            risk = abs(signal.entry - signal.stop_loss)
            if p.min_risk_points and risk < p.min_risk_points:
                continue
            if p.max_risk_points and risk > p.max_risk_points:
                continue

            self.state.signaled.add(key)
            out.append(signal)

        return out

    def mark_trade_taken(self):
        self.state.trades_taken += 1
