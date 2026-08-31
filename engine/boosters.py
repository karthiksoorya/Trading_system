from dataclasses import dataclass

import config
from brokers.base import Candle
from engine.zones import Zone


@dataclass
class BoosterResult:
    freshness:  float   # 0 | 1.5 | 3
    strength:   float   # 0 | 1   | 2
    time_score: float   # 0 | 1   | 2
    rr_score:   float   # 0 | 1.5 | 3

    @property
    def total(self) -> float:
        return self.freshness + self.strength + self.time_score + self.rr_score

    def as_dict(self) -> dict:
        return {
            "freshness":   self.freshness,
            "strength":    self.strength,
            "time_score":  self.time_score,
            "rr_score":    self.rr_score,
            "total":       self.total,
        }


# ── Individual scorers ────────────────────────────────────────────────────

def score_freshness(zone: Zone) -> float:
    """
    Fresh (0 touches) = 3
    One touch          = 1.5
    More than 1 touch  = 0
    """
    if zone.touch_count == 0:  return 3.0
    if zone.touch_count == 1:  return 1.5
    return 0.0


def score_strength(leg_out: Candle, prev_candles: list[Candle], zone: "Zone | None" = None) -> float:
    """
    Gap away from base OR explosive move (body >> avg) = 2
    Strong (exciting candle, no gap)                   = 1
    Weak                                               = 0

    BUG 14 fix: gap is detected by comparing leg_out.open against the last base
    candle of the zone (zone.base_candles[-1]). Previously it compared against
    prev_candles[-1] which is the most recent live candle — almost always non-zero,
    giving every historical leg_out a spurious gap score of 2.0.

    Falls back to prev_candles[-1] if zone or base_candles are unavailable.
    Explosive is body > 2× average body of prev_candles (unchanged).
    """
    if not prev_candles:
        return 1.0  # default to strong if no context

    # Use the candle immediately before leg_out (last base candle) for gap detection
    if zone is not None and zone.base_candles:
        candle_before_legout = zone.base_candles[-1]
    else:
        candle_before_legout = prev_candles[-1]  # fallback (old behaviour)

    # Gap threshold: must exceed GAP_MIN_POINTS OR 10% of zone ATR, whichever is larger.
    # The old check (> 0) fired on any float difference — effectively always true.
    atr = getattr(zone, "atr", 0.0) if zone is not None else 0.0
    gap_min = config.load_settings().get("GAP_MIN_POINTS", config.GAP_MIN_POINTS)
    gap_threshold = max(gap_min, 0.1 * atr) if atr > 0 else gap_min
    gap = abs(leg_out.open - candle_before_legout.close) > gap_threshold

    avg_body = sum(c.body for c in prev_candles) / len(prev_candles)
    explosive = leg_out.body > 2 * avg_body if avg_body > 0 else False

    if gap or explosive:  return 2.0
    if leg_out.body > 0:  return 1.0
    return 0.0


def score_time(zone: Zone) -> float:
    """
    1–3 base candles = 2
    4–6 base candles = 1
    >6  base candles = 0
    """
    n = zone.base_length
    if n <= 3:  return 2.0
    if n <= 6:  return 1.0
    return 0.0


def score_rr(
    entry: float,
    stop_loss: float,
    intraday_target: float,
    overnight_target: float | None = None,
) -> float:
    """
    ON ≥ 1:3 AND ID ≥ 1:2  = 3
    ON ≥ 1:2 AND ID ≥ 1:1.5 = 1.5
    else                     = 0

    If overnight_target is None, only intraday R:R is evaluated:
      ID ≥ 1:2 = 3, ID ≥ 1:1.5 = 1.5, else 0.
    """
    risk = abs(entry - stop_loss)
    if risk == 0:
        return 0.0

    id_rr = abs(intraday_target - entry) / risk

    if overnight_target is not None:
        on_rr = abs(overnight_target - entry) / risk
        if on_rr >= 3 and id_rr >= 2:   return 3.0
        if on_rr >= 2 and id_rr >= 1.5: return 1.5
        return 0.0
    else:
        if id_rr >= 2:   return 3.0
        if id_rr >= 1.5: return 1.5
        return 0.0


# ── Composite scorer ──────────────────────────────────────────────────────

def score_zone(
    zone: Zone,
    entry: float,
    stop_loss: float,
    intraday_target: float,
    prev_candles: list[Candle],
    overnight_target: float | None = None,
) -> BoosterResult:
    return BoosterResult(
        freshness=  score_freshness(zone),
        strength=   score_strength(zone.leg_out, prev_candles, zone=zone),
        time_score= score_time(zone),
        rr_score=   score_rr(entry, stop_loss, intraday_target, overnight_target),
    )
