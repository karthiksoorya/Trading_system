"""
backtest/ — offline strategy research for the Nifty trading system.

Design goals:
  1. Reuse the production `engine/` code (zones, candles, signals, boosters,
     confluence) so the backtest exercises the *real* strategy logic, not a
     drifting reimplementation.
  2. Model option premiums (Black-Scholes + VIX-driven IV) and calibrate the
     model against the real option fills already recorded in the trade DB.
  3. Compare the same signal stream across instruments: Nifty futures (1:1),
     deep-ITM options, 1-ITM weekly (current), monthly options.

Nothing in this package touches the live engine, the broker, or Telegram.
It reads cached historical candles from `backtest/data/cache/` only.
"""

import sys as _sys

# The CLIs print a few non-ASCII chars (Rs sign, arrows, check marks). On Windows
# a piped/redirected stdout defaults to cp1252 and those raise UnicodeEncodeError.
# Force UTF-8 so `python -m backtest.*` works from any shell or into a file.
for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

