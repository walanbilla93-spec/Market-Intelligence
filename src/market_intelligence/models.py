from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Event:
    source: str
    event_type: str
    title: str
    publisher: str
    source_url: str
    observed_at_utc: str
    available_to_system_at_utc: str
    verification_status: str = "VERIFIED_OFFICIAL_SOURCE"
    scheduled_at_utc: str | None = None
    publisher_time_utc: str | None = None
    first_seen_at_utc: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Observation:
    source: str
    metric: str
    observed_at_utc: str
    available_to_system_at_utc: str
    source_url: str
    status: str = "OK"
    instrument: str | None = None
    value_num: float | None = None
    unit: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

