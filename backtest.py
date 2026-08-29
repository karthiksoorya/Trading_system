"""
backtest.py — Offline zone-detection backtest with ATR departure and BOS annotation.

Runs zone detection on a synthetic candle series, then simulates entries at
each zone's proximal with a 1:2 R:R target and fixed stop loss.

Produces a BEFORE / AFTER comparison:
  BEFORE — all detected zones (no quality filter)
  AFTER  — zones filtered by departure_strength >= MIN_DEPARTURE and/or BOS confirmed

Usage:
    python backtest.py               # synthetic data (default)
    python backtest.py --csv path    # load OHLC from a CSV  (cols: open,high,low,close)
    python backtest.py --departure 1.5 --bos  # stricter filter
"""

import argparse
import csv
import math
import random
from datetime import datetime, timedelta
from typing import Optional

from brokers.base import Candle
from engine.zones import BOS, Zone, detect_bos, detect_zones


# ── Candle generation ─────────────────────────────────────────────────────────

def _synthetic_candles(n: int = 300, seed: int = 42) -> list[Candle]:
    """
    Generate a synthetic Nifty-like intraday candle series with embedded
    demand/supply zones.  Each 'session' alternates: trend → consolidation → reversal.
    """
    rng = random.Random(seed)
    candles: list[Candle] = []
    price = 24_000.0
    ts = datetime(2026, 1, 2, 9, 15)
    step = timedelta(minutes=5)

    for i in range(n):
        phase = (i // 30) % 4   # 0=uptrend, 1=consol, 2=downtrend, 3=consol

        if phase == 0:       # uptrend — bullish exciting candles
            body  = rng.uniform(25, 70)
            wicks = rng.uniform(2, 15)
            lo    = price - wicks
            hi    = price + body + wicks
            close = price + body
        elif phase == 2:     # downtrend — bearish exciting candles
            body  = rng.uniform(25, 70)
            wicks = rng.uniform(2, 15)
            hi    = price + wicks
            lo    = price - body - wicks
            close = price - body
        else:                # consolidation — boring candles
            body  = rng.uniform(1, 10)
            wicks = rng.uniform(8, 25)
            direction = rng.choice([-1, 1])
            lo    = price - wicks
            hi    = price + wicks
            close = price + direction * body

        o     = price
        lo    = min(lo, o, close)
        hi    = max(hi, o, close)
        close = max(lo, min(hi, close))   # clamp

        candles.append(Candle(timestamp=ts, open=round(o, 2), high=round(hi, 2),
                               low=round(lo, 2), close=round(close, 2), volume=rng.randint(5000, 50000)))
        price = close
        ts += step

    return candles


def _load_csv(path: str) -> list[Candle]:
    candles: list[Candle] = []
    ts = datetime(2026, 1, 2, 9, 15)
    step = timedelta(minutes=5)
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            candles.append(Candle(
                timestamp=ts,
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]),  close=float(row["close"]),
                volume=int(float(row.get("volume", 10000))),
            ))
            ts += step
    return candles


# ── Simulation ────────────────────────────────────────────────────────────────

def _simulate_trade(zone: Zone, candles_after: list[Candle], sl_buffer: float = 5.0) -> dict:
    """
    Simulate entering at proximal, SL at distal ± buffer, target at 2× risk.
    Returns a result dict with outcome, pnl_points, hold_candles.
    """
    entry = zone.proximal
    if zone.zone_class == "demand":
        sl     = zone.distal - sl_buffer
        target = entry + 2 * abs(entry - sl)
    else:
        sl     = zone.distal + sl_buffer
        target = entry - 2 * abs(entry - sl)

    for k, c in enumerate(candles_after):
        if zone.zone_class == "demand":
            if c.low <= sl:
                return {"outcome": "SL", "pnl_points": round(sl - entry, 1), "hold": k + 1}
            if c.high >= target:
                return {"outcome": "TGT", "pnl_points": round(target - entry, 1), "hold": k + 1}
        else:
            if c.high >= sl:
                return {"outcome": "SL", "pnl_points": round(sl - entry, 1), "hold": k + 1}
            if c.low <= target:
                return {"outcome": "TGT", "pnl_points": round(target - entry, 1), "hold": k + 1}

    return {"outcome": "EOD", "pnl_points": 0.0, "hold": len(candles_after)}


# ── Report ────────────────────────────────────────────────────────────────────

def _pct(num: int, denom: int) -> str:
    return f"{num/denom*100:.0f}%" if denom else "—"


def _summarise(rows: list[dict], label: str) -> None:
    if not rows:
        print(f"\n  {label}: no zones\n")
        return

    wins  = [r for r in rows if r["outcome"] == "TGT"]
    loses = [r for r in rows if r["outcome"] == "SL"]
    eods  = [r for r in rows if r["outcome"] == "EOD"]
    total_pnl = sum(r["pnl_points"] for r in rows)

    print(f"\n{'-'*60}")
    print(f"  {label}")
    print(f"{'-'*60}")
    print(f"  Zones evaluated : {len(rows)}")
    print(f"  Win (TGT hit)   : {len(wins)}  ({_pct(len(wins), len(rows))})")
    print(f"  Loss (SL hit)   : {len(loses)}  ({_pct(len(loses), len(rows))})")
    print(f"  EOD (no exit)   : {len(eods)}")
    print(f"  Net P&L (pts)   : {total_pnl:+.1f}")
    print(f"  Avg departure   : {sum(r['departure_strength'] for r in rows)/len(rows):.2f}xATR")
    bos_ct = sum(1 for r in rows if r.get("bos"))
    print(f"  BOS confirmed   : {bos_ct}  ({_pct(bos_ct, len(rows))})")
    print(f"{'-'*60}")
    print(f"  {'Zone':<8} {'Type':<5} {'Class':<7} {'Dep':<7} {'BOS':<5} {'Result':<6} {'P&L':>7}")
    print(f"  {'-'*8} {'-'*5} {'-'*7} {'-'*7} {'-'*5} {'-'*6} {'-'*7}")
    for r in rows:
        print(f"  {r['zone_idx']:<8} {r['zone_type']:<5} {r['zone_class']:<7} "
              f"  {r['departure_strength']:>4.2f}  {'Y' if r.get('bos') else 'N':<5} "
              f"{r['outcome']:<6} {r['pnl_points']:>+6.1f}")


# ── Main ──────────────────────────────────────────────────────────────────────

def run(candles: list[Candle], min_departure: float = 0.0,
        require_bos: bool = False, bos_lookback: int = 20) -> None:

    print(f"\n{'='*60}")
    print(f"  BACKTEST - zone detection on {len(candles)} candles")
    print(f"  Filter: departure_strength >= {min_departure:.1f}xATR"
          f"{'  |  BOS required' if require_bos else ''}")
    print(f"{'='*60}")

    zones = detect_zones(candles, timeframe="5minute")
    print(f"\n  Zones detected: {len(zones)}")

    all_rows:      list[dict] = []
    filtered_rows: list[dict] = []

    for idx, zone in enumerate(zones):
        # Find where this zone appears in the candle series (by formed_at timestamp)
        formed_idx = next(
            (k for k, c in enumerate(candles) if c.timestamp >= zone.formed_at), None
        )
        if formed_idx is None:
            continue
        post = candles[formed_idx + 1:]
        if not post:
            continue

        # BOS: detected in the candles BEFORE zone formation
        pre    = candles[:formed_idx + 1]
        bos    = detect_bos(pre, lookback=bos_lookback)
        bos_ok = bos is not None and (
            (zone.zone_class == "demand" and bos.direction == "bullish") or
            (zone.zone_class == "supply" and bos.direction == "bearish")
        )

        sim = _simulate_trade(zone, post)
        row = {
            "zone_idx":         idx + 1,
            "zone_type":        zone.zone_type,
            "zone_class":       zone.zone_class,
            "departure_strength": zone.departure_strength,
            "bos":              bos_ok,
            **sim,
        }
        all_rows.append(row)

        passes = zone.departure_strength >= min_departure
        if require_bos:
            passes = passes and bos_ok
        if passes:
            filtered_rows.append(row)

    _summarise(all_rows, "BEFORE (all zones, no quality filter)")

    label = f"AFTER  (departure >= {min_departure:.1f}xATR"
    if require_bos:
        label += ", BOS confirmed"
    label += ")"
    _summarise(filtered_rows, label)

    if all_rows:
        before_pnl   = sum(r["pnl_points"] for r in all_rows)
        after_pnl    = sum(r["pnl_points"] for r in filtered_rows)
        pnl_delta    = after_pnl - before_pnl
        zones_saved  = len(all_rows) - len(filtered_rows)
        print(f"\n  Improvement: {zones_saved} low-quality zones filtered out")
        print(f"  P&L delta  : {pnl_delta:+.1f} pts  "
              f"({'better' if pnl_delta >= 0 else 'worse'} than unfiltered)\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Zone-detection backtest with ATR + BOS filter.")
    parser.add_argument("--csv",        default=None,  help="Path to OHLC CSV (optional)")
    parser.add_argument("--departure",  type=float, default=1.0,
                        help="Min ATR-normalized departure_strength (default 1.0)")
    parser.add_argument("--bos",        action="store_true",
                        help="Also require BOS confirmation in zone direction")
    parser.add_argument("--candles",    type=int, default=300,
                        help="Number of synthetic candles (default 300, ignored with --csv)")
    parser.add_argument("--seed",       type=int, default=42, help="RNG seed for synthetic data")
    args = parser.parse_args()

    if args.csv:
        print(f"Loading candles from {args.csv} ...")
        candles = _load_csv(args.csv)
    else:
        print(f"Generating {args.candles} synthetic candles (seed={args.seed}) ...")
        candles = _synthetic_candles(n=args.candles, seed=args.seed)

    run(candles, min_departure=args.departure, require_bos=args.bos)


if __name__ == "__main__":
    main()
