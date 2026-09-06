"""Pure observational Black-Scholes calculations; never used for trading decisions."""
from dataclasses import dataclass
from datetime import datetime, time
import math

EXPIRY_TIME_IST = time(15, 30)
RISK_FREE_RATE = 0.06
DIVIDEND_YIELD = 0.0

@dataclass(frozen=True)
class GreeksSnapshot:
    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float
    basis: str
    timestamp: str

def _cdf(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))
def _pdf(x): return math.exp(-x*x/2) / math.sqrt(2*math.pi)

def time_to_expiry(expiry: str, as_of: str) -> float | None:
    try:
        end = datetime.fromisoformat(expiry).replace(hour=15, minute=30, second=0, microsecond=0)
        start = datetime.fromisoformat(as_of)
        seconds = (end - start).total_seconds()
        return seconds / (365.0 * 24 * 3600) if seconds > 0 else None
    except (TypeError, ValueError):
        return None

def _price(s, k, t, r, q, sigma, call):
    d1 = (math.log(s/k) + (r-q+sigma*sigma/2)*t) / (sigma*math.sqrt(t))
    d2 = d1 - sigma*math.sqrt(t)
    return (s*math.exp(-q*t)*_cdf(d1) - k*math.exp(-r*t)*_cdf(d2) if call else
            k*math.exp(-r*t)*_cdf(-d2) - s*math.exp(-q*t)*_cdf(-d1))

def implied_volatility(premium, s, k, t, r=RISK_FREE_RATE, q=DIVIDEND_YIELD, call=True):
    if any(x is None for x in (premium,s,k,t)) or min(premium,s,k,t) <= 0: return None
    low, high = 1e-6, 8.0
    if not (_price(s,k,t,r,q,low,call) <= premium <= _price(s,k,t,r,q,high,call)): return None
    for _ in range(100):
        mid=(low+high)/2; value=_price(s,k,t,r,q,mid,call)
        if abs(value-premium) < 1e-8: return mid
        if value < premium: low=mid
        else: high=mid
    return (low+high)/2 if abs(_price(s,k,t,r,q,(low+high)/2,call)-premium) < 1e-5 else None

def calculate_greeks(premium, underlying, strike, expiry, as_of, option_type,
                     *, basis="signal_time_proxy_model") -> GreeksSnapshot | None:
    try:
        s,k=float(underlying),float(strike)
        call=option_type == "CE"
        if option_type not in ("CE","PE"): return None
        t=time_to_expiry(expiry, as_of)
        iv=implied_volatility(float(premium),s,k,t,call=call)
        if iv is None: return None
        d1=(math.log(s/k)+(RISK_FREE_RATE-DIVIDEND_YIELD+iv*iv/2)*t)/(iv*math.sqrt(t)); d2=d1-iv*math.sqrt(t)
        delta=math.exp(-DIVIDEND_YIELD*t)*(_cdf(d1) if call else _cdf(d1)-1)
        gamma=math.exp(-DIVIDEND_YIELD*t)*_pdf(d1)/(s*iv*math.sqrt(t))
        theta=(-(s*math.exp(-DIVIDEND_YIELD*t)*_pdf(d1)*iv/(2*math.sqrt(t)))
                -RISK_FREE_RATE*k*math.exp(-RISK_FREE_RATE*t)*(_cdf(d2) if call else _cdf(-d2))
                +DIVIDEND_YIELD*s*math.exp(-DIVIDEND_YIELD*t)*(_cdf(d1) if call else _cdf(-d1)))/365
        vega=s*math.exp(-DIVIDEND_YIELD*t)*_pdf(d1)*math.sqrt(t)/100
        return GreeksSnapshot(round(iv,6),round(delta,6),round(gamma,8),round(theta,6),round(vega,6),basis,as_of)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError): return None
