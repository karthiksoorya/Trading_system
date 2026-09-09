"""
Check the synthetic option model against REAL daily option prices.

`calibrate.py` used to fit `option_model.py` to ~14 live trades only. This script
marks every liquid near-the-money NIFTY weekly contract in the NSE bhavcopy at
its own day's close and compares the model premium to the real last-traded
premium — thousands of points instead of fourteen. It answers one question: how
wrong, and wrong in which direction, is the model we currently trust across
history?

The mark universe comes from `nse_bhavcopy.liquid_marks()` (also used by the
bhavcopy stage of calibrate.py), so "validate" and "calibrate" see the same rows.

Prereqs:
    python -m backtest.data.fetch --years 3          # nifty + vix candles
    python -m backtest.data.nse_bhavcopy --years 3   # real option prices

Run:
    python -m backtest.validate_option_model
    python -m backtest.validate_option_model --max-dte 7 --mny 0.03
"""

from __future__ import annotations

import argparse
from datetime import datetime

import numpy as np
import pandas as pd

from .data.nse_bhavcopy import liquid_marks
from .option_model import DEFAULT_PARAMS, ModelParams, OptionContract, price_at

MARK_TIME = (15, 29)          # price the model a minute before the 15:30 close


def _bucket_dte(n: int) -> str:
    if n <= 2:
        return "2"
    if n <= 3:
        return "3"
    if n <= 5:
        return "4-5"
    if n <= 7:
        return "6-7"
    return "8+"


def _bucket_mny(m: float) -> str:
    # m = spot/strike - 1 with the buyer-favourable sign applied by the caller
    a = abs(m)
    if a <= 0.0025:
        return "ATM (±0.25%)"
    side = "ITM" if m > 0 else "OTM"
    if a <= 0.01:
        return f"{side} 0.25-1%"
    return f"{side} >1%"


def price_marks(marks: pd.DataFrame, params: ModelParams = DEFAULT_PARAMS) -> pd.DataFrame:
    """Add model premium + error columns to a `liquid_marks()` frame."""
    rows = marks.copy()

    def _model_mid(r) -> float:
        c = OptionContract(float(r["strike"]), r["exp_d"], r["option_type"])
        now = datetime(r["d"].year, r["d"].month, r["d"].day, *MARK_TIME)
        return price_at(c, float(r["spot"]), float(r["vix"]), now, params=params)

    rows["model"] = rows.apply(_model_mid, axis=1)
    rows["err"] = rows["model"] - rows["real"]              # + = model too rich
    rows["abs_err"] = rows["err"].abs()
    rows["pct_err"] = rows["err"] / rows["real"] * 100.0

    fav = np.where(rows["option_type"] == "CE", 1.0, -1.0)
    rows["mny_signed"] = (rows["spot"] / rows["strike"] - 1.0) * fav
    rows["dte_bucket"] = rows["dte"].map(_bucket_dte)
    rows["mny_bucket"] = rows["mny_signed"].map(_bucket_mny)
    return rows


def _summ(g: pd.DataFrame) -> pd.Series:
    return pd.Series({
        "n": len(g),
        "real_med": g["real"].median(),
        "med_abs_err": g["abs_err"].median(),
        "med_signed_err": g["err"].median(),
        "med_pct_err": g["pct_err"].median(),
        "within_5pt_%": (g["abs_err"] <= 5).mean() * 100,
        "within_10pt_%": (g["abs_err"] <= 10).mean() * 100,
    })


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-dte", type=int, default=10, help="max calendar days to expiry (default 10)")
    ap.add_argument("--min-dte", type=int, default=2, help="min days to expiry — matches live 2-day buffer (default 2)")
    ap.add_argument("--mny", type=float, default=0.04, help="max |strike/spot - 1| (default 0.04)")
    ap.add_argument("--min-oi", type=int, default=1000, help="min open interest (contracts)")
    ap.add_argument("--min-vol", type=int, default=1000, help="min traded volume")
    ap.add_argument("--csv", default=None, help="also write the per-contract rows here")
    args = ap.parse_args()

    marks = liquid_marks(args.max_dte, args.min_dte, args.mny, args.min_oi, args.min_vol)
    if marks.empty:
        raise SystemExit("No contracts matched. Widen --mny / --max-dte, or fetch more bhavcopy days.")
    rows = price_marks(marks)

    span = f"{rows['date'].min():%Y-%m-%d} → {rows['date'].max():%Y-%m-%d}"
    print(f"\nReal vs model — {len(rows):,} liquid NIFTY contracts  ({span})")
    print(f"  filters: {args.min_dte}≤dte≤{args.max_dte}, |moneyness|≤{args.mny:.0%}, "
          f"OI≥{args.min_oi:,}, vol≥{args.min_vol:,}\n")

    overall = _summ(rows)
    print(f"  median absolute error : {overall['med_abs_err']:6.1f} premium pts")
    print(f"  median signed error   : {overall['med_signed_err']:+6.1f} pts   "
          f"({'model RICH — backtest too optimistic for buyers' if overall['med_signed_err'] > 0 else 'model CHEAP'})")
    print(f"  median % error        : {overall['med_pct_err']:+6.1f} %")
    print(f"  within  5 pts         : {overall['within_5pt_%']:5.1f} %")
    print(f"  within 10 pts         : {overall['within_10pt_%']:5.1f} %")
    lot = OptionContract(0, rows['exp_d'].iloc[0], 'CE').lot_size
    print(f"  → per-leg rupee bias  : ₹{overall['med_signed_err'] * lot:,.0f}  "
          f"(lot {lot}); round-trip ≈ ₹{overall['med_signed_err'] * lot * 2:,.0f}\n")

    with pd.option_context("display.float_format", lambda x: f"{x:9.2f}",
                           "display.max_columns", None, "display.width", 200):
        print("By days-to-expiry:")
        print(rows.groupby("dte_bucket", sort=False).apply(_summ).sort_index(), "\n")
        print("By moneyness (buyer's perspective):")
        print(rows.groupby("mny_bucket").apply(_summ), "\n")
        print("By calendar quarter (is the fit stable over time?):")
        rows["q"] = rows["date"].dt.to_period("Q").astype(str)
        print(rows.groupby("q").apply(_summ), "\n")

    print("Read: a positive signed error means the model prices options ABOVE what\n"
          "they really traded at — a long-option backtest using it will look better\n"
          "than reality by roughly that per-leg rupee bias, every trade.\n")

    if args.csv:
        keep = ["date", "expiry", "dte", "strike", "option_type", "spot", "vix",
                "real", "model", "err", "pct_err", "oi", "volume"]
        rows.rename(columns={"exp_d": "expiry"})[keep].to_csv(args.csv, index=False)
        print(f"  wrote {args.csv}")


if __name__ == "__main__":
    main()
