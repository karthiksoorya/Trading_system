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

        i = j  # leg_out is the candidate for the next leg_in

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
        # DBR: leg in is the drop that created the low → include it in distal
        # RBR: leg in is a rally (high values) → exclude it from distal
        pool = (base + [leg_out]) if zone_type == "RBR" else ([leg_in] + base + [leg_out])
        distal = min(c.low for c in pool)
    else:
        # Proximal = lowest body bottom across all base candles
        proximal = min(min(c.open, c.close) for c in base)
        # RBD: leg in is the rally that created the high → include it in distal
        # DBD: leg in is a drop (low values) → exclude it from distal
        pool = (base + [leg_out]) if zone_type == "DBD" else ([leg_in] + base + [leg_out])
        distal = max(c.high for c in pool)

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
            touched = zone.contains_price(c.low)
        else:
            if c.close > zone.distal:       # closed above distal → zone broken
                zone.is_valid = False
                break
            touched = zone.contains_price(c.high)

        if touched and not inside:
            zone.touch_count += 1
            inside = True
        elif not touched:
            inside = False

    return zone
