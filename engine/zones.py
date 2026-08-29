from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from brokers.base import Candle
from engine.candle import is_boring, is_exciting


@dataclass
class Zone:
    zone_type: str      # "DBR" | "RBR" | "RBD" | "DBD"
    zone_class: str     # "demand" | "supply"
    proximal: float     # line closest to current price
    distal: float       # line farthest from current price
    formed_at: datetime
    timeframe: str
    leg_in: Candle
    base_candles: list[Candle]
    leg_out: Candle
    touch_count: int = 0
    is_valid: bool = True
    # ATR-normalized departure strength: leg_out body / 14-period ATR of preceding candles.
    # Values > 1.5 indicate a strong impulsive departure from the base.
    # 0.0 when context candles are unavailable (zone at start of series).
    departure_strength: float = 0.0
    # ATR-normalized base compression: base price range / 14-period ATR.
    # Lower = tighter coil before the explosion = higher quality zone.
    # < 0.5 = very compressed, > 1.5 = loose/sloppy base.
    # 0.0 when context candles are unavailable.
    base_compression: float = 0.0

    @property
    def base_length(self) -> int:
        return len(self.base_candles)

    def contains_price(self, price: float) -> bool:
        low, high = sorted([self.proximal, self.distal])
        return low <= price <= high

    def is_fresh(self) -> bool:
        return self.touch_count == 0


# ── ATR helper ────────────────────────────────────────────────────────────────

def _compute_atr(candles: list[Candle], period: int = 14) -> float:
    """Average True Range over the last `period` candles.

    Requires at least 2 candles (first candle has no prev_close for True Range).
    Returns 0.0 if insufficient data.
    """
    if len(candles) < 2:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(candles)):
        tr = max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - candles[i - 1].close),
            abs(candles[i].low  - candles[i - 1].close),
        )
        trs.append(tr)
    tail = trs[-period:]
    return sum(tail) / len(tail) if tail else 0.0


# ── Break of Structure ────────────────────────────────────────────────────────

@dataclass
class BOS:
    """A confirmed Break of Structure in a candle series."""
    direction: str      # "bullish" | "bearish"
    break_price: float  # the level that was breached (swing high or swing low)
    confirmed_at: datetime


def detect_bos(candles: list[Candle], lookback: int = 20) -> Optional[BOS]:
    """Detect the most recent Break of Structure in a candle series.

    Bullish BOS : latest candle closes above the highest high of the prior
                  `lookback` candles — structure has shifted upward.
    Bearish BOS : latest candle closes below the lowest low of the prior
                  `lookback` candles — structure has shifted downward.

    Returns None when fewer than 2 candles are supplied or no break is detected.
    """
    if len(candles) < 2:
        return None
    latest = candles[-1]
    # Reference window: up to `lookback` candles immediately before the last
    start = max(0, len(candles) - lookback - 1)
    reference = candles[start:-1]
    if not reference:
        return None
    highest = max(c.high for c in reference)
    lowest  = min(c.low  for c in reference)
    if latest.close > highest:
        return BOS(direction="bullish", break_price=highest, confirmed_at=latest.timestamp)
    if latest.close < lowest:
        return BOS(direction="bearish", break_price=lowest,  confirmed_at=latest.timestamp)
    return None


# ── Zone detection ────────────────────────────────────────────────────────────

def detect_zones(candles: list[Candle], timeframe: str) -> list[Zone]:
    """
    Scan a candle list for DBR / RBR / RBD / DBD patterns.

    Pattern: 1 exciting (leg in) → 1+ boring (base) → 1 exciting (leg out)
    Leg in and leg out must always be exciting; base must always be boring.

    Each Zone is annotated with departure_strength (leg_out body / ATR of the
    candles that preceded the leg_in), giving a volatility-normalized measure
    of how impulsively price left the base.
    """
    zones: list[Zone] = []
    i = 0

    while i < len(candles):
        if not is_exciting(candles[i]):
            i += 1
            continue

        leg_in = candles[i]
        j = i + 1
        base: list[Candle] = []

        while j < len(candles) and is_boring(candles[j]):
            base.append(candles[j])
            j += 1

        if not base or j >= len(candles) or not is_exciting(candles[j]):
            i += 1
            continue

        leg_out = candles[j]
        # Pass all candles before the leg_in as context for ATR computation
        zone = _build_zone(leg_in, base, leg_out, timeframe, candles[:i])
        if zone:
            zones.append(zone)

        # BUG 9 fix: advance past leg_out to prevent overlapping zones that share
        # a candle. Previously i = j allowed leg_out to immediately become the next
        # leg_in, producing zones with a shared boundary candle.
        # Starting at j + 1 means we look for a completely fresh pattern next.
        i = j + 1

    return zones


def _build_zone(
    leg_in: Candle,
    base: list[Candle],
    leg_out: Candle,
    timeframe: str,
    context_candles: Optional[list[Candle]] = None,
) -> Optional[Zone]:
    li_bull = leg_in.is_bullish
    lo_bull = leg_out.is_bullish

    if   not li_bull and lo_bull:  zone_type = "DBR"   # demand
    elif li_bull     and lo_bull:  zone_type = "RBR"   # demand
    elif li_bull     and not lo_bull: zone_type = "RBD" # supply
    else:                          zone_type = "DBD"   # supply

    is_demand = zone_type in ("DBR", "RBR")

    if is_demand:
        # Proximal = highest body top across all base candles
        proximal = max(max(c.open, c.close) for c in base)
        # BUG 8 fix: for both DBR and RBR, leg_in establishes the zone's low boundary
        # so it must always be included in the distal pool.
        # Previous code excluded leg_in for RBR, giving a higher distal (SL too tight).
        distal = min(c.low for c in [leg_in] + base + [leg_out])
    else:
        # Proximal = lowest body bottom across all base candles
        proximal = min(min(c.open, c.close) for c in base)
        # BUG 8 fix: for both RBD and DBD, leg_in establishes the zone's high boundary
        # so it must always be included in the distal pool.
        distal = max(c.high for c in [leg_in] + base + [leg_out])

    # ATR-normalized quality metrics — both reuse the same ATR computation
    atr = _compute_atr(context_candles) if context_candles else 0.0
    departure_strength = round(leg_out.body / atr, 2) if atr > 0 else 0.0
    base_range = max(c.high for c in base) - min(c.low for c in base)
    base_compression = round(base_range / atr, 2) if atr > 0 else 0.0

    return Zone(
        zone_type=zone_type,
        zone_class="demand" if is_demand else "supply",
        proximal=proximal,
        distal=distal,
        formed_at=leg_out.timestamp,
        timeframe=timeframe,
        leg_in=leg_in,
        base_candles=list(base),
        leg_out=leg_out,
        departure_strength=departure_strength,
        base_compression=base_compression,
    )


def update_zone_state(zone: Zone, candles_after: list[Candle]) -> Zone:
    """
    Walk candles that occurred after the zone formed.
    - Increments touch_count each time price enters the zone from outside.
    - Marks is_valid=False if price closes beyond the distal (zone broken).
    """
    inside = False

    for c in candles_after:
        if not zone.is_valid:
            break

        if zone.zone_class == "demand":
            if c.close < zone.distal:       # closed below distal → zone broken
                zone.is_valid = False
                break
            # BUG 15 fix: count touch if wick OR body enters the zone, not just wick.
            # A candle whose body is fully inside the zone but whose low doesn't reach
            # proximal was previously not counted, under-counting touches.
            touched = zone.contains_price(c.low) or zone.contains_price(min(c.open, c.close))
        else:
            if c.close > zone.distal:       # closed above distal → zone broken
                zone.is_valid = False
                break
            # BUG 15 fix: same for supply — check high wick OR body top
            touched = zone.contains_price(c.high) or zone.contains_price(max(c.open, c.close))

        if touched and not inside:
            zone.touch_count += 1
            inside = True
        elif not touched:
            inside = False

    return zone
