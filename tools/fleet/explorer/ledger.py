"""Pure attempt ledger projection.

Liveness, outcome and gate are three independent sources and are only displayed here:
liveness is the live job's own value (``unknown`` when no live job carries the attempt),
outcome is the ``attempt_decision`` the collector already computed, and the gate is the
route's completion-marker verdict. A missing verdict stays missing; it is never turned
into a failure or a success.
"""
from collections.abc import Mapping

from .output import cell_width
from .types import LedgerRow, RequestLedger

HARNESS_ABBR = {"claude": "CL", "codex": "CX", "opencode": "OC"}
LIVENESS_GLYPH = {"working": "⠇", "idle": "·", "unknown": "?", "blocked": "◑"}
LIVENESS_WORD = {"working": "도는 중", "idle": "쉼", "unknown": "확인 불가", "blocked": "답 대기"}
OUTCOME_GLYPH = {"succeeded": "✓", "failed": "✕", "review-blocked": "◇",
                 "cancelled": "⊘", "pending": "…"}
OUTCOME_WORD = {"succeeded": "성공", "failed": "실패", "review-blocked": "검토",
                "cancelled": "취소", "pending": "아직"}
GATE_GLYPH = {"passed": "⊸", "hold": "—"}


def harness_abbr(harness):
    return HARNESS_ABBR.get(str(harness or "").lower(), "?" if not harness else str(harness)[:2].upper())


def liveness_glyph(liveness):
    if liveness in LIVENESS_GLYPH:
        return LIVENESS_GLYPH[liveness]
    return "?" if not liveness else "○"            # an ended attempt is not "unknown"


def liveness_word(liveness):
    if liveness in LIVENESS_WORD:
        return LIVENESS_WORD[liveness]
    return "확인 불가" if not liveness else "끝남"


def outcome_text(outcome):
    """'✓ 성공' for a decided outcome; '? 미확인' when the policy gave none (never a guess)."""
    if outcome in OUTCOME_GLYPH:
        return f"{OUTCOME_GLYPH[outcome]} {OUTCOME_WORD[outcome]}"
    return "? 미확인"


def short_attempt(attempt_id):
    text = str(attempt_id or "")
    return (text[:11] + "…") if len(text) > 11 else text


def _pad(text, width):
    return text + " " * max(0, width - cell_width(text))


def _node_ids(route_record, rows):
    nodes = []
    if isinstance(route_record, Mapping):
        nodes = [str(n.get("id")) for n in route_record.get("nodes", ()) if isinstance(n, Mapping) and n.get("id")]
    for row in rows:
        if row.route_node and row.route_node not in nodes:
            nodes.append(row.route_node)
    return nodes


def _tab_rows(tab, attempts):
    ids = set(tab.attempt_ids)
    return sorted((row for row in attempts if row.attempt_id in ids), key=lambda r: r.line)


def build_ledger_rows(tab, attempts, route_records=None, gates=None):
    """One row per route node of the request's current route, in route order."""
    route_records = route_records or {}
    gates = gates or {}
    route_id = tab.owner_route_ids[-1] if tab.owner_route_ids else tab.route_id
    rows = [row for row in _tab_rows(tab, attempts) if row.route_id == route_id and row.route_node]
    out = []
    for node in _node_ids(route_records.get(route_id), rows):
        history = [row for row in rows if row.route_node == node]
        current = history[-1] if history else None
        out.append(LedgerRow(
            route_node=node,
            harness=harness_abbr(current.harness) if current else "",
            liveness=current.liveness if current else "",
            outcome=current.outcome if current else None,
            gate=gates.get((route_id, node)),
            attempt_id=current.attempt_id if current else "",
            note=current.note if current else "",
            attempts=len(history),
            history=tuple((n, row.attempt_id, row.note) for n, row in enumerate(history, 1)),
            request_id=tab.identity, route_id=route_id or "",
            trail=tuple((row.attempt_id, row.note, harness_abbr(row.harness), row.liveness, row.outcome)
                        for row in history)))
    return tuple(out)


def build_request_ledger(tab, tabs, attempts, route_records=None, gates=None):
    """Header, stage rows, gate summary and cross-request line for one request tab."""
    route_records = route_records or {}
    members = _tab_rows(tab, attempts)
    rows = build_ledger_rows(tab, attempts, route_records, gates)
    frames = [row for row in members if row.worker_type == "frame"]
    owners = [row for row in members if row.worker_type == "owner"]
    chain = []
    if frames:
        frame_record = route_records.get(frames[-1].route_id)
        has_decision = (not isinstance(frame_record, Mapping)
                        or any(isinstance(n, Mapping) and n.get("id") == "route-decision"
                               for n in frame_record.get("nodes", ())))
        chain.append("frame")
        if has_decision:
            chain.append("경로 결정")
    if owners:
        chain.append(f"감독(owner) {harness_abbr(owners[-1].harness)}")
    ids = []
    if frames:
        ids.append("frame " + short_attempt(frames[-1].attempt_id))
    for index, owner in enumerate(owners):
        ids.append(("감독 " if index == 0 else "") + short_attempt(owner.attempt_id))
    passed = sum(1 for row in rows if row.gate == "passed")
    hold = tuple(row.route_node for row in rows if row.gate == "hold")
    supervisor = owners[-1] if owners else (frames[-1] if frames else None)
    owner_ids = {tab_.identity: [r for r in _tab_rows(tab_, attempts) if r.worker_type == "owner"]
                 for tab_ in tabs}
    others = []
    for other in tabs:
        if other.identity == tab.identity or other.complete:
            continue
        sup = (owner_ids.get(other.identity) or [None])[-1]
        if sup is None or sup.liveness == "working":
            continue                       # a working request is already visible as ⠇ in the nav
        others.append((other.label, sup.liveness, sup.outcome))
    return RequestLedger(
        request_id=tab.identity, title=tab.label, rows=rows,
        start_chain=" → ".join(chain), attempt_chain=" → ".join(ids),
        supervisor_liveness=supervisor.liveness if supervisor else "unknown",
        supervisor_outcome=supervisor.outcome if supervisor else None,
        gate_passed=passed, gate_total=len(rows), gate_hold=hold,
        gate_mismatch=bool(hold), others=tuple(others),
        responsible="감독(owner)" if owners else ("frame" if frames else ""),
        supervision=tuple((harness_abbr(row.harness), row.outcome, row.attempt_id)
                          for row in members if row.worker_type in ("frame", "owner")),
        owner_count=len(owners))


def format_row(row, selected=False):
    """' plan        CX  ?  ✓ 성공 ⊸    1' — mark, stage, harness, liveness, outcome, gate, attempts."""
    mark = "▶" if selected else " "
    gate = GATE_GLYPH.get(row.gate, " ")
    return (f"{mark}{_pad(row.route_node, 11)} {_pad(row.harness, 2)}  "
            f"{liveness_glyph(row.liveness) if row.attempts else ' '}  "
            f"{outcome_text(row.outcome) if row.attempts else '… 아직'} {gate}    {row.attempts}")


def ledger_lines(view, selected_node=None):
    """The ledger column text, in the canonical row order."""
    lines = [f"{view.title} · 완료 근거"]
    if view.start_chain:
        lines.append(f"시작: {view.start_chain}")
    if view.attempt_chain:
        lines.append(view.attempt_chain)
    if view.attempt_chain:
        lines.append(f"감독 생존 {liveness_glyph(view.supervisor_liveness)} "
                     f"{liveness_word(view.supervisor_liveness)} │ 결과 {outcome_text(view.supervisor_outcome)}")
    lines.append("단계        하네스 생존 결과    관문  시도")
    for row in view.rows:
        selected = row.route_node == selected_node
        lines.append(format_row(row, selected))
        if selected:
            lines.extend(f"  {n} {note}" for n, _attempt, note in row.history)
    if view.gate_total:
        summary = f"⊸ 통과 {view.gate_passed}/{view.gate_total}"
        if view.gate_hold:
            summary += f" · {', '.join(view.gate_hold)} — 판정 보류"
        lines.append(summary)
        if view.gate_mismatch:
            lines.append("  근거 불일치")
    if view.others:
        parts = []
        for label, liveness, outcome in view.others:
            result = (f"{OUTCOME_GLYPH[outcome]}{OUTCOME_WORD[outcome]}" if outcome in OUTCOME_GLYPH
                      else "?미확인")
            parts.append(f"{label} {liveness_glyph(liveness)}{liveness_word(liveness)} {result}")
        lines.append("같은 세션 다른 요청: " + " · ".join(parts))
    if view.responsible:
        lines.append(f"담당: {view.responsible} · 실행 중 단계는 상세에서")
    return tuple(lines)


def history_lines(view):
    """시도 이력: every attempt of every step, oldest first, with its own note, liveness and outcome."""
    lines = [f"시도 이력 · {view.title}"]
    for row in view.rows:
        for attempt_id, note, harness, liveness, outcome in row.trail:
            lines.append(f"{row.route_node} {note}".rstrip())
            lines.append(f"  {attempt_id}")
            lines.append(f"  {harness} · 생존 {liveness_glyph(liveness)} {liveness_word(liveness)}"
                         f" · 결과 {outcome_text(outcome)}")
    if view.gate_hold and view.gate_mismatch:
        lines.append(f"{', '.join(view.gate_hold)} — 판정 보류(근거 불일치)")
    if view.owner_count > 1:
        lines.extend(replacement_lines(view))
    return tuple(lines)


def replacement_lines(view):
    """감독 교체: the frame and every supervisor attempt of a request whose supervisor changed."""
    lines = [f"감독 교체 · 같은 요청 {view.title}"]
    lines.extend(f"{harness} {outcome_text(outcome)} {attempt_id}"
                 for harness, outcome, attempt_id in view.supervision)
    return tuple(lines)


def narrow_ledger_lines(view):
    """One glyph cell per step (liveness, outcome, gate) for the 5-column ledger beside a wide output."""
    return tuple(f"{liveness_glyph(row.liveness) if row.attempts else ' '}"
                 f"{OUTCOME_GLYPH.get(row.outcome, '?') if row.attempts else '…'}"
                 f"{GATE_GLYPH.get(row.gate, '')}" for row in view.rows)


def summary_lines(view, selected_node=None):
    """The output pane before the raw record reader exists: what the ledger already knows, then '읽는 중'."""
    row = next((r for r in view.rows if r.route_node == selected_node), None) if selected_node else None
    if row is None:
        lines = [f"{view.title} · 감독 요약"]
        lines.append(f"감독 생존 {liveness_glyph(view.supervisor_liveness)} {liveness_word(view.supervisor_liveness)}"
                     f" │ 결과 {outcome_text(view.supervisor_outcome)}")
    else:
        lines = [f"{row.route_node} · {row.harness} · 요약",
                 f"생존 {liveness_glyph(row.liveness)} {liveness_word(row.liveness)}"
                 f" │ 결과 {outcome_text(row.outcome)} │ 관문 {GATE_GLYPH.get(row.gate, '—')}"]
        if row.note:
            lines.append(f"사유 {row.note}")
    lines.append("원문 읽는 중")
    return tuple(lines)
