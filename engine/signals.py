from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import config
from brokers.base import Candle
from engine.boosters import BoosterResult, score_zone
from engine.confluence import ConfluenceResult
from engine.zones import Zone


@dataclass
class Signal:
    timestamp: datetime
    zone: Zone
    entry: float
    stop_loss: float
    intraday_target: float
    overnight_target: Optional[float]
    boosters: BoosterResult
    entry_type: int                         # 1 | 2 | 3
    confluence: ConfluenceResult = field(
        default_factory=lambda: ConfluenceResult(entry_tf="")
    )

    @property
    def is_tradeable(self) -> bool:
        # BUG 20 fix: read live setting instead of stale module-level config.MIN_BOOSTER_SCORE
        min_score = config.load_settings().get("MIN_BOOSTER_SCORE", config.MIN_BOOSTER_SCORE)
        return self.boosters.total >= min_score

    def as_dict(self) -> dict:
        return {
            "timestamp":          self.timestamp.isoformat(),
            "zone_type":          self.zone.zone_type,
            "zone_class":         self.zone.zone_class,
            "timeframe":          self.zone.timeframe,
            "proximal":           self.zone.proximal,
            "distal":             self.zone.distal,
            "entry":              self.entry,
            "stop_loss":          self.stop_loss,
            "intraday_target":    self.intraday_target,
            "overnight_target":   self.overnight_target,   # informational only — engine always closes intraday
            "entry_type":         self.entry_type,
            "confluence_count":   self.confluence.count,
            "confluence_tfs":     self.confluence.label(),
            **self.boosters.as_dict(),
        }


def generate_signal(
    zone: Zone,
    ltp: float,
    prev_candles: list[Candle],
    confluence: Optional[ConfluenceResult] = None,
    overnight_target_multiplier: float = 3.0,
    opposing_zones: Optional[list[Zone]] = None,
) -> Optional[Signal]:
    """
    Build a Signal for a zone.
    Returns None if zone is invalid or booster score < MIN_BOOSTER_SCORE.

    Entry logic (Type 1 — limit at proximal):
      Demand: entry = proximal, SL = distal - buffer, target = entry + 2× risk
      Supply: entry = proximal, SL = distal + buffer, target = entry − 2× risk

    FIX D: if opposing_zones are supplied, intraday_target is capped just below
    (demand) or above (supply) the nearest opposing zone proximal that sits
    between entry and the 2× target. Avoids setting targets inside resistance.
    """
    if not zone.is_valid:
        return None

    entry = zone.proximal
    if zone.zone_class == "demand":
        stop_loss = zone.distal - config.SL_BUFFER_POINTS
    else:
        stop_loss = zone.distal + config.SL_BUFFER_POINTS
    risk = abs(entry - stop_loss)

    if risk == 0:
        return None

    if zone.zone_class == "demand":
        raw_target       = entry + 2 * risk
        overnight_target = entry + overnight_target_multiplier * risk
        # FIX D: cap target at nearest valid supply zone proximal between entry and raw_target
        if opposing_zones:
            obstacles = [
                z.proximal for z in opposing_zones
                if z.zone_class == "supply" and z.is_valid
                and entry < z.proximal < raw_target
            ]
            if obstacles:
                intraday_target = min(obstacles) - 2   # 2 pts buffer below resistance
            else:
                intraday_target = raw_target
        else:
            intraday_target = raw_target
    else:
        raw_target       = entry - 2 * risk
        overnight_target = entry - overnight_target_multiplier * risk
        # FIX D: cap target at nearest valid demand zone proximal between entry and raw_target
        if opposing_zones:
            obstacles = [
                z.proximal for z in opposing_zones
                if z.zone_class == "demand" and z.is_valid
                and raw_target < z.proximal < entry
            ]
            if obstacles:
                intraday_target = max(obstacles) + 2   # 2 pts buffer above support
            else:
                intraday_target = raw_target
        else:
            intraday_target = raw_target

    boosters = score_zone(
        zone=zone,
        entry=entry,
        stop_loss=stop_loss,
        intraday_target=intraday_target,
        prev_candles=prev_candles,
        overnight_target=overnight_target,
    )

    min_score = config.load_settings().get("MIN_BOOSTER_SCORE", config.MIN_BOOSTER_SCORE)
    if boosters.total < min_score:
        return None

    if confluence is None:
        confluence = ConfluenceResult(entry_tf=zone.timeframe)

    return Signal(
        timestamp=datetime.now(),
        zone=zone,
        entry=entry,
        stop_loss=stop_loss,
        intraday_target=intraday_target,
        overnight_target=overnight_target,
        boosters=boosters,
        entry_type=_decide_entry_type(boosters.total),
        confluence=confluence,
    )


def _decide_entry_type(score: float) -> int:
    """
    Score 10    → Type 1 (strongest — all boosters near max)
    Score 8–9   → Type 2 (good setup)
    Score < 8   → Type 3 (below threshold — only reached if MIN_BOOSTER_SCORE
                  was lowered below 8 via dashboard; treat as low-confidence)
    FIX C: overnight_target is informational only — the engine always closes
    intraday at 15:20. No code path switches to overnight_target automatically.
    """
    if score >= 10: return 1
    if score >= 8:  return 2
    return 3
