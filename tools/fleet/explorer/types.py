"""Immutable values shared by the Fleet explorer's pure projections."""

from dataclasses import dataclass
from typing import Any, Optional, Tuple


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h


@dataclass(frozen=True)
class Hit:
    rect: Rect
    identity: Any
    action: str = "select"


@dataclass(frozen=True)
class FocusKey:
    harness: str
    sid: str
    request_id: str = ""
    stage_id: str = ""
    attempt_id: str = ""


@dataclass(frozen=True)
class Health:
    state: str = "unknown"
    last_success_at: Optional[float] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class NavRow:
    identity: str
    kind: str
    depth: int
    label: str
    meta: Tuple[Tuple[str, Any], ...] = ()
    health: Health = Health()
    target_ref: Any = None


@dataclass(frozen=True)
class RequestTab:
    identity: str
    label: str
    route_id: Optional[str] = None
    cycle_id: Optional[str] = None
    complete: bool = False
    verified: bool = False


@dataclass(frozen=True)
class LedgerRow:
    route_node: str
    harness: str
    liveness: str
    outcome: Optional[str]
    gate: Optional[str]
    attempt_id: str
    note: str = ""


@dataclass(frozen=True)
class FocusRequest:
    key: FocusKey
    epoch: int
    snapshot_generation: int
    expanded_sessions: Tuple[str, ...] = ()


@dataclass(frozen=True)
class FocusResult:
    key: FocusKey
    epoch: int
    snapshot_generation: int
    tabs: Tuple[RequestTab, ...] = ()
    ledger: Tuple[LedgerRow, ...] = ()
    output: Tuple[str, ...] = ()
    overview: Tuple[str, ...] = ()
    health: Health = Health()


@dataclass(frozen=True)
class Frame:
    lines: Tuple[str, ...]
    regions: Tuple[Tuple[str, Rect], ...]
    hits: Tuple[Hit, ...] = ()
    nav_rows: Tuple[NavRow, ...] = ()
    last_good: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    errors: Tuple[Tuple[str, str], ...] = ()
    geometry: Tuple[int, int] = (0, 0)

    def region(self, name: str) -> Optional[Rect]:
        return dict(self.regions).get(name)
