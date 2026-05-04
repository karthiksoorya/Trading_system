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
        return self.boosters.total >= config.MIN_BOOSTER_SCORE

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
            "overnight_target":   self.overnight_target,
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
) -> Optional[Signal]:
    """
    Build a Signal for a zone.
    Returns None if zone is invalid or booster score < MIN_BOOSTER_SCORE.

    Entry logic (Type 1 — limit at proximal):
      Demand: entry = proximal, SL = distal, target = entry + 2× risk
      Supply: entry = proximal, SL = distal, target = entry − 2× risk
    """
    if not zone.is_valid:
        return None

    entry     = zone.proximal
    stop_loss = zone.distal
    risk      = abs(entry - stop_loss)

    if risk == 0:
        return None

    if zone.zone_class == "demand":
        intraday_target  = entry + 2 * risk
        overnight_target = entry + overnight_target_multiplier * risk
    else:
        intraday_target  = entry - 2 * risk
        overnight_target = entry - overnight_target_multiplier * risk

    boosters = score_zone(
        zone=zone,
        entry=entry,
        stop_loss=stop_loss,
        intraday_target=intraday_target,
        prev_candles=prev_candles,
        overnight_target=overnight_target,
    )

    if boosters.total < config.MIN_BOOSTER_SCORE:
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
    """Score 10 → Type 1 | Score 8–9 → Type 2 | < 8 → no trade."""
    if score >= 10: return 1
    if score >= 8:  return 2
    return 3
