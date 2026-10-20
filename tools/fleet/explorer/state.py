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
        )

    # Schema 1 cursor identities are the closest available nav selection evidence.
    cursor = _mapping(data.get("cursor"))
    selection = cursor.get("_entry_id") or cursor.get("entry_id") or ""
    folds = data.get("folds") or data.get("repo_folds") or ()
    if isinstance(folds, Mapping):
        folds = [key for key, folded in folds.items() if folded]
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
        if key == "home":
            current = 0
        elif key == "end":
            current = max(0, int(event.get("max_scroll", current)))
        else:
            current = max(0, current + delta)
            if "max_scroll" in event:
                current = min(current, max(0, int(event["max_scroll"])))
        offsets[slot] = current
        return replace(state, scrolls=tuple(sorted(offsets.items())))
    if key in ("enter", "\n"):
        return _enter(state, rows)
    return state
