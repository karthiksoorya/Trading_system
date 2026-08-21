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

    @property
    def base_length(self) -> int:
        return len(self.base_candles)

    def contains_price(self, price: float) -> bool:
        low, high = sorted([self.proximal, self.distal])
        return low <= price <= high

    def is_fresh(self) -> bool:
        return self.touch_count == 0


def detect_zones(candles: list[Candle], timeframe: str) -> list[Zone]:
    """
    Scan a candle list for DBR / RBR / RBD / DBD patterns.

    Pattern: 1 exciting (leg in) → 1+ boring (base) → 1 exciting (leg out)
    Leg in and leg out must always be exciting; base must always be boring.
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
        zone = _build_zone(leg_in, base, leg_out, timeframe)
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
