"""
Run a backtest and print the instrument comparison.

    python -m backtest.run                       # demand/supply, full history, all instruments
    python -m backtest.run --from 2024-01-01 --to 2024-12-31
    python -m backtest.run --no-trend-filter     # ablation: drop the 60min trend filter
    python -m backtest.run --classes demand      # CE only
    python -m backtest.run --entry-mode market   # live-style market fill (vs default limit-at-proximal)
    python -m backtest.run --csv out.csv         # dump every trade

Output: a table with one row per instrument —
  net P&L, expectancy, profit factor, max drawdown, Sharpe, and ₹ earned per
  index point (the conversion efficiency the whole exercise is about).
"""

from __future__ import annotations

import argparse
import csv
from datetime import date

from .data import CACHE_DIR
from .metrics import compare_table
from .replay import INSTRUMENTS, run_backtest
from .strategy_ds import DSParams


def _synthetic_warning():
    if (CACHE_DIR / "_SYNTHETIC.flag").exists():
        print("=" * 70)
        print("⚠  CACHE IS SYNTHETIC TOY DATA — these numbers mean nothing.")
        print("   Run:  python -m backtest.data.fetch --years 3   (needs Kite login)")
        print("=" * 70)


def _fmt_table(rows: list[dict]) -> str:
    if not rows:
        return "(no trades)"
    cols = list(rows[0].keys())
    widths = {c: max(len(str(c)), *(len(str(r[c])) for r in rows)) for c in cols}
    line = "  ".join(f"{c:>{widths[c]}}" for c in cols)
    sep = "  ".join("-" * widths[c] for c in cols)
    body = "\n".join("  ".join(f"{str(r[c]):>{widths[c]}}" for c in cols) for r in rows)
    return f"{line}\n{sep}\n{body}"


def main():
    _d = DSParams()   # defaults mirror the live config
    ap = argparse.ArgumentParser(description="Defaults mirror config.py (PE-only, score 10, conf 3, ...)")
    ap.add_argument("--from", dest="start", default=None)
    ap.add_argument("--to", dest="end", default=None)
    ap.add_argument("--classes", default=",".join(_d.active_classes))
    ap.add_argument("--entry-tf", default=_d.entry_tf)
    ap.add_argument("--min-confluence", type=int, default=_d.min_confluence)
    ap.add_argument("--min-score", type=float, default=_d.min_booster_score)
    ap.add_argument("--approach", type=float, default=_d.zone_approach_points)
    ap.add_argument("--min-risk", type=float, default=_d.min_risk_points, help="skip signals with |entry-SL| below this (0=off)")
    ap.add_argument("--max-risk", type=float, default=_d.max_risk_points, help="skip signals with |entry-SL| above this (0=off)")
    ap.add_argument("--no-trend-filter", action="store_true")
    ap.add_argument("--no-ce-time-filter", action="store_true")
    ap.add_argument("--no-vix-filter", action="store_true")
    ap.add_argument("--time-exit-hour", type=int, default=13)
    ap.add_argument("--entry-mode", choices=["limit", "market"], default="limit",
                    help="limit = order at zone proximal, no fill if price never returns; "
                         "market = fill next bar open (current live behaviour)")
    ap.add_argument("--entry-delay-bars", type=int, default=0,
                    help="market mode: extra 5-min bars between signal and fill (approval lag)")
    ap.add_argument("--entry-delay-frac", type=float, default=0.0,
                    help="market mode: fraction into the fill bar (0=open,1=close) for sub-bar lag")
    ap.add_argument("--csv", default=None)
    args = ap.parse_args()

    params = DSParams(
        entry_tf=args.entry_tf,
        active_classes=tuple(args.classes.split(",")),
        min_booster_score=args.min_score,
        min_confluence=args.min_confluence,
        zone_approach_points=args.approach,
        min_risk_points=args.min_risk,
        max_risk_points=args.max_risk,
        entry_delay_bars=args.entry_delay_bars,
        entry_delay_frac=args.entry_delay_frac,
        trend_filter=not args.no_trend_filter,
        ce_after_11=not args.no_ce_time_filter,
        vix_direction_filter=not args.no_vix_filter,
        time_exit_hour=args.time_exit_hour,
    )
    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None

    _synthetic_warning()
    print(f"\n{params.label()}")
    print(f"range: {start or 'all'} → {end or 'all'}   entry: {args.entry_mode}\n")

    trades = run_backtest(params=params, start=start, end=end, entry_mode=args.entry_mode)

    n_signals = len({(t.date, t.entry_time) for t in trades})
    print(f"\n{n_signals} trades taken\n")
    print(_fmt_table(compare_table(trades)))

    print("\nby exit reason (opt_itm1):")
    reasons: dict[str, list] = {}
    for t in trades:
        if t.instrument == "opt_itm1":
            reasons.setdefault(t.exit_reason, []).append(t.net_pnl)
    for r, vals in sorted(reasons.items()):
        print(f"  {r:10} n={len(vals):3}  net ₹{sum(vals):>9,.0f}  avg ₹{sum(vals)/len(vals):>7,.0f}")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "entry_time", "exit_time", "direction", "instrument",
                        "index_entry", "index_exit", "index_points", "gross", "costs",
                        "net", "exit_reason", "booster", "zone"])
            for t in trades:
                w.writerow([t.date, t.entry_time, t.exit_time, t.direction, t.instrument,
                            round(t.index_entry, 2), round(t.index_exit, 2),
                            round(t.index_points, 2), round(t.gross_pnl), round(t.costs),
                            round(t.net_pnl), t.exit_reason,
                            t.meta.get("booster"), t.meta.get("zone")])
        print(f"\nwrote {args.csv}")


if __name__ == "__main__":
    main()
