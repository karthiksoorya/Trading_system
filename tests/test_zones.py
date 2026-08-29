"""
Tests for engine/zones.py:
  - detect_bos()       : Break of Structure detection
  - departure_strength : ATR-normalized leg-out strength stored on Zone
"""
import sys
import os
from datetime import datetime

import pytest

# Make the project root importable when running pytest from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brokers.base import Candle
from engine.zones import BOS, Zone, _compute_atr, detect_bos, detect_zones


# ── helpers ───────────────────────────────────────────────────────────────────

_TS = datetime(2026, 1, 1, 9, 15)


def _c(o: float, h: float, l: float, c: float) -> Candle:
    """Shorthand: Candle(open, high, low, close, volume=1000, timestamp=_TS)."""
    return Candle(timestamp=_TS, open=o, high=h, low=l, close=c, volume=1_000)


def _boring() -> Candle:
    """A boring candle: body=1, range=10 → body_ratio=0.1 < 0.5."""
    return _c(100, 105, 95, 101)


def _bull_exciting() -> Candle:
    """Bullish exciting: body=14, range=16 → ratio=0.875."""
    return _c(86, 102, 85, 100)


def _bear_exciting() -> Candle:
    """Bearish exciting: body=14, range=16 → ratio=0.875."""
    return _c(100, 101, 85, 86)


# ── detect_bos ────────────────────────────────────────────────────────────────

class TestDetectBOS:

    def test_bullish_bos_closes_above_highest_high(self):
        # 20 ranging candles (high=110), then one close above 110
        ranging = [_c(100, 110, 90, 105)] * 20
        breakout = _c(108, 115, 107, 112)   # close=112 > highest_high=110
        result = detect_bos(ranging + [breakout])
        assert result is not None
        assert result.direction == "bullish"
        assert result.break_price == 110.0

    def test_bearish_bos_closes_below_lowest_low(self):
        # 20 ranging candles (low=90), then one close below 90
        ranging = [_c(100, 110, 90, 95)] * 20
        breakdown = _c(92, 94, 85, 88)      # close=88 < lowest_low=90
        result = detect_bos(ranging + [breakdown])
        assert result is not None
        assert result.direction == "bearish"
        assert result.break_price == 90.0

    def test_no_bos_when_close_stays_inside_range(self):
        candles = [_c(100, 110, 90, 100)] * 21
        assert detect_bos(candles) is None

    def test_bullish_bos_takes_priority_when_close_above_high(self):
        # Even if low is also extreme, bullish check runs first
        ranging = [_c(100, 110, 90, 105)] * 20
        extreme = _c(108, 120, 50, 115)     # close=115 > 110 → bullish BOS
        result = detect_bos(ranging + [extreme])
        assert result is not None
        assert result.direction == "bullish"

    def test_confirmed_at_matches_latest_candle_timestamp(self):
        ts = datetime(2026, 8, 29, 10, 0)
        ranging = [_c(100, 110, 90, 105)] * 20
        breakout = Candle(timestamp=ts, open=108, high=115, low=107, close=112, volume=1000)
        result = detect_bos(ranging + [breakout])
        assert result is not None
        assert result.confirmed_at == ts

    def test_returns_none_for_single_candle(self):
        assert detect_bos([_c(100, 110, 90, 100)]) is None

    def test_returns_none_for_empty_list(self):
        assert detect_bos([]) is None

    def test_lookback_limits_reference_window(self):
        # First 10 candles have high=120, then 10 more have high=110, then a breakout at 115
        early   = [_c(100, 120, 90, 100)] * 10
        recent  = [_c(100, 110, 90, 100)] * 10
        breakout = _c(108, 118, 107, 115)   # close=115 > 110 but < 120

        # With lookback=10 only the recent candles (high=110) are in reference → BOS
        result_small = detect_bos(early + recent + [breakout], lookback=10)
        assert result_small is not None
        assert result_small.break_price == 110.0

        # With lookback=30 the early candles (high=120) are included → no BOS
        result_large = detect_bos(early + recent + [breakout], lookback=30)
        assert result_large is None


# ── _compute_atr ──────────────────────────────────────────────────────────────

class TestComputeATR:

    def test_returns_zero_for_single_candle(self):
        assert _compute_atr([_c(100, 110, 90, 100)]) == 0.0

    def test_returns_zero_for_empty_list(self):
        assert _compute_atr([]) == 0.0

    def test_simple_atr_two_candles(self):
        # prev_close=100, next: high=110, low=90 → TR=max(20,10,10)=20
        candles = [_c(100, 105, 95, 100), _c(100, 110, 90, 100)]
        atr = _compute_atr(candles, period=14)
        assert atr == 20.0

    def test_atr_uses_period_tail(self):
        # 20 candles with TR=10 each, period=5 → ATR=10
        candles = [_c(100, 105, 95, 100)] * 20
        atr = _compute_atr(candles, period=5)
        assert atr == pytest.approx(10.0)


# ── departure_strength on Zone ────────────────────────────────────────────────

class TestDepartureStrength:
    """
    DBR pattern: bear exciting (leg_in) → boring (base) → bull exciting (leg_out).
    Context candles precede leg_in and drive the ATR calculation.
    """

    def _make_dbr_candles(self, n_context: int):
        """Build a minimal DBR sequence with n_context preceding candles."""
        # Context: ranging boring candles (TR ~ 10 each)
        context = [_c(100, 105, 95, 100)] * n_context
        leg_in  = _bear_exciting()                    # open=100,h=101,l=85,c=86; body=14
        base    = [_c(87, 92, 83, 88)]               # boring; body=1, range=9, ratio=0.11
        leg_out = _c(88, 105, 87, 104)               # bullish exciting; body=16, range=18
        return context + [leg_in] + base + [leg_out]

    def test_departure_strength_positive_with_context(self):
        candles = self._make_dbr_candles(n_context=15)
        zones = detect_zones(candles, "5minute")
        assert len(zones) == 1
        assert zones[0].departure_strength > 0.0

    def test_departure_strength_zero_without_context(self):
        # leg_in at index 0 → context_candles = candles[:0] = []
        candles = self._make_dbr_candles(n_context=0)
        zones = detect_zones(candles, "5minute")
        assert len(zones) == 1
        assert zones[0].departure_strength == 0.0

    def test_departure_strength_scales_with_leg_out_body(self):
        """A bigger leg_out body → higher departure_strength."""
        context = [_c(100, 105, 95, 100)] * 15
        leg_in  = _bear_exciting()
        base    = [_c(87, 92, 83, 88)]

        # Small leg_out: body = 5
        leg_out_small = _c(88, 100, 87, 93)   # body=5, range=13, ratio≈0.38 — may not be exciting
        # Use a clearly exciting small leg_out
        leg_out_small = _c(87, 100, 86, 97)   # body=10, range=14, ratio≈0.71 → exciting

        # Large leg_out: body = 20
        leg_out_large = _c(87, 115, 86, 107)  # body=20, range=29, ratio≈0.69 → exciting

        zones_small = detect_zones(context + [leg_in] + base + [leg_out_small], "5minute")
        zones_large = detect_zones(context + [leg_in] + base + [leg_out_large], "5minute")

        assert len(zones_small) == 1
        assert len(zones_large) == 1
        assert zones_large[0].departure_strength > zones_small[0].departure_strength

    def test_departure_strength_existing_fields_unchanged(self):
        """Adding departure_strength must not alter existing Zone fields."""
        candles = self._make_dbr_candles(n_context=15)
        zones = detect_zones(candles, "5minute")
        z = zones[0]
        assert z.zone_type == "DBR"
        assert z.zone_class == "demand"
        assert z.is_valid is True
        assert z.touch_count == 0
        assert z.proximal > z.distal   # demand: proximal above distal

    def test_zone_dataclass_default_departure_strength(self):
        """Existing code that creates Zone() without departure_strength still works."""
        z = Zone(
            zone_type="RBD", zone_class="supply",
            proximal=100.0, distal=120.0,
            formed_at=_TS, timeframe="15minute",
            leg_in=_bull_exciting(), base_candles=[_boring()], leg_out=_bear_exciting(),
        )
        assert z.departure_strength == 0.0   # default, no crash
