"""Pure request-tab reconstruction from registry, route, and cycle evidence.

A request is one artifact cycle. The only evidence that joins attempts to a tab is
the owner route's ``parent_cycle_id`` equal to a cycle record's ``cycle_id`` whose
``route_id`` is the frame route; frame rows join through that same ``route_id``.
Slug or campaign similarity never joins anything.
"""
import re
from collections.abc import Mapping

from .ledger import GATE_GLYPH, outcome_text
from .output import cell_width, clip
from .types import AttemptRow, RequestHeader, RequestTab

OPEN_STATUSES = frozenset(("open", "running"))
UNVERIFIED_LABEL = "연결 미확인"
_HASH_SUFFIX = re.compile(r"-[0-9a-f]{10}$")
_SLUG_ROLE = re.compile(r"-(?:leg\d+-)?(?:frame|owner)$")


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _index(items, key):
    if isinstance(items, Mapping):
        return dict(items)
    return {_get(item, key): item for item in (items or ()) if _get(item, key)}


def is_open(status):
    return str(status or "") in OPEN_STATUSES


def request_name(title="", slug=""):
    """The request's own name: the cycle title, else the leading words of a framed slug."""
    title = str(title or "").strip()
    if title and not (title.startswith("framed-") or _HASH_SUFFIX.search(title)):
        return title
    stem = str(title or slug or "").strip()
    stem = _SLUG_ROLE.sub("", stem)
    stem = _HASH_SUFFIX.sub("", stem)
    if stem.startswith("framed-"):
        stem = stem[len("framed-"):]
    words = [word for word in stem.split("-") if word]
    return "-".join(words[:2]) if words else UNVERIFIED_LABEL


def short_label(name):
    """Tab-bar name of an inactive tab: the first dash word, or two when that is a stub."""
    words = str(name).split("-")
    if len(words) <= 1:
        return str(name)
    return words[0] if len(words[0]) >= 2 else "-".join(words[:2])


def _verified(cycle, parent_cycle_id):
    if not isinstance(cycle, Mapping):
        return False
    if _get(cycle, "cycle_id") != parent_cycle_id or not _get(cycle, "route_id"):
        return False
    capability = _get(cycle, "route_capability")
    return capability in (None, "", "route-frame")


class _Group:
    def __init__(self, key):
        self.key = key
        self.cycle = None
        self.rows = []

    def add(self, row):
        self.rows.append(row)


def build_tabs(attempts=(), routes=(), cycles=()):
    """Join attempts to requests; return tabs in start order.

    ``routes`` and ``cycles`` are mappings (or iterables of records) keyed by
    ``route_id`` / ``cycle_id``. A missing or unreadable record simply never proves a join.
    """
    attempts = [row for row in attempts if isinstance(row, AttemptRow)]
    route_by_id = _index(routes, "route_id")
    cycle_by_id = _index(cycles, "cycle_id")
    cycle_by_route = {}
    for cycle in cycle_by_id.values():
        if _get(cycle, "route_id") and _get(cycle, "route_capability") in (None, "", "route-frame"):
            cycle_by_route.setdefault(_get(cycle, "route_id"), cycle)

    groups = {}
    route_key = {}                      # owner route id -> group key

    def group(key, cycle=None):
        item = groups.setdefault(key, _Group(key))
        if cycle is not None:
            item.cycle = cycle
        return item

    owners = sorted((row for row in attempts if row.worker_type == "owner"), key=lambda r: r.line)
    for row in owners:
        owner_route = row.owner_route_id or row.route_id
        route = route_by_id.get(owner_route) if owner_route else None
        parent = _get(route, "parent_cycle_id") if route else None
        cycle = cycle_by_id.get(parent) if parent else None
        if _verified(cycle, parent):
            key = parent
            group(key, cycle).add(row)
        else:
            key = "unverified:" + str(owner_route or row.attempt_id)
            group(key).add(row)
        if owner_route:
            route_key[owner_route] = key

    for row in attempts:
        if row.worker_type == "owner":
            continue
        if row.worker_type == "frame":
            cycle = cycle_by_route.get(row.route_id) if row.route_id else None
            if cycle is not None:
                group(_get(cycle, "cycle_id"), cycle).add(row)
            else:
                group("unverified:" + row.attempt_id).add(row)
            continue
        key = route_key.get(row.route_id)
        if key is None:
            route = route_by_id.get(row.route_id) if row.route_id else None
            parent = _get(route, "parent_cycle_id") if route else None
            cycle = cycle_by_id.get(parent) if parent else None
            key = parent if _verified(cycle, parent) else "unverified:" + str(row.route_id or row.attempt_id)
            group(key, cycle if _verified(cycle, parent) else None)
        group(key).add(row)

    tabs = []
    for key, item in groups.items():
        rows = sorted(item.rows, key=lambda r: r.line)
        owners_in = [r for r in rows if r.worker_type == "owner"]
        frames = [r for r in rows if r.worker_type == "frame"]
        cycle = item.cycle
        verified = cycle is not None
        if owners_in:
            latest = owners_in[-1]
            open_now = is_open(latest.status)
            route_id = latest.owner_route_id or latest.route_id
        elif frames:
            open_now = is_open(frames[-1].status)
            route_id = frames[-1].route_id
        else:
            open_now = any(is_open(r.status) for r in rows)
            route_id = rows[-1].route_id if rows else None
        owner_routes = []
        for r in owners_in:
            owner_route = r.owner_route_id or r.route_id
            if owner_route and owner_route not in owner_routes:
                owner_routes.append(owner_route)
        if verified:
            label = request_name(_get(cycle, "title"), (frames or rows)[0].slug)
            started = str(_get(cycle, "started_on") or (rows[0].timestamp if rows else ""))
            reason = ""
        else:
            label = UNVERIFIED_LABEL
            started = rows[0].timestamp if rows else ""
            reason = "owner route의 parent_cycle_id로 cycle 기록을 확인하지 못함"
        tabs.append(RequestTab(
            identity=key, label=label, route_id=route_id,
            cycle_id=_get(cycle, "cycle_id") if verified else None,
            complete=not open_now, verified=verified,
            frame_route_id=_get(cycle, "route_id") if verified else None,
            owner_route_ids=tuple(owner_routes),
            attempt_ids=tuple(r.attempt_id for r in rows),
            started_at=started, reason=reason))
    first_line = {tab.identity: min((r.line for r in groups[tab.identity].rows), default=0) for tab in tabs}
    tabs.sort(key=lambda t: (t.started_at, first_line[t.identity]))
    return tuple(tabs)


def latest_results(tabs, attempts):
    """Latest row per (route_id, route_node) among open requests (highest registry line)."""
    by_id = {row.attempt_id: row for row in attempts}
    latest = {}
    for tab in tabs:
        if tab.complete:
            continue
        for attempt_id in tab.attempt_ids:
            row = by_id.get(attempt_id)
            if row is None or not row.route_id or not row.route_node:
                continue
            key = (row.route_id, row.route_node)
            if key not in latest or row.line > latest[key].line:
                latest[key] = row
    return tuple(sorted(latest.values(), key=lambda r: r.line))


def request_header(tabs, attempts):
    """요청 N · 열림 N · 최신 실패 N · 검토 N from the latest decision per route/node."""
    results = latest_results(tabs, attempts)
    failed = sum(1 for row in results if row.outcome == "failed")
    review = sum(1 for row in results if row.outcome == "review-blocked")
    return RequestHeader(len(tuple(tabs)), sum(1 for tab in tabs if not tab.complete), failed, review)


def header_text(header):
    if header is None:
        return "요청 ?"
    return (f"요청 {header.total} · 열림 {header.open} · "
            f"최신 실패 {header.latest_failed} · 검토 {header.review}")


def head_line(tag, title, project, header):
    """'[3e] P3+P5 구현 · hearting │ 요청 9 · 열림 4 · 최신 실패 3 · 검토 0' — the focused session's header."""
    who = f"[{tag}] " if tag else ""
    return f"{who}{title or '역할 미확인'} · {project} │ {header_text(header)}"


def visible_tabs(tabs, keep=()):
    """(visible, hidden_closed): every open tab and each kept tab stay; other closed tabs fold."""
    keep = set(keep)
    visible, hidden = [], []
    for tab in tabs:
        (hidden if tab.complete and tab.identity not in keep else visible).append(tab)
    return tuple(visible), tuple(hidden)


def tab_bar(tabs, active="", selected=None, keep=(), width=None):
    """개요 │ ▶b6-report │ b-p3 │ … │ ‹+4 끝남›.

    ``active`` is the tab being shown ('' is 개요) and carries ▶. The selected request
    (``selected``, default ``active``) keeps its full name; every other tab shows the short
    name. Open tabs always stay; a closed tab stays only when active, selected or in ``keep``.
    """
    selected = active if selected is None else selected
    pinned = set(keep) | {active, selected}
    visible, hidden = visible_tabs(tabs, pinned)

    def build(limit):
        parts = ["▶개요" if not active else "개요"]
        for tab in visible:
            name = tab.label if tab.identity in (active, selected) else short_label(tab.label)
            if limit is not None and tab.identity not in (active, selected) and cell_width(name) > limit:
                name = clip(name, limit)
            parts.append(("▶" if tab.identity == active else "") + name)
        if hidden:
            parts.append(f"‹+{len(hidden)} 끝남›")
        return " │ ".join(parts)

    text = build(None)
    if width is not None:
        limit = 8
        while cell_width(text) > width and limit > 1:
            text = build(limit)
            limit -= 1
        if cell_width(text) > width:
            text = clip(text, width)
    return text


def overflow_lines(tabs, header, keep=(), failures=()):
    """The 요청 index shown when the bar overflows: every request, then the open-result ledger."""
    keep = set(keep)
    _visible, hidden = visible_tabs(tabs, keep)
    closed = [tab for tab in tabs if tab.complete]
    lines = [f"요청 {header.total} · 개요는 별도"]
    for tab in tabs:
        lines.append(f"{'끝남' if tab.complete else '열림'} {tab.label} {tab.route_id or '?'}")
    kept = [short_label(tab.label) for tab in closed if tab.identity in keep]
    lines.append(f"끝난 요청 {len(closed)} · 선택 {', '.join(kept) or '없음'} 외 ‹+{len(hidden)} 끝남›")
    lines.append(f"열린 요청 {header.open} · 끝남 접기에 포함하지 않음")
    lines.append("열린 요청 · 단계별 최신 시도 장부")
    lines.extend(failures)
    return tuple(lines)


def open_result_lines(results):
    """rt-… execute │ 결과 ✕ 실패 │ 관문 — : the latest non-passing result of each open request step."""
    return tuple(f"{route_id} {node} │ 결과 {outcome_text(outcome)} │ 관문 {GATE_GLYPH.get(gate, '—')}"
                 for route_id, node, outcome, gate in results)


def select_fallback(tabs, selected="", previous=()):
    """Keep the selection; a vanished request moves to its nearest remaining neighbour, else 개요 ('')."""
    ids = [tab.identity for tab in tabs]
    if not selected or selected in ids:
        return selected
    previous = list(previous)
    if selected in previous:
        at = previous.index(selected)
        for candidate in previous[at + 1:] + previous[:at][::-1]:
            if candidate in ids:
                return candidate
    return ""
