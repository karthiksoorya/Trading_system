"""
Multi-timeframe confluence check.

A signal is confluent when the entry zone on a lower TF is nested inside
a valid zone of the same class on a higher TF.

  5min zone inside 15min zone inside 60min zone → count = 3  (strongest)
  5min zone inside 60min zone only              → count = 2
  5min zone alone                               → count = 1  (weakest)

Higher-TF zones act as the "curve" (60min) and "trend" (15min).
The lower TF zone is only the entry trigger.
"""

from dataclasses import dataclass, field

from engine.zones import Zone


@dataclass
class ConfluenceResult:
    entry_tf: str
    aligned_tfs: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        """Total number of TFs in agreement (entry TF + aligned higher TFs)."""
        return 1 + len(self.aligned_tfs)

    @property
    def is_confluent(self) -> bool:
        return len(self.aligned_tfs) > 0

    def label(self) -> str:
        """Human-readable label, e.g. '5minute + 15minute + 60minute'."""
        return " + ".join([self.entry_tf] + self.aligned_tfs)


def _zones_overlap(entry_zone: Zone, reference_zone: Zone) -> bool:
    """
    True if entry_zone's proximal price falls inside reference_zone's band,
    and both zones are the same class (demand/supply).
    """
    if entry_zone.zone_class != reference_zone.zone_class:
        return False
    if not reference_zone.is_valid:
        return False
    low  = min(reference_zone.proximal, reference_zone.distal)
    high = max(reference_zone.proximal, reference_zone.distal)
    return low <= entry_zone.proximal <= high


def check_confluence(
    entry_zone: Zone,
    zones_by_tf: dict[str, list[Zone]],
) -> ConfluenceResult:
    """
    Check which higher TFs have a zone that aligns with the entry zone.

    Args:
        entry_zone   : the zone that triggered the signal
        zones_by_tf  : {timeframe: [Zone, ...]} for HIGHER timeframes only

    Returns a ConfluenceResult with aligned_tfs populated.
    """
    result = ConfluenceResult(entry_tf=entry_zone.timeframe)

    for tf, zones in zones_by_tf.items():
        for z in zones:
            if _zones_overlap(entry_zone, z):
                result.aligned_tfs.append(tf)
                break  # one matching zone per TF is enough

    return result
