"""Immutable values shared by the Fleet explorer's pure projections."""

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple


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
    """One navigation row.

    ``label`` is the text after the fold marker; ``marker`` is the fold glyph the row wears
    (``▾`` open, ``▸`` closed, ``""`` for a leaf or an entry that only selects a page).
    ``parent`` is the identity of the row whose fold hides this one. ``target_ref`` is set
    only on session and worker rows (the rows s/x may act on).
    """
    identity: str
    kind: str
    depth: int
    label: str
    meta: Tuple[Tuple[str, Any], ...] = ()
    health: Health = Health()
    target_ref: Any = None
    marker: str = ""
    parent: str = ""
    open: Optional[bool] = None


@dataclass(frozen=True)
class AttemptRow:
    """One registry attempt (latest row) with the decision Fleet already holds for it.

    ``liveness`` is the live job's own value, or ``unknown`` when no live job carries the
    attempt; the explorer never classifies liveness or outcome itself.
    """
    attempt_id: str
    line: int
    status: str
    slug: str
    harness: str
    worker_type: str = ""
    depth: int = 0
    route_id: Optional[str] = None
    owner_route_id: Optional[str] = None
    route_node: Optional[str] = None
    note: str = ""
    timestamp: str = ""
    parent_sid: str = ""
    liveness: str = "unknown"
    decision: Optional[Mapping[str, Any]] = field(default=None, compare=False)
    meta: Mapping[str, str] = field(default_factory=dict, compare=False)

    @property
    def outcome(self) -> Optional[str]:
        return (self.decision or {}).get("outcome") if self.decision else None


@dataclass(frozen=True)
class RequestTab:
    identity: str
    label: str
    route_id: Optional[str] = None
    cycle_id: Optional[str] = None
    complete: bool = False
    verified: bool = False
    frame_route_id: Optional[str] = None
    owner_route_ids: Tuple[str, ...] = ()
    attempt_ids: Tuple[str, ...] = ()
    started_at: str = ""
    reason: str = ""


@dataclass(frozen=True)
class RequestHeader:
    total: int = 0
    open: int = 0
    latest_failed: int = 0
    review: int = 0


@dataclass(frozen=True)
class LedgerRow:
    route_node: str
    harness: str
    liveness: str
    outcome: Optional[str]
    gate: Optional[str]
    attempt_id: str
    note: str = ""
    attempts: int = 1
    history: Tuple[Tuple[int, str, str], ...] = ()
    request_id: str = ""
    route_id: str = ""
    # Every attempt of the step, oldest first: (attempt_id, note, harness abbr, liveness, outcome).
    trail: Tuple[Tuple[str, str, str, str, Optional[str]], ...] = field(default=(), compare=False)


@dataclass(frozen=True)
class RequestLedger:
    """Everything the ledger column shows for one request tab."""
    request_id: str
    title: str
    rows: Tuple[LedgerRow, ...] = ()
    start_chain: str = ""
    attempt_chain: str = ""
    supervisor_liveness: str = "unknown"
    supervisor_outcome: Optional[str] = None
    gate_passed: int = 0
    gate_total: int = 0
    gate_hold: Tuple[str, ...] = ()
    gate_mismatch: bool = False
    others: Tuple[Tuple[str, str, Optional[str]], ...] = ()
    responsible: str = ""
    # Frame and supervisor attempts of the request, oldest first: (harness abbr, outcome, attempt_id).
    supervision: Tuple[Tuple[str, Optional[str], str], ...] = field(default=(), compare=False)
    owner_count: int = 0


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
    header: RequestHeader = RequestHeader()
    request_ledger: Optional[RequestLedger] = None
    obligations: Optional[Tuple[Mapping[str, Any], ...]] = None
    open_results: Tuple[Tuple[str, str, Optional[str], Optional[str]], ...] = ()
    # Recent event rows of the focused session (registry / peer-ledger projections).
    events: Tuple[Mapping[str, Any], ...] = ()
    # Per-source health the screen shows when a source fails: (name, Health).
    sources: Tuple[Tuple[str, Health], ...] = ()

    def source_health(self, name: str) -> Optional[Health]:
        return dict(self.sources).get(name)


@dataclass(frozen=True)
class Frame:
    lines: Tuple[str, ...]
    regions: Tuple[Tuple[str, Rect], ...]
    hits: Tuple[Hit, ...] = ()
    nav_rows: Tuple[NavRow, ...] = ()
    last_good: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    errors: Tuple[Tuple[str, str], ...] = ()
    geometry: Tuple[int, int] = (0, 0)
    good_at: Tuple[Tuple[str, float], ...] = ()
    page: str = ""
    # First visible navigation row drawn, and how far each scroll slot may go (slot, max offset).
    nav_offset: int = 0
    scroll_max: Tuple[Tuple[str, int], ...] = ()

    def region(self, name: str) -> Optional[Rect]:
        return dict(self.regions).get(name)
