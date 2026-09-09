"""
Calibrate the option-pricing model, in two stages.

STAGE A — the pricing surface, against NSE bhavcopy daily closes.
    Thousands of real liquid near-money weekly closes (via
    nse_bhavcopy.liquid_marks) fix the *static* premium: term_mult, iv_add,
    skew, theta_accel, iv_floor. A time split (fit early, score late) shows
    whether the fit generalises or is just curve-fitting the sample.

STAGE B — the execution / dynamics terms, against the ~14 disciplined live
    trades in data/trades_*.db. With the surface held fixed, this fits only
    crush_coef (IV deflation as the trade's thesis plays out) and
    half_spread_pts (bid/ask paid) — the parts a daily close cannot show.

Fall back to Stage B alone (old behaviour) if no bhavcopy cache exists.

Run:
    python -m backtest.data.nse_bhavcopy --years 3     # once, for Stage A
    python -m backtest.calibrate
    python -m backtest.calibrate --source trades       # Stage B only
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

MARK_TIME = (15, 29)          # bhavcopy close ≈ a mid a minute before 15:30

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


# ─────────────────────────────────────────────────────────────────────────────
# Stage A — pricing surface vs NSE bhavcopy daily closes
# ─────────────────────────────────────────────────────────────────────────────
# Only params that move a static mid premium. crush_coef needs a move-from-entry
# and half_spread_pts needs bid/ask, so neither is observable from a daily close.
_SURFACE_KEYS = ("term_mult", "iv_add", "skew", "theta_accel", "iv_floor")

_SURFACE_GRID = dict(
    term_mult=np.arange(0.60, 1.61, 0.05),
    iv_add=np.arange(-0.06, 0.061, 0.01),
    skew=np.arange(0.0, 2.51, 0.10),
    theta_accel=np.arange(0.8, 2.01, 0.10),
    iv_floor=np.arange(0.03, 0.121, 0.01),
)


def _mark_tuples(df) -> list[tuple]:
    out = []
    for r in df.itertuples(index=False):
        now = datetime(r.d.year, r.d.month, r.d.day, *MARK_TIME)
        out.append((float(r.strike), r.exp_d, r.option_type,
                    float(r.spot), float(r.vix), now))
    return out


def _model_prices(tuples: list[tuple], mp: ModelParams) -> np.ndarray:
    out = np.empty(len(tuples))
    for i, (strike, exp_d, ot, spot, vix, now) in enumerate(tuples):
        out[i] = price_at(OptionContract(strike, exp_d, ot), spot, vix, now, params=mp)
    return out


def _surface_score(real: np.ndarray, model: np.ndarray) -> float:
    """Median abs error, penalised hard for a residual bias (we want it centred)."""
    err = model - real
    return float(np.median(np.abs(err)) + 2.0 * abs(np.median(err)))


def _fit_surface(tuples: list[tuple], real: np.ndarray,
                 start: ModelParams = DEFAULT_PARAMS) -> tuple[ModelParams, float]:
    cur = {k: getattr(start, k) for k in _SURFACE_KEYS}
    best, best_s = replace(DEFAULT_PARAMS, **cur), 1e18
    for _ in range(4):
        for key, values in _SURFACE_GRID.items():
            scored = []
            for v in values:
                mp = replace(DEFAULT_PARAMS, **{**cur, key: float(v)})
                scored.append((_surface_score(real, _model_prices(tuples, mp)), float(v)))
            s, v = min(scored)
            cur[key] = v
            if s < best_s:
                best_s, best = s, replace(DEFAULT_PARAMS, **cur)
    return best, best_s


def _report_marks(label: str, df, mp: ModelParams) -> None:
    err = _model_prices(_mark_tuples(df), mp) - df["real"].to_numpy()
    ae = np.abs(err)
    lot = 65
    print(f"  [{label:<22}] n={len(df):>6,}  "
          f"med|err| {np.median(ae):5.1f}pt  bias {np.median(err):+5.1f}pt "
          f"(₹{np.median(err) * lot:+,.0f}/leg)  "
          f"≤5pt {100*np.mean(ae <= 5):4.1f}%  ≤10pt {100*np.mean(ae <= 10):4.1f}%")


def calibrate_surface(max_dte: int, mny: float, split: float, seed: int,
                      n_fit: int) -> ModelParams | None:
    try:
        from .data.nse_bhavcopy import liquid_marks
    except Exception as e:                       # pragma: no cover
        print(f"  (bhavcopy stage unavailable: {e})")
        return None
    try:
        marks = liquid_marks(max_dte=max_dte, mny=mny)
    except FileNotFoundError as e:
        print(f"  (skipping Stage A — {e})")
        return None
    if len(marks) < 200:
        print(f"  (skipping Stage A — only {len(marks)} marks; run a wider bhavcopy pull)")
        return None

    marks = marks.sort_values("d").reset_index(drop=True)
    days = sorted(marks["d"].unique())
    cut = days[int(len(days) * split)]
    train = marks[marks["d"] < cut]
    test = marks[marks["d"] >= cut]

    rng = np.random.default_rng(seed)
    if n_fit <= 0 or len(train) <= n_fit:
        fit_src = train
    else:
        fit_src = train.iloc[np.sort(rng.choice(len(train), n_fit, replace=False))]
    tuples = _mark_tuples(fit_src)
    real = fit_src["real"].to_numpy()

    print(f"\nSTAGE A — pricing surface vs bhavcopy "
          f"({marks['date'].min():%Y-%m-%d} → {marks['date'].max():%Y-%m-%d}, "
          f"{len(marks):,} marks; fit on {len(fit_src):,} of the first {int(split*100)}%)")

    print("  before (current option_model defaults):")
    _report_marks("train", train, DEFAULT_PARAMS)
    _report_marks("test / held-out", test, DEFAULT_PARAMS)

    fitted, s = _fit_surface(tuples, real)
    print(f"  fit score {s:.2f}. after:")
    _report_marks("train", train, fitted)
    _report_marks("test / held-out", test, fitted)

    for k in _SURFACE_KEYS:
        print(f"    {k:<14}= {getattr(fitted, k):+.3f}   (was {getattr(DEFAULT_PARAMS, k):+.3f})")
    return fitted


# ─────────────────────────────────────────────────────────────────────────────
# Stage B — execution / dynamics vs the real live trades
# ─────────────────────────────────────────────────────────────────────────────
_DYN_GRID = dict(
    crush_coef=np.arange(0.0, 1.21, 0.05),
    half_spread_pts=np.arange(0.0, 12.1, 0.5),
)


def calibrate_dynamics(trades, md: MarketData,
                       surface: ModelParams) -> tuple[ModelParams, float]:
    """Fit only crush_coef + half_spread_pts, with the surface held fixed."""
    fit_set = [t for t in trades if t["id"] not in OUTLIER_IDS]
    cur = dict(crush_coef=surface.crush_coef, half_spread_pts=surface.half_spread_pts)
    best, best_s = replace(surface, **cur), 1e18
    for _ in range(5):
        for key, values in _DYN_GRID.items():
            scored = []
            for v in values:
                mp = replace(surface, **{**cur, key: float(v)})
                scored.append((_score(_residuals(fit_set, md, mp)), float(v)))
            s, v = min(scored)
            cur[key] = v
            if s < best_s:
                best_s, best = s, replace(surface, **cur)
    return best, best_s


def _report_trades(trades, md: MarketData, mp: ModelParams) -> None:
    rows = _residuals(trades, md, mp)
    print(f"  {'id':>4} {'':1} {'real_en':>8} {'mod_en':>8} {'real_ex':>8} {'mod_ex':>8} "
          f"{'realP&L':>9} {'modelP&L':>9} {'err':>7}")
    for r in rows:
        flag = "*" if r["id"] in OUTLIER_IDS else " "
        print(f"  {r['id']:>4} {flag} {r['real_en']:>8.1f} {r['mod_en']:>8.1f} "
              f"{r['real_ex']:>8.1f} {r['mod_ex']:>8.1f} "
              f"{r['real_pnl']:>9.0f} {r['model_pnl']:>9.0f} "
              f"{r['model_pnl'] - r['real_pnl']:>7.0f}")
    clean = [r for r in rows if r["id"] not in OUTLIER_IDS]
    for label, rr in [("fit set (disciplined)", clean), ("all trades", rows)]:
        errs = [abs(r["model_pnl"] - r["real_pnl"]) for r in rr]
        within = sum(1 for e in errs if e <= 300)
        print(f"    [{label:<21}] P&L err median ₹{np.median(errs):,.0f} "
              f"mean ₹{np.mean(errs):,.0f} | {within}/{len(rr)} within ₹300 | "
              f"real ₹{sum(r['real_pnl'] for r in rr):,.0f} vs model ₹{sum(r['model_pnl'] for r in rr):,.0f}")


# ─────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="trades DB (default: newest data/trades_*.db)")
    ap.add_argument("--source", choices=("both", "bhavcopy", "trades"), default="both",
                    help="which stages to run (default both)")
    ap.add_argument("--max-dte", type=int, default=10, help="Stage A: max days to expiry")
    ap.add_argument("--mny", type=float, default=0.04, help="Stage A: max |strike/spot - 1|")
    ap.add_argument("--split", type=float, default=0.70, help="Stage A: fraction of days to fit on")
    ap.add_argument("--n-fit", type=int, default=8000, help="Stage A: max marks sampled for the fit")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    surface = DEFAULT_PARAMS
    surface_from_A = False

    if args.source in ("both", "bhavcopy"):
        fitted = calibrate_surface(args.max_dte, args.mny, args.split, args.seed, args.n_fit)
        if fitted is not None:
            surface = fitted
            surface_from_A = True
        elif args.source == "bhavcopy":
            raise SystemExit("Stage A produced nothing — run: python -m backtest.data.nse_bhavcopy --years 3")

    final = surface
    if args.source in ("both", "trades"):
        if args.db:
            db = Path(args.db)
        else:
            cands = sorted((BASE_DIR / "data").glob("trades_*.db"))
            db = cands[-1] if cands else BASE_DIR / "data" / "trades.db"
        if not db.exists():
            raise SystemExit(f"no trade DB found at {db}")

        trades = load_real_trades(db)
        md = MarketData()
        have_vix = sum(1 for t in trades if md.vix_at(t["entry_time"]) is not None)
        if have_vix == 0:
            raise SystemExit("No cached VIX covers these trade dates. Run: python -m backtest.data.fetch")

        print(f"\nSTAGE B — dynamics vs {db.name}  ({len(trades)} real option trades, "
              f"surface {'from Stage A' if surface_from_A else 'left at defaults'})")
        print("  before:")
        _report_trades(trades, md, surface)
        final, s = calibrate_dynamics(trades, md, surface)
        print(f"  fit score {s:.1f}. after:")
        _report_trades(trades, md, final)
        print(f"    crush_coef     = {final.crush_coef:.3f}   (was {surface.crush_coef:.3f})")
        print(f"    half_spread_pts= {final.half_spread_pts:.2f}   (was {surface.half_spread_pts:.2f})")

    print("\n" + "─" * 70)
    print("Paste into option_model.ModelParams if the numbers above look sane:\n")
    print(f"    term_mult: float = {final.term_mult:.2f}")
    print(f"    iv_add: float = {final.iv_add:.3f}")
    print(f"    skew: float = {final.skew:.2f}")
    print(f"    crush_coef: float = {final.crush_coef:.2f}")
    print(f"    crush_cap: float = {final.crush_cap:.2f}")
    print(f"    theta_accel: float = {final.theta_accel:.2f}")
    print(f"    iv_floor: float = {final.iv_floor:.2f}")
    print(f"    half_spread_pts: float = {final.half_spread_pts:.1f}")
    print("\n  Stage A generalises only if train and held-out errors above are close.")
    print("  The live trades include panic exits the model can't reproduce — Stage B")
    print("  targets typical disciplined trades.")


if __name__ == "__main__":
    main()
