"""
Option pricing model for the backtest.

Real intraday historical option data for expired Nifty weeklies is not freely
available, so we synthesise premiums from the index path + India VIX.

The model has three parts:

  1. Black-Scholes European price (Nifty options are European-style).
  2. An IV model that turns the 30-day VIX into a contract-specific sigma:
        sigma = VIX/100 * term_mult * (1 + skew * |moneyness|) - crush
     where `crush` grows as the index makes the move the option's buyer wanted
     (this is the IV-crush effect the live journal keeps documenting).
  3. Execution frictions (half-spread in premium points) applied by costs.py,
     not here — this module returns the mid premium only.

Free parameters (term_mult, skew, crush_coef) are fitted by calibrate.py
against the real option fills in data/trades_*.db.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

# India 10-year risk-free rate, roughly. Options are so short-dated that this
# barely matters (a 7-day option's rho is negligible).
RISK_FREE_RATE = 0.07

# Nifty options expire at market close on expiry day.
EXPIRY_TIME = (15, 30)

# A trading day is ~6.25h but time value bleeds even overnight. Using calendar
# time slightly under-weights intraday theta; MODEL_PARAMS.theta_accel corrects.
YEAR_DAYS = 365.0


@dataclass(frozen=True)
class ModelParams:
    """
    Defaults below are the calibrate.py fit against 14 disciplined real trades
    (Aug 2026). Fit quality: entry premiums within ~10 pts median, but per-trade
    P&L error is ~₹300-400 and the model runs ~₹150/trade OPTIMISTIC vs reality.
    Treat modelled option P&L as indicative (+/- ₹400), not precise. The futures
    numbers in the backtest carry no such uncertainty.
    """
    term_mult: float = 1.30      # weekly IV vs 30-day VIX
    iv_add: float = 0.0          # additive annualised-vol term (can be negative)
    skew: float = 0.90           # extra IV per unit |log-moneyness| (smile)
    crush_coef: float = 0.70     # IV drop as a fraction of the buyer-favourable move
    crush_cap: float = 0.45      # max fractional IV reduction from crush
    theta_accel: float = 1.10    # multiplies calendar theta to mimic trading-clock decay
    iv_floor: float = 0.05       # sigma never falls below this (annualised)
    half_spread_pts: float = 1.5  # realistic bid/ask half-spread for liquid NIFTY weeklies


DEFAULT_PARAMS = ModelParams()


# ─────────────────────────────────────────────────────────────────────────────
# Black-Scholes
# ─────────────────────────────────────────────────────────────────────────────
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(spot: float, strike: float, t: float, sigma: float, r: float):
    if t <= 0 or sigma <= 0:
        return None, None
    vol_sqrt_t = sigma * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    return d1, d2


def bs_price(spot: float, strike: float, t: float, sigma: float,
             option_type: str, r: float = RISK_FREE_RATE) -> float:
    """Black-Scholes price. `t` in years, `option_type` in {'CE','PE'}."""
    ot = option_type.upper()
    if t <= 0:                      # at/after expiry → intrinsic only
        return max(0.0, spot - strike) if ot == "CE" else max(0.0, strike - spot)
    d1, d2 = _d1_d2(spot, strike, t, sigma, r)
    disc = math.exp(-r * t)
    if ot == "CE":
        return spot * _norm_cdf(d1) - strike * disc * _norm_cdf(d2)
    return strike * disc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def bs_delta(spot: float, strike: float, t: float, sigma: float,
             option_type: str, r: float = RISK_FREE_RATE) -> float:
    ot = option_type.upper()
    if t <= 0:
        if ot == "CE":
            return 1.0 if spot > strike else 0.0
        return -1.0 if spot < strike else 0.0
    d1, _ = _d1_d2(spot, strike, t, sigma, r)
    return _norm_cdf(d1) if ot == "CE" else _norm_cdf(d1) - 1.0


def bs_theta_per_day(spot: float, strike: float, t: float, sigma: float,
                     option_type: str, r: float = RISK_FREE_RATE) -> float:
    """Calendar theta in premium points per day (negative for long options)."""
    ot = option_type.upper()
    d1, d2 = _d1_d2(spot, strike, t, sigma, r)
    if d1 is None:
        return 0.0
    disc = math.exp(-r * t)
    term1 = -(spot * _norm_pdf(d1) * sigma) / (2.0 * math.sqrt(t))
    if ot == "CE":
        term2 = -r * strike * disc * _norm_cdf(d2)
    else:
        term2 = r * strike * disc * _norm_cdf(-d2)
    return (term1 + term2) / YEAR_DAYS


# ─────────────────────────────────────────────────────────────────────────────
# Time to expiry
# ─────────────────────────────────────────────────────────────────────────────
def years_to_expiry(now: datetime, expiry: date) -> float:
    exp_dt = datetime(expiry.year, expiry.month, expiry.day, *EXPIRY_TIME)
    seconds = (exp_dt - now).total_seconds()
    return max(seconds / (YEAR_DAYS * 24 * 3600), 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# IV model
# ─────────────────────────────────────────────────────────────────────────────
def effective_iv(vix: float, spot: float, strike: float, option_type: str,
                 favourable_move_frac: float = 0.0,
                 params: ModelParams = DEFAULT_PARAMS) -> float:
    """
    Turn the index-wide VIX into a sigma for this specific contract.

    favourable_move_frac : how far the index has moved in the option buyer's
        favour since entry, as a fraction of spot (e.g. a CE where Nifty rose
        0.4% → 0.004). Drives the IV-crush term. Pass 0.0 for entry pricing.
    """
    base = max(vix, 1.0) / 100.0 * params.term_mult + params.iv_add
    log_m = abs(math.log(spot / strike)) if strike > 0 and spot > 0 else 0.0
    smile = 1.0 + params.skew * log_m
    crush = min(params.crush_cap, params.crush_coef * max(favourable_move_frac, 0.0) * 100.0)
    sigma = base * smile * (1.0 - crush)
    return max(sigma, params.iv_floor)


# ─────────────────────────────────────────────────────────────────────────────
# Contract + path pricing
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class OptionContract:
    strike: float
    expiry: date
    option_type: str        # 'CE' | 'PE'
    lot_size: int = 65


def price_at(contract: OptionContract, spot: float, vix: float, now: datetime,
             entry_spot: float | None = None,
             params: ModelParams = DEFAULT_PARAMS) -> float:
    """
    Mid premium for `contract` given the current index level, VIX and time.

    entry_spot : the index level when the position was opened. Supplying it
        activates the IV-crush term (IV deflates as the trade's thesis plays
        out). Omit for pricing at entry.
    """
    t = years_to_expiry(now, contract.expiry)
    fav = 0.0
    if entry_spot:
        move = (spot - entry_spot) / entry_spot
        fav = move if contract.option_type.upper() == "CE" else -move
    sigma = effective_iv(vix, spot, contract.strike, contract.option_type, fav, params)
    price = bs_price(spot, contract.strike, t, sigma, contract.option_type)
    if params.theta_accel != 1.0 and t > 0:
        # Pull premium toward intrinsic by the extra decay the trading clock implies.
        intrinsic = (max(0.0, spot - contract.strike)
                     if contract.option_type.upper() == "CE"
                     else max(0.0, contract.strike - spot))
        extra = (params.theta_accel - 1.0) * min(1.0, t * YEAR_DAYS / 7.0)
        price = price - extra * (price - intrinsic)
    return max(price, 0.05)


def fill_price(mid: float, side: str, params: ModelParams = DEFAULT_PARAMS) -> float:
    """Apply half the bid/ask spread: pay up when buying, give up when selling."""
    h = params.half_spread_pts
    return max(0.05, mid + h if side == "buy" else max(0.05, mid - h))


def choose_strike(spot: float, direction: str, itm_steps: int = 1,
                  step: float = 50.0) -> float:
    """
    direction : 'demand' → CE, 'supply' → PE.
    itm_steps : 0 = ATM, 1 = one strike in-the-money (current live behaviour),
                2+ = deeper ITM, negative = OTM.
    """
    atm = round(spot / step) * step
    if direction == "demand":          # CE: ITM strikes are BELOW spot
        return atm - itm_steps * step
    return atm + itm_steps * step      # PE: ITM strikes are ABOVE spot


def next_weekly_expiry(d: date, skip_within_days: int = 2) -> date:
    """Next Tuesday expiry, skipping any expiry <= skip_within_days away."""
    days_ahead = (1 - d.weekday()) % 7          # Tue = weekday 1
    if days_ahead <= skip_within_days - 1:
        days_ahead += 7
    return d + timedelta(days=days_ahead)


def monthly_expiry(d: date) -> date:
    """Last Tuesday of the current month; roll to next month if already past."""
    year, month = d.year, d.month
    for _ in range(2):
        last = date(year, month, 28) + timedelta(days=4)
        last = last - timedelta(days=last.day)          # last day of month
        while last.weekday() != 1:                       # back up to Tuesday
            last -= timedelta(days=1)
        if last > d:
            return last
        month = 1 if month == 12 else month + 1
        year = year + 1 if month == 1 else year
    return last


def with_params(**overrides) -> ModelParams:
    return replace(DEFAULT_PARAMS, **overrides)
