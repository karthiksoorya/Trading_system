"""
agent/costs.py — Execution cost estimation for options trades.

Based on Zerodha fee structure (most common retail broker).
Adjust the constants below if you use a different broker.

Breakdown per round-trip:
  Brokerage : ₹20/order × 2 orders                      = ₹40 flat
  STT       : 0.05% × sell-side premium value            (on exit only)
  Exchange  : 0.0530% of premium turnover (both legs)    (NSE charges)
  GST       : 18% on (brokerage + exchange charges)
  Stamp     : 0.003% on buy-side value                   (on entry only)
"""

# Brokerage constants (Zerodha; adjust for your broker)
_BROKERAGE_PER_ORDER   = 20.0     # ₹ flat per order (buy + sell = ₹40)
_STT_RATE_SELL         = 0.0005   # 0.05% on sell-side premium value
_EXCHANGE_RATE         = 0.00053  # NSE charges ~0.053% of both-leg turnover
_GST_RATE              = 0.18     # 18% on brokerage + exchange
_STAMP_RATE_BUY        = 0.00003  # 0.003% on buy-side value


def estimate_cost(entry_price: float, exit_price: float, lot_size: int) -> float:
    """
    Estimate total round-trip execution cost in rupees.

    Args:
        entry_price : options premium paid per unit (₹)
        exit_price  : options premium received per unit (₹)
        lot_size    : number of units per lot (e.g. 75 for NIFTY)

    Returns:
        Total cost in ₹ (always positive — subtract from gross P&L)
    """
    brokerage = _BROKERAGE_PER_ORDER * 2
    stt       = _STT_RATE_SELL    * exit_price  * lot_size
    exchange  = _EXCHANGE_RATE    * (entry_price + exit_price) * lot_size
    gst       = _GST_RATE         * (brokerage + exchange)
    stamp     = _STAMP_RATE_BUY   * entry_price * lot_size
    return round(brokerage + stt + exchange + gst + stamp, 2)


def net_options_pnl(entry_price: float, exit_price: float, lot_size: int) -> float:
    """
    Gross P&L minus execution costs.
    gross = (exit - entry) × lot_size   (works for both CE and PE since
    options_exit_price is always the sell price regardless of direction)
    """
    gross = (exit_price - entry_price) * lot_size
    cost  = estimate_cost(entry_price, exit_price, lot_size)
    return round(gross - cost, 2)
