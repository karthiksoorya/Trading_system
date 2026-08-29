"""
Event-driven replay.

For each trading day it walks the 5-minute bars, asks the strategy for signals
on completed bars, fills an entry, then manages the position purely on the index
path (target / stop / time-exit / EOD).

Entry fill model (`entry_mode`):
  - "limit"  (default): a limit order rests at the zone proximal — exactly the
    Signal.entry the strategy computes SL and target from. It fills only when a
    later bar's range spans the proximal, within SIGNAL_EXPIRY_MINUTES; if price
    never comes back to the zone the order cancels and NO trade is taken. This is
    what a demand/supply zone entry actually is, and it keeps the fill price
    consistent with the R the 2R target is built on.
  - "market": fill at the next bar's open. Faithful to the current *live*
    behaviour (a market order the moment a signal is approved, fired whenever LTP
    is within ZONE_APPROACH_POINTS of the proximal). Kept for comparison — it
    enters many trades the limit model never would, at a worse price.

The exact same entry and exit are then priced across every instrument in
`INSTRUMENTS`, so any P&L difference is the instrument alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import pandas as pd

from .costs import CostConfig, future_roundtrip, option_roundtrip
from .marketdata import MarketData
from .metrics import Trade
from .option_model import (DEFAULT_PARAMS, ModelParams, OptionContract, choose_strike,
                           fill_price, monthly_expiry, next_weekly_expiry, price_at)
from .strategy_ds import DemandSupplyStrategy, DSParams

LOT = 65
EOD = time(15, 20)


@dataclass
class InstrumentSpec:
    name: str
    kind: str                 # 'future' | 'option'
    itm_steps: int = 1
    expiry: str = "weekly"    # 'weekly' | 'monthly'
    slippage_pts: float = 0.0


INSTRUMENTS = [
    InstrumentSpec("futures",       "future", slippage_pts=0.5),
    InstrumentSpec("opt_atm",       "option", itm_steps=0, slippage_pts=1.5),
    InstrumentSpec("opt_itm1",      "option", itm_steps=1, slippage_pts=1.5),   # current live
    InstrumentSpec("opt_itm3",      "option", itm_steps=3, slippage_pts=2.0),
    InstrumentSpec("opt_itm1_mono", "option", itm_steps=1, expiry="monthly", slippage_pts=2.0),
]


@dataclass
class OpenPosition:
    signal_id: int | None
    direction: str
    entry_time: datetime
    entry_index: float
    stop_loss: float
    target: float
    booster: float


def _pick_signal(signals, ltp: float):
    """Live logs them all and trades the first/best. Rank by booster score, then
    confluence, then the zone whose proximal is closest to current price."""
    return sorted(signals, key=lambda s: (
        -s.boosters.total, -s.confluence.count, abs(ltp - s.zone.proximal),
    ))[0]


def _entry_fill(sig, bars: pd.DataFrame, decision_bi: int, decision_time: datetime,
                mode: str, params: DSParams):
    """Resolve an entry order to (fill_bi, fill_time, fill_index), or None if it
    never fills.

    market : next bar's open — matches the live 'market order on approval'.
    limit  : rest at sig.entry (the zone proximal); fill the first later bar whose
             range spans it, within SIGNAL_EXPIRY_MINUTES and before the time-exit
             cutoff. If price never returns to the zone, the order cancels — None.
    """
    n = len(bars)
    if decision_bi + 1 >= n:
        return None

    if mode == "market":
        nxt = bars.iloc[decision_bi + 1]
        return decision_bi + 1, nxt.date.to_pydatetime(), float(nxt.open)

    if mode != "limit":
        raise ValueError(f"unknown entry_mode {mode!r}")

    proximal = sig.entry
    deadline = decision_time + timedelta(minutes=params.signal_expiry_minutes)
    for j in range(decision_bi + 1, n):
        b = bars.iloc[j]
        bt = b.date.to_pydatetime()
        if bt > deadline:
            return None
        if params.time_exit_hour and bt.time() >= time(params.time_exit_hour, 0):
            return None
        if float(b.low) <= proximal <= float(b.high):
            return j, bt, proximal
    return None


def _exit_on_path(pos: OpenPosition, bars: pd.DataFrame, fill_bi: int, time_exit_hour: int):
    """Walk bars from the fill bar onward; return (exit_time, exit_index, reason).

    On the fill bar itself only an adverse wick counts (worst case) — we can't
    assume we also caught the favourable extreme after filling mid-bar.
    """
    fb = bars.iloc[fill_bi]
    fbt = fb.date.to_pydatetime()
    if pos.direction == "demand":
        if float(fb.low) <= pos.stop_loss:
            return fbt, pos.stop_loss, "stoploss"
    else:
        if float(fb.high) >= pos.stop_loss:
            return fbt, pos.stop_loss, "stoploss"

    for b in bars.iloc[fill_bi + 1:].itertuples(index=False):
        bt = b.date.to_pydatetime()
        if time_exit_hour and bt.time() >= time(time_exit_hour, 0):
            return bt, float(b.open), "time_exit"
        if bt.time() >= EOD:
            return bt, float(b.open), "eod"
        hi, lo = float(b.high), float(b.low)
        if pos.direction == "demand":
            if lo <= pos.stop_loss:
                return bt, pos.stop_loss, "stoploss"      # SL first — assume worst case
            if hi >= pos.target:
                return bt, pos.target, "target"
        else:
            if hi >= pos.stop_loss:
                return bt, pos.stop_loss, "stoploss"
            if lo <= pos.target:
                return bt, pos.target, "target"
    # never exited — close at last bar
    last = bars.iloc[-1]
    return last.date.to_pydatetime(), float(last.close), "eod"


def _price_trade(spec: InstrumentSpec, pos: OpenPosition, exit_time: datetime,
                 exit_index: float, md: MarketData, mp: ModelParams,
                 cost_cfg: CostConfig) -> tuple[float, float, float]:
    """Return (gross_pnl, costs, net_pnl) in rupees for one instrument."""
    dir_sign = 1.0 if pos.direction == "demand" else -1.0

    if spec.kind == "future":
        fe = md.fut_price_at(pos.entry_time, pos.entry_index)
        fx = md.fut_price_at(exit_time, exit_index)
        gross = dir_sign * (fx - fe) * LOT
        costs = future_roundtrip(fe, fx, LOT, cost_cfg) + spec.slippage_pts * LOT
        return gross, costs, gross - costs

    # option
    opt_type = "CE" if pos.direction == "demand" else "PE"
    strike = choose_strike(pos.entry_index, pos.direction, spec.itm_steps)
    exp = (monthly_expiry(pos.entry_time.date()) if spec.expiry == "monthly"
           else next_weekly_expiry(pos.entry_time.date()))
    contract = OptionContract(strike=strike, expiry=exp, option_type=opt_type, lot_size=LOT)

    v_entry = md.vix_at(pos.entry_time) or 14.0
    v_exit = md.vix_at(exit_time) or v_entry
    entry_mid = price_at(contract, pos.entry_index, v_entry, pos.entry_time, params=mp)
    exit_mid = price_at(contract, exit_index, v_exit, exit_time,
                        entry_spot=pos.entry_index, params=mp)
    # cross the spread: pay above mid on entry, below on exit. spec.slippage_pts
    # widens it further for deeper-ITM / monthly contracts that quote wider.
    extra = spec.slippage_pts
    entry_prem = fill_price(entry_mid, "buy", mp) + extra
    exit_prem = max(0.05, fill_price(exit_mid, "sell", mp) - extra)

    gross = (exit_prem - entry_prem) * LOT
    costs = option_roundtrip(entry_prem, exit_prem, LOT, cost_cfg)
    return gross, costs, gross - costs


def run_backtest(params: DSParams | None = None,
                 model_params: ModelParams = DEFAULT_PARAMS,
                 cost_cfg: CostConfig = CostConfig(),
                 instruments: list[InstrumentSpec] = INSTRUMENTS,
                 start: date | None = None, end: date | None = None,
                 md: MarketData | None = None,
                 entry_mode: str = "limit",
                 progress: bool = True) -> list[Trade]:
    md = md or MarketData()
    params = params or DSParams()
    strat = DemandSupplyStrategy(params)

    days = md.trading_days()
    if start:
        days = [d for d in days if d >= start]
    if end:
        days = [d for d in days if d <= end]

    trades: list[Trade] = []
    for n, day in enumerate(days):
        if progress and n % 25 == 0:
            print(f"  {day}  ({n}/{len(days)})  trades so far: {len(trades)//max(len(instruments),1)}")
        strat.new_day(day)
        bars = md.day_bars(day)
        if len(bars) < 10:
            continue

        last_exit: datetime | None = None
        for bi in range(len(bars) - 1):
            b = bars.iloc[bi]
            now = b.date.to_pydatetime() + timedelta(minutes=5)   # bar-close = decision time
            if now.time() < params.scan_start or now.hour >= params.time_exit_hour:
                continue
            if last_exit is not None and now <= last_exit:
                continue                                          # still inside previous trade

            windows = md.windows(now, days=5)
            ltp = float(b.close)
            vix = md.vix_at(now)
            iv_rank = md.iv_rank_on(day)
            signals = strat.evaluate(now, windows, ltp, vix, iv_rank)
            if not signals:
                continue

            sig = _pick_signal(signals, ltp)
            fill = _entry_fill(sig, bars, bi, now, entry_mode, params)
            if fill is None:
                continue                      # limit order never touched — no trade
            fill_bi, entry_time, entry_index = fill
            pos = OpenPosition(
                signal_id=None, direction=sig.zone.zone_class,
                entry_time=entry_time, entry_index=entry_index,
                stop_loss=sig.stop_loss, target=sig.intraday_target,
                booster=sig.boosters.total,
            )
            strat.mark_trade_taken()

            exit_time, exit_index, reason = _exit_on_path(pos, bars, fill_bi, params.time_exit_hour)
            dir_sign = 1.0 if pos.direction == "demand" else -1.0
            idx_pts = dir_sign * (exit_index - pos.entry_index)

            for spec in instruments:
                gross, costs, net = _price_trade(spec, pos, exit_time, exit_index,
                                                 md, model_params, cost_cfg)
                trades.append(Trade(
                    signal_id=None, date=day.isoformat(),
                    entry_time=pos.entry_time, exit_time=exit_time,
                    direction=pos.direction, instrument=spec.name,
                    index_entry=pos.entry_index, index_exit=exit_index,
                    index_points=idx_pts, gross_pnl=gross, costs=costs, net_pnl=net,
                    exit_reason=reason,
                    meta={"booster": pos.booster, "zone": sig.zone.zone_type,
                          "risk": round(abs(pos.entry_index - pos.stop_loss), 1)},
                ))
            last_exit = exit_time     # block re-entry until this trade is done

    return trades
