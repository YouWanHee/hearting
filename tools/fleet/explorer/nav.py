"""Pure navigation projection: 세션 · 남은 일 · 관계 · 시스템.

``build_nav`` returns every row with its fold parent (rows under a closed fold are still in
the list, so the s/x target set is independent of what is expanded); ``visible_rows`` applies
the folds and the name search; ``nav_lines`` draws the viewport. Only session and worker rows
carry a ``target_ref`` — requests, steps, relations, remaining work and the system section are
information and can never be a kill target.
"""

from collections import OrderedDict
from collections.abc import Mapping
import os
import re

from . import system
from .ledger import harness_abbr, liveness_glyph
from .output import cell_width, clip
from .types import Health, NavRow

INDENT = (0, 1, 2, 4, 6, 8, 10, 12, 14)
SYSTEM_OPEN_WIDTH = 40            # the system section opens by itself once the column is this wide
_ENDED = frozenset(("done", "dead"))
_LIVE_ORDER = ("working", "idle", "unknown", "blocked")
NO_ROLE = "역할 미확인"


def _get(value, *names, default=None):
    for name in names:
        candidate = value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)
        if candidate is not None and candidate != "":
            return candidate
    return default


def indent_of(depth):
    return INDENT[min(max(0, depth), len(INDENT) - 1)]


# -- sessions and projects ------------------------------------------------------------------

def is_main(session):
    """A conversation the user started; worker/child sessions are counted, not listed as sessions."""
    return not _get(session, "is_child") and _get(session, "process_role") != "registered-worker"


def session_ids(session):
    ids = [_get(session, "session_id", "sid", "id")] + list(_get(session, "session_aliases", default=()) or ())
    return [str(i) for i in ids if i]


def ordered_mains(snapshot):
    """Main sessions in the snapshot's live order hint, then in snapshot order."""
    mains = [s for s in (_get(snapshot, "sessions", default=()) or ()) if is_main(s)]
    position = {str(sid): i for i, sid in enumerate(_get(snapshot, "session_order", default=()) or ())}
    hinted = sorted((s for s in mains if str(_get(s, "session_id", "sid", "id")) in position),
                    key=lambda s: position[str(_get(s, "session_id", "sid", "id"))])
    return hinted + [s for s in mains if str(_get(s, "session_id", "sid", "id")) not in position]


def project_name(cwd, resolver=None):
    """Repository name of a working directory.

    ``resolver`` may be the existing Fleet project helper (git-aware, cached by the caller);
    without one the path rules of ``model.project_of`` are applied, which need no I/O.
    """
    if not cwd:
        return "(unknown)"
    if resolver is not None:
        return resolver(cwd)
    parts = [p for p in os.fspath(cwd).rstrip("/").split("/") if p]
    for comp in parts:
        if len(comp) > 3 and comp.endswith("-wt"):
            return comp[: -len("-wt")]
    for comp in parts:
        if len(comp) > len("_worktrees") and comp.endswith("_worktrees"):
            return comp[: -len("_worktrees")]
    for index, comp in enumerate(parts):
        if comp == ".worktrees" and index:
            return parts[index - 1]
    base = re.sub(r"\.broken.*$", "", parts[-1]) if parts else ""
    return base or "(root)"


_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}_")


def role_name(value):
    """The role a session or worker was given: its own title or summary, never a guess."""
    for name in ("title", "summary", "name"):
        text = " ".join(str(_get(value, name, default="")).split())
        if text:
            return text
    return NO_ROLE


def _aggregate(sessions):
    counts = OrderedDict((key, 0) for key in _LIVE_ORDER)
    counts["other"] = 0
    for session in sessions:
        live = str(_get(session, "liveness", default="unknown"))
        counts[live if live in counts else "other"] += 1
    parts = []
    for key, number in counts.items():
        if number:
            glyph = liveness_glyph(key if key != "other" else "ended")
            parts.append(f"{glyph}{number}")
    return " ".join(parts)


def relation_summary(snapshot):
    """('⚑2 관찰6 최근필드16'): steward sessions, their observed targets, and filled last-message fields."""
    mains = ordered_mains(snapshot)
    stewards = sum(1 for s in mains if _get(s, "steward"))
    targets = sum(len(_get(s, "steward_targets", default=()) or ()) for s in mains)
    fields = sum((1 if _get(s, "peer_last_sent") else 0) + (1 if _get(s, "peer_last_recv") else 0)
                 for s in mains)
    return f"⚑{stewards} 관찰{targets} 최근필드{fields}"


def find_session(snapshot, sid):
    for session in _get(snapshot, "sessions", default=()) or ():
        if sid and str(sid) in session_ids(session):
            return session
    return None


# -- state access ----------------------------------------------------------------------------

def _set(value):
    return set(value or ())


def _focus_sid(focus_result):
    key = getattr(focus_result, "key", None)
    return str(getattr(key, "sid", "") or "")


def _selected_request(state, focus_result):
    selection = str(getattr(state, "selection", "") or "")
    for prefix in ("request:", "stages:", "stage:"):
        if selection.startswith(prefix):
            return selection[len(prefix):].split(":", 1)[0]
    tab = str(getattr(state, "tab", "") or "")
    return tab or ""


# -- row construction ------------------------------------------------------------------------

def _session_label(session, request_token):
    tag = _get(session, "session_tag")
    flag = "⚑" if _get(session, "steward") else ""
    who = f"[{tag}]{flag}" if tag else flag
    parts = [liveness_glyph(str(_get(session, "liveness", default="unknown"))),
             harness_abbr(_get(session, "harness"))]
    if who:
        parts.append(who)
    parts.append(request_token)
    parts.append(role_name(session))
    return " ".join(parts)


def _job_is_live(job):
    return str(_get(job, "liveness", default="unknown")) not in _ENDED


def _job_key(job):
    return str(_get(job, "job_id", "attempt_id", "key", "slug", "id", default="unknown"))


def _worker_label(job):
    harness = harness_abbr(_get(job, "harness"))
    if _get(job, "worker_type") == "owner":
        return f"감독 {harness}"
    node = _get(job, "route_node")
    return f"{node} {harness}" if node else f"{role_name(job)} {harness}".strip()


def _worker_row(job, session, depth, parent):
    harness = str(_get(session, "harness", default="unknown"))
    sid = str(_get(session, "session_id", "sid", "id", default="unknown"))
    return NavRow(f"worker:{harness}:{sid}:{_job_key(job)}", "worker", depth, _worker_label(job),
                  (("sid", sid), ("job_id", _job_key(job))),
                  Health(state=str(_get(job, "liveness", default="unknown"))), job, parent=parent)


def _jobs_of(snapshot, session):
    ids = set(session_ids(session))
    return [job for job in (_get(snapshot, "jobs", default=()) or ())
            if str(_get(job, "_parent_edge_sid", "parent_sid", "caller_sid", default="")) in ids
            and _job_is_live(job)]


def _previous_generation(session):
    previous = _get(session, "previous_generation", "previous_sid")
    if previous is None:
        return None
    sid = previous if isinstance(previous, str) else _get(previous, "session_id", "sid", "id")
    return str(sid or "")


def _request_rows(tabs, focus_result, snapshot, session, parent_id, state):
    """The focused session's request tree: shown requests, then 그 외 요청 N with the rest."""
    open_set, fold_set = _set(getattr(state, "nav_open", ())), _set(getattr(state, "nav_fold", ()))
    selected = _selected_request(state, focus_result)
    keep = set(getattr(state, "keep", ()) or ())
    open_tabs = [tab for tab in tabs if not tab.complete]
    shown_ids = {selected} | keep | ({open_tabs[-1].identity} if open_tabs else set())
    shown = [tab for tab in tabs if tab.identity in shown_ids]
    others = [tab for tab in tabs if tab.identity not in shown_ids]
    ledger = getattr(focus_result, "request_ledger", None)
    jobs = _jobs_of(snapshot, session)
    rows, claimed = [], set()

    def request_block(tab, depth, parent):
        is_selected = tab.identity == selected
        rid = f"request:{tab.identity}"
        rows.append(NavRow(rid, "request", depth, tab.label,
                           (("route_id", tab.route_id), ("cycle_id", tab.cycle_id)),
                           Health(state="open" if not tab.complete else "closed"),
                           marker="▶" if is_selected else "▸", parent=parent, open=rid not in fold_set))
        stages_id = f"stages:{tab.identity}"
        known = ledger is not None and ledger.request_id == tab.identity
        stage_rows = tuple(ledger.rows) if known else ()
        mine = [job for job in jobs if str(_get(job, "attempt_id", default="")) in tab.attempt_ids]
        stages_open = stages_id in open_set and stages_id not in fold_set
        rows.append(NavRow(stages_id, "stage-group", depth + 1,
                           f"단계 {len(stage_rows)}" if known else "단계",
                           marker="▾" if stages_open else "▸", parent=rid, open=stages_open))
        for row in stage_rows:
            rows.append(NavRow(f"stage:{tab.identity}:{row.route_node}", "stage", depth + 2,
                               f"{row.route_node} {row.harness}".strip(), health=Health(state=str(row.liveness)),
                               parent=stages_id))
        for job in mine:
            claimed.add(id(job))
            rows.append(_worker_row(job, session, depth + 2, stages_id))

    for tab in shown:
        request_block(tab, 3, parent_id)
    other_id = f"requests-other:{_get(session, 'session_id', default='')}"
    other_open = other_id in open_set and other_id not in fold_set
    rows.append(NavRow(other_id, "request-group", 3, f"그 외 요청 {len(others)}",
                       marker="▾" if other_open else "▸", parent=parent_id, open=other_open))
    for tab in others:
        request_block(tab, 4, other_id)
    return rows, [job for job in jobs if id(job) not in claimed]


def build_nav(snapshot, focus_result=None, state=None, *, width=None, project_resolver=None):
    """Every navigation row, in display order, without reading anything outside the arguments."""
    open_set, fold_set = _set(getattr(state, "nav_open", ())), _set(getattr(state, "nav_fold", ()))
    selection = str(getattr(state, "selection", "") or "")
    sessions_all = list(_get(snapshot, "sessions", default=()) or ())
    mains = ordered_mains(snapshot)
    focus_sid = _focus_sid(focus_result)
    focus = find_session(snapshot, focus_sid) if focus_sid else None
    tabs = tuple(getattr(focus_result, "tabs", ()) or ())

    sessions_open = "section:sessions" not in fold_set
    rows = [NavRow("section:sessions", "section", 0,
                   f"세션 {len(mains)} · worker {len(sessions_all) - len(mains)}",
                   marker="▾" if sessions_open else "▸", open=sessions_open)]

    projects = OrderedDict()
    for session in mains:
        projects.setdefault(project_name(_get(session, "cwd", "project"), project_resolver), []).append(session)
    for name, members in projects.items():
        pid = f"project:{name}"
        holds_focus = focus is not None and any(s is focus for s in members)
        holds_selected = any(f"session:{_get(s, 'harness', default='unknown')}:"
                             f"{_get(s, 'session_id', 'sid', 'id', default='unknown')}" == selection
                             for s in members)
        is_open = pid in open_set or ((holds_focus or holds_selected) and pid not in fold_set)
        label = f"{name} {_aggregate(members)}".rstrip()
        if not is_open:
            tags = [f"[{_get(s, 'session_tag')}]⚑" for s in members
                    if _get(s, "steward") and _get(s, "session_tag")]
            if tags:
                label += " " + " ".join(tags)
        rows.append(NavRow(pid, "project", 1, label, marker="▾" if is_open else "▸",
                           parent="section:sessions", open=is_open))
        for session in members:
            harness = str(_get(session, "harness", default="unknown"))
            sid = str(_get(session, "session_id", "sid", "id", default="unknown"))
            rid = f"session:{harness}:{sid}"
            is_focus = focus is session
            expanded = (rid in open_set or is_focus) and rid not in fold_set
            if expanded and is_focus and tabs:
                token = f"▾{len(tabs)}"
            elif expanded and is_focus:
                token = "▾?"
            else:
                token = "▸?" if not expanded else "▾?"
            rows.append(NavRow(rid, "session", 2, _session_label(session, token),
                               (("harness", harness), ("sid", sid)),
                               Health(state=str(_get(session, "liveness", default="unknown"))),
                               session, parent=pid, open=expanded))
            unclaimed = _jobs_of(snapshot, session)
            if is_focus and tabs:
                block, unclaimed = _request_rows(tabs, focus_result, snapshot, session, rid, state)
                rows.extend(block)
            previous = _previous_generation(session)
            if previous is not None:
                tag = _get(find_session(snapshot, previous), "session_tag") if previous else None
                shown = f"[{tag}]" if tag else (previous[:8] if previous else NO_ROLE)
                rows.append(NavRow(f"previous-generation:{harness}:{sid}", "previous-generation", 3,
                                   f"이전 세대 {shown}", (("sid", previous),), marker="▸", parent=rid))
            for job in unclaimed:
                rows.append(_worker_row(job, session, 3, rid))

    obligations = getattr(focus_result, "obligations", None)
    rows.append(NavRow("section:todo", "section", 0,
                       f"남은 일 {'?' if obligations is None else len(obligations)}", marker="▸"))
    rows.append(NavRow("section:relations", "section", 0, f"관계 {relation_summary(snapshot)}", marker="▸"))

    system_open = ("section:system" in open_set or (width is not None and width >= SYSTEM_OPEN_WIDTH)) \
        and "section:system" not in fold_set
    rows.append(NavRow("section:system", "section", 0,
                       "시스템" if system_open else f"시스템 {system.gpu_text(snapshot)}",
                       marker="▾" if system_open else "▸", open=system_open))
    if system_open:
        rows.append(NavRow("system:usage", "system", 1, system.usage_text(snapshot, spaced=True),
                           parent="section:system"))
        rows.append(NavRow("system:allocation", "system", 1, system.allocation_text(snapshot),
                           parent="section:system"))
        for host, suffix in system.host_nav_labels(snapshot):
            rows.append(NavRow(f"system:{host}", "system", 1, f"{host} {suffix}", marker="▸",
                               parent="section:system"))
    return rows


# -- visibility and drawing --------------------------------------------------------------------

def visible_rows(rows, state=None):
    """Rows not hidden by a closed fold; with a search text, matches and their ancestors."""
    rows = list(rows)
    by_id = {row.identity: row for row in rows}
    query = str(getattr(state, "search", "") or "").casefold()
    if query:
        keep = set()
        for row in rows:
            if query in row.label.casefold():
                while row is not None and row.identity not in keep:
                    keep.add(row.identity)
                    row = by_id.get(row.parent)
        return [row for row in rows if row.identity in keep]
    shown = {}
    result = []
    for row in rows:
        parent = by_id.get(row.parent)
        visible = parent is None or (shown.get(parent.identity, False) and parent.open is not False)
        shown[row.identity] = visible
        if visible:
            result.append(row)
    return result


def row_text(row, selected=False):
    """indent + fold marker + label; the selected row wears ▶ in place of its marker (or in the indent)."""
    pad = indent_of(row.depth)
    marker = row.marker
    if selected:
        if marker:
            marker = "▶"
        elif pad:
            return " " * (pad - 1) + "▶" + row.label
    return " " * pad + (f"{marker} {row.label}" if marker else row.label)


def _fit(row, selected, width):
    """Row text for ``width``; a project name that does not fit first drops its 'YYYY-MM_' date prefix."""
    text = row_text(row, selected)
    if row.kind == "project" and cell_width(text) > width:
        text = _DATE_PREFIX.sub("", text.replace(row.label, _DATE_PREFIX.sub("", row.label, count=1), 1), count=1)
    return text


def nav_lines(rows, state, width, height):
    """Title, the viewport of rows, and the '탐색 a–b / n (접힘 제외)' footer; fits ``height`` rows."""
    selection = str(getattr(state, "selection", "") or "")
    search = str(getattr(state, "search", "") or "")
    searching = bool(getattr(state, "searching", False))
    title = "탐색" + (f" / {search}" + ("_" if searching else "") if (search or searching) else "")
    rows = list(rows)
    window = max(0, height - 3)
    scroll = dict(getattr(state, "scrolls", ()) or ()).get("nav", 0)
    offset = max(0, min(int(scroll or 0), max(0, len(rows) - window)))
    for index, row in enumerate(rows):
        if row.identity == selection:
            if index < offset:
                offset = index
            elif window and index >= offset + window:
                offset = index - window + 1
            break
    shown = rows[offset:offset + window]
    lines = [clip(title, width), ""]
    lines.extend(clip(_fit(row, row.identity == selection, width), width) for row in shown)
    first, last = (offset + 1, offset + len(shown)) if shown else (0, 0)
    lines.append(clip(f"탐색 {first}–{last} / {len(rows)} (접힘 제외)", width))
    return tuple(lines), offset
