"""
Calibrate the option-pricing model against the real option fills recorded in
data/trades_*.db.

We know, for ~18 live trades: the index level and time at entry and exit, the
strike, the expiry, and the *actual* premium paid and received. This fits the
model's free parameters (term_mult, crush_coef, theta_accel) so its premiums
reproduce those fills, then reports the residual error. If the fit is tight we
can trust the model across the full history where no real premiums exist.

Run:
    python -m backtest.calibrate
    python -m backtest.calibrate --db data/trades_2026-08-29.db
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import numpy as np

from .marketdata import MarketData
from .option_model import DEFAULT_PARAMS, ModelParams, OptionContract, fill_price, price_at

BASE_DIR = Path(__file__).resolve().parents[1]

# Kite weekly tradingsymbol: NIFTY + YY + M + DD + STRIKE + CE/PE
#   M = 1..9 for Jan..Sep, O/N/D for Oct/Nov/Dec
#   monthly form: NIFTY + YY + MMM + STRIKE + CE/PE  (e.g. NIFTY26SEP24000CE)
_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
_MCODE = {"O": 10, "N": 11, "D": 12}


def parse_symbol(sym: str) -> tuple[float, date, str]:
    """Return (strike, expiry, option_type) from a Kite NFO tradingsymbol."""
    m = re.match(r"NIFTY(\d{2})([A-Z]{3})(\d+)(CE|PE)$", sym)
    if m:                                   # monthly
        yy, mon, strike, ot = m.groups()
        exp = _last_tuesday(2000 + int(yy), _MONTHS[mon])
        return float(strike), exp, ot
    m = re.match(r"NIFTY(\d{2})([1-9OND])(\d{2})(\d+)(CE|PE)$", sym)
    if m:                                   # weekly
        yy, mc, dd, strike, ot = m.groups()
        month = _MCODE.get(mc, int(mc) if mc.isdigit() else 0)
        return float(strike), date(2000 + int(yy), month, int(dd)), ot
    raise ValueError(f"cannot parse option symbol: {sym}")


def _last_tuesday(year: int, month: int) -> date:
    from datetime import timedelta
    d = date(year, month, 28) + timedelta(days=4)
    d = d - timedelta(days=d.day)
    while d.weekday() != 1:
        d -= timedelta(days=1)
    return d


# Trades the knowledge base explicitly flags as non-representative of a
# disciplined systematic exit — excluded from the FIT, still shown in the report.
#   806, 867 : manual close after a multi-minute stall (spread + panic)
#   772, 809 : bought into a sharp bounce at a spiked premium, exited in seconds
OUTLIER_IDS = {806, 867, 772, 809}


def load_real_trades(db_path: Path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT id, date, time_signal, exit_time, entry, exit_price,
               options_symbol, options_entry_price, options_exit_price, options_lot_size
        FROM signals
        WHERE status='closed' AND mode='live'
          AND options_entry_price IS NOT NULL AND options_exit_price IS NOT NULL
          AND options_symbol IS NOT NULL
        ORDER BY date, time_signal
    """).fetchall()
    con.close()
    out = []
    for r in rows:
        try:
            strike, exp, ot = parse_symbol(r["options_symbol"])
        except ValueError as e:
            print(f"  skip #{r['id']}: {e}")
            continue
        d = r["date"]
        et = datetime.fromisoformat(f"{d}T{_pad(r['time_signal'])}")
        xt = datetime.fromisoformat(f"{d}T{_pad(r['exit_time'])}") if r["exit_time"] else et
        out.append(dict(
            id=r["id"], day=date.fromisoformat(d),
            entry_time=et, exit_time=xt,
            entry_index=r["entry"], exit_index=r["exit_price"],
            strike=strike, expiry=exp, option_type=ot,
            real_entry=r["options_entry_price"], real_exit=r["options_exit_price"],
            lot=r["options_lot_size"] or 65,
        ))
    return out


def _pad(t: str) -> str:
    parts = str(t).split(":")
    while len(parts) < 3:
        parts.append("00")
    return ":".join(f"{int(p):02d}" for p in parts)


def _residuals(trades, md: MarketData, mp: ModelParams):
    rows = []
    for t in trades:
        c = OptionContract(t["strike"], t["expiry"], t["option_type"], t["lot"])
        v_en = md.vix_at(t["entry_time"])
        v_ex = md.vix_at(t["exit_time"])
        if v_en is None or v_ex is None:
            continue
        m_en = fill_price(price_at(c, t["entry_index"], v_en, t["entry_time"], params=mp),
                          "buy", mp)
        m_ex = fill_price(price_at(c, t["exit_index"], v_ex, t["exit_time"],
                                   entry_spot=t["entry_index"], params=mp), "sell", mp)
        real_pnl = (t["real_exit"] - t["real_entry"]) * t["lot"]
        model_pnl = (m_ex - m_en) * t["lot"]
        rows.append(dict(id=t["id"], real_en=t["real_entry"], mod_en=m_en,
                         real_ex=t["real_exit"], mod_ex=m_ex,
                         real_pnl=real_pnl, model_pnl=model_pnl))
    return rows


def _score(rows) -> float:
    """Robust: median absolute error across entry premium, exit premium and P&L.
    Median (not mean) so a few spike-entry / panic-exit trades don't dominate."""
    if not rows:
        return 1e9
    errs = []
    for r in rows:
        errs.append(abs(r["mod_en"] - r["real_en"]))
        errs.append(abs(r["mod_ex"] - r["real_ex"]))
        errs.append(abs(r["model_pnl"] - r["real_pnl"]) / 65)
    return float(np.median(errs))


def calibrate(trades, md: MarketData) -> tuple[ModelParams, float]:
    grid = dict(
        term_mult=np.arange(0.60, 1.61, 0.05),
        iv_add=np.arange(-0.06, 0.061, 0.01),
        skew=np.arange(0.0, 1.51, 0.15),
        crush_coef=np.arange(0.0, 1.21, 0.10),
        theta_accel=np.arange(1.0, 2.01, 0.10),
        half_spread_pts=np.arange(0.0, 12.1, 1.0),
    )
    cur = dict(term_mult=1.0, iv_add=0.0, skew=0.35, crush_coef=0.55,
               theta_accel=1.15, half_spread_pts=3.0)
    fit_set = [t for t in trades if t["id"] not in OUTLIER_IDS]
    best, best_s = replace(DEFAULT_PARAMS, **cur), 1e18
    for _ in range(4):                                   # coordinate descent sweeps
        for key, values in grid.items():
            scored = []
            for v in values:
                mp = replace(DEFAULT_PARAMS, **{**cur, key: float(v)})
                scored.append((_score(_residuals(fit_set, md, mp)), float(v)))
            s, v = min(scored)
            cur[key] = v
            if s < best_s:
                best_s, best = s, replace(DEFAULT_PARAMS, **cur)
    return best, best_s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="path to trades DB (default: newest data/trades_*.db)")
    args = ap.parse_args()

    if args.db:
        db = Path(args.db)
    else:
        cands = sorted((BASE_DIR / "data").glob("trades_*.db"))
        db = cands[-1] if cands else BASE_DIR / "data" / "trades.db"
    if not db.exists():
        raise SystemExit(f"no trade DB found at {db}")
    print(f"Calibrating against {db.name}")

    trades = load_real_trades(db)
    print(f"  {len(trades)} real option trades with full data\n")

    md = MarketData()
    have_vix = sum(1 for t in trades if md.vix_at(t["entry_time"]) is not None)
    if have_vix == 0:
        raise SystemExit(
            "No cached VIX data covers these trade dates.\n"
            "Run:  python -m backtest.data.fetch --years 1   (needs a fresh Kite token)"
        )

    best, score = calibrate(trades, md)
    print("Fitted parameters (robust / median-error):")
    print(f"  term_mult       = {best.term_mult:.3f}")
    print(f"  iv_add          = {best.iv_add:+.3f}")
    print(f"  skew            = {best.skew:.3f}")
    print(f"  crush_coef      = {best.crush_coef:.3f}")
    print(f"  theta_accel     = {best.theta_accel:.3f}")
    print(f"  half_spread_pts = {best.half_spread_pts:.1f}")
    print(f"  median abs error = {score:.1f} premium points\n")

    rows = _residuals(trades, md, best)
    print(f"{'id':>4} {'':1} {'real_en':>8} {'mod_en':>8} {'real_ex':>8} {'mod_ex':>8} "
          f"{'realP&L':>9} {'modelP&L':>9} {'err':>7}")
    for r in rows:
        flag = "*" if r["id"] in OUTLIER_IDS else " "
        print(f"{r['id']:>4} {flag} {r['real_en']:>8.1f} {r['mod_en']:>8.1f} "
              f"{r['real_ex']:>8.1f} {r['mod_ex']:>8.1f} "
              f"{r['real_pnl']:>9.0f} {r['model_pnl']:>9.0f} "
              f"{r['model_pnl']-r['real_pnl']:>7.0f}")

    clean = [r for r in rows if r["id"] not in OUTLIER_IDS]
    for label, rr in [("fit set (14 disciplined)", clean), ("all 18", rows)]:
        errs = [abs(r["model_pnl"] - r["real_pnl"]) for r in rr]
        within = sum(1 for e in errs if e <= 300)
        print(f"\n  [{label}] P&L error median ₹{np.median(errs):,.0f} "
              f"mean ₹{np.mean(errs):,.0f} | {within}/{len(rr)} within ₹300 | "
              f"real ₹{sum(r['real_pnl'] for r in rr):,.0f} vs model ₹{sum(r['model_pnl'] for r in rr):,.0f}")
    print("  (* = excluded from fit: spike entry or manual-stall exit)")
    print("\n  NOTE: the real trades include 37-second panic exits and 13-minute manual")
    print("  stalls the model can't reproduce. It targets typical disciplined trades.")
    print("  Paste params into option_model.ModelParams if entry premiums look sane.")


if __name__ == "__main__":
    main()
