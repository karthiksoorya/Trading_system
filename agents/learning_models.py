"""Offline learning records; these models never enable trading behavior."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import math
from uuid import uuid4


class KnowledgeStatus(str, Enum):
    REFERENCE = "REFERENCE"
    HYPOTHESIS = "HYPOTHESIS"
    OBSERVED = "OBSERVED"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, kw_only=True)
class KnowledgeEntry:
    category: str
    title: str
    source: str
    learning: str
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    status: KnowledgeStatus = KnowledgeStatus.REFERENCE
    evidence: tuple[str, ...] = ()
    sample_count: int = 0
    confidence: float = 0.0
    live_use_allowed: bool = False
    next_validation: str = ""

    def __post_init__(self):
        for name in ("id", "category", "title", "source", "learning"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty string")
        if not isinstance(self.next_validation, str):
            raise ValueError("next_validation must be a string")
        object.__setattr__(self, "status", KnowledgeStatus(self.status))
        if not isinstance(self.evidence, (tuple, list)) or any(
            not isinstance(item, str) or not item.strip() for item in self.evidence
        ):
            raise ValueError("evidence must be a sequence of nonempty strings")
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if type(self.sample_count) is not int or self.sample_count < 0:
            raise ValueError("sample_count must be a nonnegative integer")
        if (type(self.confidence) not in (float, int)
                or not math.isfinite(self.confidence)
                or not 0 <= self.confidence <= 1):
            raise ValueError("confidence must be finite and between 0 and 1")
        if type(self.live_use_allowed) is not bool:
            raise ValueError("live_use_allowed must be a boolean")
        if self.live_use_allowed and self.status != KnowledgeStatus.VALIDATED:
            raise ValueError("Only validated knowledge can be eligible for live use")
        for value in (self.created_at, self.updated_at):
            if not isinstance(value, datetime) or value.utcoffset() is None:
                raise ValueError("timestamps must be timezone-aware datetimes")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")

    def to_dict(self) -> dict:
        result = asdict(self)
        result.update(created_at=self.created_at.isoformat(),
                      updated_at=self.updated_at.isoformat(),
                      status=self.status.value, evidence=list(self.evidence))
        return result

    @classmethod
    def from_dict(cls, value: dict) -> "KnowledgeEntry":
        data = dict(value)
        for name in ("created_at", "updated_at"):
            data[name] = datetime.fromisoformat(data[name])
        return cls(**data)
