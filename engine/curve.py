"""
Curve analysis — is a zone's price stretched on the bigger picture?

The gap this fills: the engine currently takes a demand/supply zone regardless
of where price sits on the higher-timeframe range. A "fresh-looking" 5-min
demand zone that has formed near the TOP of the last month's range is
structurally weak — price is already extended, there's less room to run, and
it's more likely to be the start of a reversal than a bounce. Symmetrically, a
supply zone near the BOTTOM of the range is weak.

This is a pure, stateless helper — no lookahead, no I/O. Callers (backtest
strategy, later the live scheduler if it proves out) supply the reference
range explicitly.
"""

from __future__ import annotations


def curve_position(price: float, range_low: float, range_high: float) -> float | None:
    """
    Where `price` sits in [range_low, range_high], as a 0..1 fraction.
    0 = at the low of the range, 1 = at the high. None if the range is degenerate.
    """
    if range_high <= range_low:
        return None
    pos = (price - range_low) / (range_high - range_low)
    return max(0.0, min(1.0, pos))


def zone_is_stretched(zone_class: str, zone_price: float,
                      range_low: float, range_high: float,
                      extreme_pct: float = 0.25) -> bool:
    """
    True if this zone sits in the "wrong" part of the range for its class:
      - demand (buy) zone in the top `extreme_pct` of the range -> stretched, skip
      - supply (sell) zone in the bottom `extreme_pct` of the range -> stretched, skip

    `extreme_pct` is a fraction (0.25 = top/bottom quartile). Unknown/degenerate
    range -> False (don't filter on missing data).
    """
    pos = curve_position(zone_price, range_low, range_high)
    if pos is None:
        return False
    if zone_class == "demand":
        return pos > (1.0 - extreme_pct)
    if zone_class == "supply":
        return pos < extreme_pct
    return False
