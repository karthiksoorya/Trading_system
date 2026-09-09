"""
Download NSE F&O daily bhavcopy and cache real NIFTY option/future prices.

This is REAL option data — daily resolution (open/high/low/close/settle + OI per
contract), free, no account. It does NOT replace minute-level vendor data, but it
lets us check how wrong the synthetic `option_model.py` is against thousands of
real premium points instead of the ~14 live trades `calibrate.py` uses.

Two source formats, handled transparently:
  • new "UDiFF" csv   (2024-07-08 onwards)
      https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_<YYYYMMDD>_F_0000.csv.zip
  • old derivatives csv (before that)
      https://nsearchives.nseindia.com/content/historical/DERIVATIVES/<YYYY>/<MON>/fo<DDMON YYYY>bhav.csv.zip

Run:
    python -m backtest.data.nse_bhavcopy --years 3
    python -m backtest.data.nse_bhavcopy --start 2024-01-01 --end 2024-12-31
    python -m backtest.data.nse_bhavcopy --check           # one day, prove access

Output (parquet, in backtest/data/cache/):
    nifty_options_daily.parquet   date, expiry, strike, option_type,
                                  open, high, low, close, settle,
                                  oi, chg_oi, volume, underlying
    nifty_fut_daily.parquet       date, expiry, open, high, low, close, settle,
                                  oi, chg_oi, volume, underlying

Raw zips are kept in cache/nse_fo_zips/ so re-runs only fetch missing days.
"""

from __future__ import annotations

import argparse
import io
import sys
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from . import CACHE_DIR

ZIP_DIR = CACHE_DIR / "nse_fo_zips"
ZIP_DIR.mkdir(exist_ok=True)

OPTIONS_PARQUET = CACHE_DIR / "nifty_options_daily.parquet"
FUT_PARQUET = CACHE_DIR / "nifty_fut_daily.parquet"

# The day the UDiFF format took over. Try it first on/after this date.
UDIFF_CUTOVER = date(2024, 7, 8)

_MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
        "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/all-reports-derivatives",
}

REQUEST_PAUSE = 0.6          # be polite to the archive host


# ─────────────────────────────────────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────────────────────────────────────
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    try:                                    # warm up — pick up the cookie wall
        s.get("https://www.nseindia.com", timeout=10)
    except requests.RequestException:
        pass
    return s


def _udiff_url(d: date) -> str:
    return ("https://nsearchives.nseindia.com/content/fo/"
            f"BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")


def _legacy_url(d: date) -> str:
    mon = _MON[d.month - 1]
    return ("https://nsearchives.nseindia.com/content/historical/DERIVATIVES/"
            f"{d.year}/{mon}/fo{d.day:02d}{mon}{d.year}bhav.csv.zip")


def _fetch(sess: requests.Session, url: str) -> bytes | None:
    for attempt in range(3):
        try:
            r = sess.get(url, timeout=20)
            if r.status_code == 404:
                return None                 # weekend / holiday / wrong format
            r.raise_for_status()
            if r.content[:2] != b"PK":      # not a zip — usually an HTML block page
                raise requests.RequestException("response is not a zip")
            return r.content
        except requests.RequestException as e:
            wait = 2 ** attempt
            print(f"    {type(e).__name__} on {url.rsplit('/', 1)[-1]}, retry in {wait}s")
            time.sleep(wait)
    return None


def _zip_path(d: date) -> Path:
    return ZIP_DIR / f"fo_{d:%Y%m%d}.csv.zip"


def _get_day_zip(sess: requests.Session, d: date, force: bool = False) -> bytes | None:
    """Return the raw zip bytes for one trading day, from disk cache if present."""
    cached = _zip_path(d)
    if cached.exists() and not force:
        return cached.read_bytes()

    urls = ([_udiff_url(d), _legacy_url(d)] if d >= UDIFF_CUTOVER
            else [_legacy_url(d), _udiff_url(d)])
    for url in urls:
        raw = _fetch(sess, url)
        if raw:
            cached.write_bytes(raw)
            return raw
        time.sleep(REQUEST_PAUSE)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────────────────────
def _read_csv_from_zip(raw: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        with z.open(name) as f:
            return pd.read_csv(f)


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _normalise(df: pd.DataFrame, trad_dt: date) -> pd.DataFrame:
    """Map either bhavcopy layout to one schema, NIFTY index contracts only."""
    cols = set(df.columns)

    if "FinInstrmTp" in cols:                                  # UDiFF
        df = df.rename(columns=str.strip)
        df = df[df["TckrSymb"].astype(str).str.strip() == "NIFTY"]
        df = df[df["FinInstrmTp"].isin(["IDO", "IDF"])]
        out = pd.DataFrame({
            "date":        trad_dt,
            "expiry":      pd.to_datetime(df["XpryDt"], errors="coerce").dt.date,
            "strike":      _num(df["StrkPric"]),
            "option_type": df["OptnTp"].astype(str).str.strip().replace({"nan": "FUT", "": "FUT"}),
            "open":        _num(df["OpnPric"]),
            "high":        _num(df["HghPric"]),
            "low":         _num(df["LwPric"]),
            "close":       _num(df["ClsPric"]),
            "settle":      _num(df["SttlmPric"]),
            "oi":          _num(df["OpnIntrst"]),
            "chg_oi":      _num(df["ChngInOpnIntrst"]),
            "volume":      _num(df["TtlTradgVol"]),
            "underlying":  _num(df["UndrlygPric"]),
        })
    elif "INSTRUMENT" in cols:                                 # legacy
        df = df.rename(columns=lambda c: c.strip())
        df = df[df["SYMBOL"].astype(str).str.strip() == "NIFTY"]
        df = df[df["INSTRUMENT"].isin(["OPTIDX", "FUTIDX"])]
        ot = df["OPTION_TYP"].astype(str).str.strip().replace({"XX": "FUT", "": "FUT", "nan": "FUT"})
        out = pd.DataFrame({
            "date":        trad_dt,
            "expiry":      pd.to_datetime(df["EXPIRY_DT"], errors="coerce", format="mixed").dt.date,
            "strike":      _num(df["STRIKE_PR"]),
            "option_type": ot,
            "open":        _num(df["OPEN"]),
            "high":        _num(df["HIGH"]),
            "low":         _num(df["LOW"]),
            "close":       _num(df["CLOSE"]),
            "settle":      _num(df["SETTLE_PR"]),
            "oi":          _num(df["OPEN_INT"]),
            "chg_oi":      _num(df["CHG_IN_OI"]),
            "volume":      _num(df["CONTRACTS"]),
            "underlying":  pd.NA,
        })
    else:
        raise ValueError(f"unrecognised bhavcopy columns: {sorted(cols)[:8]}…")

    out["date"] = pd.to_datetime(out["date"])
    return out.dropna(subset=["expiry", "close"]).reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# Build
# ─────────────────────────────────────────────────────────────────────────────
def _weekdays(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:                  # Mon-Fri; holidays just 404 and skip
            yield d
        d += timedelta(days=1)


def build(start: date, end: date, force: bool = False) -> None:
    sess = _session()
    frames: list[pd.DataFrame] = []
    got = missing = 0

    for d in _weekdays(start, end):
        raw = _get_day_zip(sess, d, force=force)
        if raw is None:
            missing += 1
            continue
        try:
            frames.append(_normalise(_read_csv_from_zip(raw), d))
            got += 1
        except (ValueError, StopIteration, zipfile.BadZipFile) as e:
            print(f"  {d}: parse failed — {e}")
            missing += 1
        if got % 20 == 0 and got:
            print(f"  …{got} days parsed ({d})")
        time.sleep(REQUEST_PAUSE)

    if not frames:
        sys.exit("No bhavcopy days downloaded — check network / NSE access "
                 "(try --check), or the date range hits only holidays.")

    allrows = pd.concat(frames, ignore_index=True)
    opts = allrows[allrows["option_type"].isin(["CE", "PE"])].copy()
    futs = allrows[allrows["option_type"] == "FUT"].drop(columns=["strike"]).copy()

    _merge_save(opts, OPTIONS_PARQUET, ["date", "expiry", "strike", "option_type"])
    _merge_save(futs, FUT_PARQUET, ["date", "expiry"])

    span = f"{allrows['date'].min():%Y-%m-%d} → {allrows['date'].max():%Y-%m-%d}"
    print(f"\nDone. {got} trading days ({span}), {missing} skipped (holiday/missing).")
    print(f"  {OPTIONS_PARQUET.name}: {len(opts):,} option rows")
    print(f"  {FUT_PARQUET.name}: {len(futs):,} future rows")


def _merge_save(df: pd.DataFrame, path: Path, keys: list[str]) -> None:
    if path.exists():
        old = pd.read_parquet(path)
        df = pd.concat([old, df], ignore_index=True)
    df = (df.sort_values("date")
            .drop_duplicates(subset=keys, keep="last")
            .reset_index(drop=True))
    df.to_parquet(path, index=False)


# ─────────────────────────────────────────────────────────────────────────────
# Loaders (used by validate_option_model.py and any future real-premium backtest)
# ─────────────────────────────────────────────────────────────────────────────
def load_options_daily() -> pd.DataFrame:
    if not OPTIONS_PARQUET.exists():
        raise FileNotFoundError(
            f"{OPTIONS_PARQUET.name} missing — run:  python -m backtest.data.nse_bhavcopy --years 3")
    df = pd.read_parquet(OPTIONS_PARQUET)
    df["date"] = pd.to_datetime(df["date"])
    df["expiry"] = pd.to_datetime(df["expiry"])
    return df


def load_fut_daily() -> pd.DataFrame:
    if not FUT_PARQUET.exists():
        raise FileNotFoundError(
            f"{FUT_PARQUET.name} missing — run:  python -m backtest.data.nse_bhavcopy --years 3")
    df = pd.read_parquet(FUT_PARQUET)
    df["date"] = pd.to_datetime(df["date"])
    df["expiry"] = pd.to_datetime(df["expiry"])
    return df


def _vix_by_day() -> pd.Series:
    from .candles import load_raw
    try:
        v = load_raw("vix", "day")
    except FileNotFoundError:
        v = load_raw("vix", "5minute")
    v = v.copy()
    v["d"] = pd.to_datetime(v["date"]).dt.date
    return v.groupby("d")["close"].last()


def _spot_close_by_day() -> pd.Series:
    from .candles import load_timeframe
    df = load_timeframe("nifty", "day", "5minute").copy()
    df["d"] = pd.to_datetime(df["date"]).dt.date
    return df.groupby("d")["close"].last()


def liquid_marks(max_dte: int = 10, min_dte: int = 2, mny: float = 0.04,
                 min_oi: int = 1000, min_vol: int = 1000) -> pd.DataFrame:
    """
    One row per liquid near-the-money NIFTY weekly contract per trading day, with
    the observable state needed to price it: spot (bhavcopy underlying, else index
    close), India VIX (day close) and the REAL premium = that day's CLOSE.

    SETTLE is not used — on expiry day NSE writes the underlying settlement value
    into the option SETTLE column. `min_dte` (default 2, matching the live expiry
    buffer) keeps us clear of that anyway.

    Shared by validate_option_model.py and calibrate.py (bhavcopy stage).
    """
    import numpy as np

    opt = load_options_daily()
    vix_d = _vix_by_day()
    spot_d = _spot_close_by_day()

    opt = opt.copy()
    opt["d"] = opt["date"].dt.date
    opt["exp_d"] = opt["expiry"].dt.date
    opt["dte"] = (opt["expiry"] - opt["date"]).dt.days
    opt["spot"] = opt.apply(
        lambda r: r["underlying"] if pd.notna(r["underlying"]) else spot_d.get(r["d"], np.nan),
        axis=1,
    )
    opt["vix"] = opt["d"].map(vix_d)

    max_prem = opt["spot"] * mny + 60
    keep = opt[
        opt["spot"].notna() & opt["vix"].notna()
        & (opt["dte"] >= max(min_dte, 1)) & (opt["dte"] <= max_dte)
        & (opt["oi"] >= min_oi) & (opt["volume"] >= min_vol)
        & (opt["close"] > 0.5) & (opt["close"] < max_prem)
        & ((opt["strike"] / opt["spot"] - 1).abs() <= mny)
    ].copy()

    keep["real"] = keep["close"]
    cols = ["date", "d", "exp_d", "dte", "strike", "option_type",
            "spot", "vix", "real", "oi", "volume"]
    return keep[cols].reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch NSE F&O bhavcopy for the backtest.")
    ap.add_argument("--years", type=float, default=3.0, help="how far back from today (default 3)")
    ap.add_argument("--start", type=lambda s: date.fromisoformat(s), help="explicit start YYYY-MM-DD")
    ap.add_argument("--end", type=lambda s: date.fromisoformat(s), help="explicit end YYYY-MM-DD")
    ap.add_argument("--force", action="store_true", help="re-download even if the zip is cached")
    ap.add_argument("--check", action="store_true", help="fetch only the most recent weekday, prove access")
    args = ap.parse_args()

    end = args.end or date.today()
    start = args.start or (end - timedelta(days=int(args.years * 365)))

    if args.check:
        d = end
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        sess = _session()
        raw = _get_day_zip(sess, d, force=True)
        if not raw:
            sys.exit(f"[X] could not fetch bhavcopy for {d} — NSE may be blocking, or {d} is a holiday.")
        df = _normalise(_read_csv_from_zip(raw), d)
        n_opt = (df["option_type"].isin(["CE", "PE"])).sum()
        print(f"[ok] {d}: {len(df):,} NIFTY index rows ({n_opt:,} options). Access works.")
        return

    print(f"Fetching NSE F&O bhavcopy {start} → {end}\n")
    build(start, end, force=args.force)


if __name__ == "__main__":
    main()
