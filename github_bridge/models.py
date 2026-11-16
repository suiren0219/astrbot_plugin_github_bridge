from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Event:
    key: str
    repo: str
    kind: str
    title: str
    body: str
    url: str
    occurred_at: str
    legacy_keys: tuple[str, ...] = ()


@dataclass
class Observation:
    snapshot: dict[str, Any]
    events: list[Event] = field(default_factory=list)
