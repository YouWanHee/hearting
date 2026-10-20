"""Pure viewer-state schema conversion and Explorer keyboard reducer.

The reducer performs no I/O and adds no mode: ↑↓ move or scroll the focused region, ←→ move the
focus between regions, Enter opens / selects, Tab walks the request tabs, ``/`` filters the
navigation by name, ``e`` folds the navigation, ``F`` hides the event row. Selection and tab
survive every fold.
"""

from dataclasses import dataclass, fields, replace
from collections.abc import Mapping

REGIONS = ("nav", "ledger", "output", "tabs", "relations", "system")
WIDE_NAV_COLS = 100
PID_PREFIX = "pid:"               # a schema-1 kill cursor, resolved to a navigation row by ``settle``


@dataclass(frozen=True)
class ExplorerState:
    selection: str = ""
    tab: str = ""
    stage: str = ""
    active_region: str = "nav"
    nav_fold: tuple = ()
    scrolls: tuple = ()
    nav_visible: bool = True
    events_visible: bool = True
    search: str = ""
    tab_order: tuple = ()
    nav_open: tuple = ()
    keep: tuple = ()
    history: tuple = ()
    searching: bool = False
    focus: str = ""

    def scroll_map(self):
        return dict(self.scrolls)

    def fold_set(self):
        return set(self.nav_fold)


def initial_state(cols=WIDE_NAV_COLS):
    """A fresh view: the navigation starts folded below 100 columns."""
    return ExplorerState(nav_visible=int(cols) >= WIDE_NAV_COLS)


def _mapping(value):
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "__dict__"):
        return vars(value)
    return {}


def _clean_scrolls(value):
    if not isinstance(value, Mapping):
        return ()
    result = []
    for identity, offset in value.items():
        try:
            result.append((str(identity), max(0, int(offset))))
        except (TypeError, ValueError, OverflowError):
            continue
    return tuple(sorted(result))


def _strings(value):
    return tuple(str(x) for x in (value or ()))


def restore(value=None):
    """Restore schema 2 and translate useful selection/fold/offset from schema 1."""
    data = _mapping(value)
    if isinstance(data.get("viewer_state"), Mapping):
        data = data["viewer_state"]
    layout = data.get("frame_layout") or data.get("layout") or {}
    layout = _mapping(layout)
    version = layout.get("schema_version", data.get("schema_version", 1))
    if version == 2:
        return ExplorerState(
            selection=str(data.get("selection") or ""),
            tab=str(data.get("tab") or ""),
            stage=str(data.get("stage") or ""),
            active_region=str(data.get("active_region") or "nav"),
            nav_fold=_strings(data.get("nav_fold")),
            scrolls=_clean_scrolls(data.get("scrolls", {})),
            nav_visible=bool(data.get("nav_visible", True)),
            events_visible=bool(data.get("events_visible", True)),
            search=str(data.get("search") or ""),
            tab_order=_strings(data.get("tab_order")),
            nav_open=_strings(data.get("nav_open")),
            keep=_strings(data.get("keep")),
            history=_strings(data.get("history")),
            focus=str(data.get("focus") or ""),
        )

    # Schema 1 cursor identities are the closest available nav selection evidence: an
    # ``_entry_id`` mapping, or the (pid, proc_start) pair the old kill cursor was.
    raw_cursor = data.get("cursor")
    cursor = _mapping(raw_cursor)
    selection = cursor.get("_entry_id") or cursor.get("entry_id") or ""
    if (not selection and isinstance(raw_cursor, (list, tuple)) and raw_cursor
            and isinstance(raw_cursor[0], int) and not isinstance(raw_cursor[0], bool)):
        start = raw_cursor[1] if len(raw_cursor) > 1 and raw_cursor[1] is not None else ""
        selection = f"{PID_PREFIX}{raw_cursor[0]}:{start}"
    folds = []
    for source in (data.get("folds"), data.get("repo_folds")):
        if isinstance(source, Mapping):
            folds.extend(key for key, folded in source.items() if folded)
        elif isinstance(source, (list, tuple)):
            for item in source:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    if item[1] and isinstance(item[0], str):
                        folds.append(item[0])
                elif isinstance(item, str):
                    folds.append(item)
    offsets = {}
    if "offset" in data:
        offsets["nav"] = data.get("offset")
    # Keep a known old view only as a display-region hint; never replay its frame.
    return ExplorerState(selection=str(selection), active_region="nav",
                         nav_fold=_strings(folds),
                         scrolls=_clean_scrolls(offsets), nav_visible=True)


def coerce(value):
    """An ExplorerState from a state, a mapping of its fields, or a persisted payload."""
    if isinstance(value, ExplorerState):
        return value
    data = _mapping(value)
    names = {field.name for field in fields(ExplorerState)}
    if data and set(data) <= names:
        known = {k: v for k, v in data.items() if k in names}
        for key in ("nav_fold", "nav_open", "keep", "history", "tab_order"):
            if key in known:
                known[key] = _strings(known[key])
        if isinstance(known.get("scrolls"), Mapping):
            known["scrolls"] = _clean_scrolls(known["scrolls"])
        return ExplorerState(**known)
    return restore(value)


def serialize(state):
    """Emit a stable schema-2 viewer payload; no frame pixels are persisted."""
    state = coerce(state)
    return {
        "schema_version": 2,
        "selection": state.selection,
        "tab": state.tab,
        "stage": state.stage,
        "active_region": state.active_region,
        "nav_fold": list(state.nav_fold),
        "nav_open": list(state.nav_open),
        "scrolls": dict(state.scrolls),
        "nav_visible": state.nav_visible,
        "events_visible": state.events_visible,
        "search": state.search,
        "tab_order": list(state.tab_order),
        "keep": list(state.keep),
        "history": list(state.history),
        "focus": state.focus,
        "frame_layout": {"schema_version": 2},
    }


def reconcile(state, tabs):
    """Keep an existing tab, otherwise select its nearest surviving old neighbor."""
    state = coerce(state)
    identities = [str(getattr(tab, "identity", tab.get("identity", "")
                              if isinstance(tab, Mapping) else "")) for tab in tabs or ()]
    if not identities or state.tab in identities or not state.tab:
        return replace(state, tab_order=tuple(identities))
    old = list(state.tab_order)
    try:
        old_index = old.index(state.tab)
    except ValueError:
        old_index = 0
    preferred = old[old_index + 1:] + list(reversed(old[:old_index]))
    selected = next((identity for identity in preferred if identity in identities), identities[0])
    return replace(state, tab=selected, tab_order=tuple(identities))


# -- settling against the navigation --------------------------------------------------------------

# Rows that exist only once the focused session's request data has been read.
_REQUEST_ROWS = ("request:", "stages:", "stage:", "requests-other:", "worker:")


def _row_session(rows, identity):
    """The session row at or above ``identity`` (its fold parents lead up to it), else None."""
    by_id = {row.identity: row for row in rows}
    row = by_id.get(identity)
    seen = set()
    while row is not None and row.identity not in seen:
        if row.kind == "session":
            return row
        seen.add(row.identity)
        row = by_id.get(row.parent)
    return None


def _session_ref(row):
    return row.identity[len("session:"):]


def _legacy_pid_row(rows, token):
    pid, _, start = token[len(PID_PREFIX):].partition(":")
    for row in rows:
        target = row.target_ref
        if target is None:
            continue
        cursor_pid = target.get("pid") if isinstance(target, Mapping) else getattr(target, "pid", None)
        cursor_start = target.get("proc_start") if isinstance(target, Mapping) else getattr(target, "proc_start", None)
        if str(cursor_pid) == pid and (not start or str(cursor_start) == start):
            return row
    return None


def _fold_identities(folds, rows):
    """Fold keys as navigation identities: schema-1 repo keys (paths / names) become project rows."""
    from . import nav
    known = {row.identity for row in rows}
    projects = {row.identity for row in rows if row.kind == "project"}
    result = []
    for key in folds:
        if key in known or not ("/" in key or f"project:{key}" in projects):
            result.append(key)
            continue
        name = nav.project_name(key) if "/" in key else key
        if f"project:{name}" in projects:
            result.append(f"project:{name}")
    return tuple(dict.fromkeys(result))


def settle(state, rows, tabs=None):
    """Resolve the state against the navigation rows it is about to be drawn with; pure.

    A schema-1 kill cursor (``pid:...``) or ``harness:sid`` selection becomes the nearest nav
    row, repo folds become project folds, an empty or vanished selection falls back to the first
    session, ``focus`` follows the selected row's session, and — once ``tabs`` (the focused
    session's request tabs) are known — a vanished request tab moves to its nearest surviving
    neighbor or the overview. Request-level selections are left alone while ``tabs`` is None:
    those rows exist only after the selected scope has been read.
    """
    state = coerce(state)
    rows = tuple(rows or ())
    identities = {row.identity for row in rows}
    selection = state.selection
    if selection.startswith(PID_PREFIX):
        row = _legacy_pid_row(rows, selection)
        selection = row.identity if row is not None else ""
    elif selection and selection not in identities:
        if f"session:{selection}" in identities:
            selection = f"session:{selection}"
        elif not selection.startswith(_REQUEST_ROWS):
            selection = ""                         # request rows are judged below, once tabs are known
    sessions = [row for row in rows if row.kind == "session"]
    if not selection and sessions:
        selection = sessions[0].identity
    focus = state.focus
    held = _row_session(rows, selection)
    if held is not None:
        focus = _session_ref(held)
    elif not any(_session_ref(row) == focus for row in sessions):
        focus = _session_ref(sessions[0]) if sessions else focus
    state = replace(state, selection=selection, focus=focus,
                    nav_fold=_fold_identities(state.nav_fold, rows))
    if tabs is not None:
        before = state.tab
        state = reconcile(state, tabs)
        if state.tab != before:
            state = replace(state, stage="")
        ids = {str(getattr(tab, "identity", tab.get("identity", "") if isinstance(tab, Mapping) else ""))
               for tab in tabs}
        if state.tab and state.tab not in ids:
            state = replace(state, tab="", stage="")          # nothing left to neighbor: the overview
        if selection.startswith(_REQUEST_ROWS[:3]) and selection.split(":", 2)[1] not in ids:
            fallback = f"request:{state.tab}" if state.tab and f"request:{state.tab}" in identities else \
                _row_session_identity(rows, focus)
            state = replace(state, selection=fallback or state.selection)
        elif selection.startswith("worker:") and selection not in identities:
            state = replace(state, selection=_row_session_identity(rows, focus) or state.selection)
    return state


def _row_session_identity(rows, focus):
    return next((row.identity for row in rows if row.kind == "session" and _session_ref(row) == focus), "")


def focus_key(state):
    """The selected-scope key the pump reads for: session, request tab and stage."""
    from .types import FocusKey
    state = coerce(state)
    harness, _, sid = state.focus.partition(":")
    if not sid:
        return None
    return FocusKey(harness, sid, state.tab, state.stage if state.tab else "")


# -- keyboard ------------------------------------------------------------------------------------

def _toggle(items, value):
    items = set(items)
    items.symmetric_difference_update((value,))
    return tuple(sorted(items))


def _scroll_key(state):
    if state.active_region == "nav":
        return "nav"
    return f"{state.active_region}:{state.tab}"


def _move_selection(state, rows, step):
    identities = [str(getattr(row, "identity", "")) for row in rows]
    if not identities:
        return state
    if state.selection not in identities:
        return replace(state, selection=identities[0 if step > 0 else -1])
    index = identities.index(state.selection) + step
    return replace(state, selection=identities[max(0, min(len(identities) - 1, index))])


def _open_toggle(state, row):
    identity = row.identity
    if row.open:
        return replace(state, nav_fold=tuple(sorted(set(state.nav_fold) | {identity})),
                       nav_open=tuple(x for x in state.nav_open if x != identity))
    return replace(state, nav_open=tuple(sorted(set(state.nav_open) | {identity})),
                   nav_fold=tuple(x for x in state.nav_fold if x != identity))


def _select_tab(state, identity):
    keep = state.keep
    if state.tab and state.tab != identity:
        keep = (state.tab,)
    return replace(state, tab=identity, keep=keep)


def _enter(state, rows):
    if state.active_region == "ledger":
        return replace(state, history=_toggle(state.history, state.tab)) if state.tab else state
    if state.active_region == "tabs":
        return replace(state, active_region="ledger")
    if state.active_region != "nav":
        return state
    row = next((r for r in rows if getattr(r, "identity", None) == state.selection), None)
    if row is None:
        return state
    if row.kind == "request":
        return replace(_select_tab(state, row.identity[len("request:"):]), stage="", active_region="ledger")
    if row.kind == "stage":
        request, _, node = row.identity[len("stage:"):].partition(":")
        return replace(_select_tab(state, request), stage=node, active_region="ledger")
    if row.identity == "section:todo":
        return replace(_select_tab(state, ""), active_region="nav")
    if row.identity == "section:relations":
        return replace(state, active_region="relations")
    if row.kind == "system" and row.open is None:
        return replace(state, active_region="system")
    if row.open is not None:
        return _open_toggle(state, row)
    return state


def _cycle_tab(state, step):
    order = list(state.tab_order)
    stops = [""] + order
    index = stops.index(state.tab) if state.tab in stops else 0
    return _select_tab(state, stops[(index + step) % len(stops)])


def _viewport(frame, region):
    """Height of the rectangle ``region`` scrolls in (pages other than nav / output live in the ledger rect)."""
    rect = frame.region(region if region in ("nav", "ledger", "output") else "ledger") if hasattr(frame, "region") else None
    return getattr(rect, "h", 0)


def _frame_rows(frame):
    return tuple(getattr(frame, "nav_rows", ()) or ())


def reduce_key(state, key, frame=None):
    """Apply one key/event and return a new state; performs no I/O."""
    state = coerce(state)
    event = key if isinstance(key, Mapping) else {"key": key}
    key = event.get("key")
    rows = tuple(event.get("nav_rows") or _frame_rows(frame))
    if key in ("resize", "RESIZE"):
        cols = int(event.get("cols", WIDE_NAV_COLS))
        return replace(state, nav_visible=bool(state.nav_visible and cols >= WIDE_NAV_COLS))
    if state.searching:
        if key in ("escape", "esc"):
            return replace(state, search="", searching=False)
        if key in ("enter", "\n"):
            return replace(state, searching=False)
        if key in ("backspace", "KEY_BACKSPACE"):
            return replace(state, search=state.search[:-1])
        if isinstance(key, str) and key.startswith("text:"):
            return replace(state, search=state.search + key[5:])
        if key not in ("up", "k", "down", "j"):
            return state
    if key in ("e", "E"):
        shown = not state.nav_visible
        region = state.active_region
        if not shown and region == "nav":
            region = "ledger"
        return replace(state, nav_visible=shown, active_region=region)
    if key in ("F", "f"):
        return replace(state, events_visible=not state.events_visible)
    if key in ("left", "h", "right", "l"):
        order = [r for r in REGIONS if r != "nav" or state.nav_visible]
        index = order.index(state.active_region) if state.active_region in order else 0
        step = -1 if key in ("left", "h") else 1
        return replace(state, active_region=order[(index + step) % len(order)])
    if key in ("tab", "TAB", "\t"):
        return _cycle_tab(state, 1)
    if key in ("btab", "shift-tab", "BTAB"):
        return _cycle_tab(state, -1)
    if key in ("/", "search"):
        return replace(state, search="", searching=True, active_region="nav" if state.nav_visible else state.active_region)
    if key in ("escape", "esc"):
        return replace(state, search="", searching=False) if state.search else state
    if key in ("up", "k", "down", "j", "pageup", "pagedown", "home", "end"):
        if state.active_region == "nav" and key in ("up", "k", "down", "j") and rows:
            return _move_selection(state, rows, -1 if key in ("up", "k") else 1)
        offsets = state.scroll_map()
        slot = _scroll_key(state)
        current = offsets.get(slot, 0)
        viewport = int(event.get("viewport") or _viewport(frame, state.active_region) or 1)
        delta = {"up": -1, "k": -1, "down": 1, "j": 1, "pageup": -viewport, "pagedown": viewport}.get(key, 0)
        limit = event.get("max_scroll", dict(getattr(frame, "scroll_max", ()) or ()).get(slot))
        if key == "home":
            current = 0
        elif key == "end":
            current = max(0, int(limit if limit is not None else current))
        else:
            current = max(0, current + delta)
            if limit is not None:
                current = min(current, max(0, int(limit)))
        offsets[slot] = current
        return replace(state, scrolls=tuple(sorted(offsets.items())))
    if key in ("enter", "\n"):
        return _enter(state, rows)
    return state
