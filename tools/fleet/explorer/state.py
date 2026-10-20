"""Pure viewer-state schema conversion and Explorer keyboard reducer."""

from dataclasses import dataclass, replace
from collections.abc import Mapping


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

    def scroll_map(self):
        return dict(self.scrolls)

    def fold_set(self):
        return set(self.nav_fold)


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
            nav_fold=tuple(str(x) for x in data.get("nav_fold", ()) or ()),
            scrolls=_clean_scrolls(data.get("scrolls", {})),
            nav_visible=bool(data.get("nav_visible", True)),
            events_visible=bool(data.get("events_visible", True)),
            search=str(data.get("search") or ""),
            tab_order=tuple(str(x) for x in data.get("tab_order", ()) or ()),
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
    active = "nav" if data.get("process_view") in (None, "group", "process") else "nav"
    return ExplorerState(selection=str(selection), active_region=active,
                         nav_fold=tuple(str(x) for x in folds),
                         scrolls=_clean_scrolls(offsets), nav_visible=True)


def serialize(state):
    """Emit a stable schema-2 viewer payload; no frame pixels are persisted."""
    state = state if isinstance(state, ExplorerState) else restore(state)
    return {
        "schema_version": 2,
        "selection": state.selection,
        "tab": state.tab,
        "stage": state.stage,
        "active_region": state.active_region,
        "nav_fold": list(state.nav_fold),
        "scrolls": dict(state.scrolls),
        "nav_visible": state.nav_visible,
        "events_visible": state.events_visible,
        "search": state.search,
        "tab_order": list(state.tab_order),
        "frame_layout": {"schema_version": 2},
    }


def reconcile(state, tabs):
    """Keep an existing tab, otherwise select its nearest surviving old neighbor."""
    state = state if isinstance(state, ExplorerState) else restore(state)
    identities = [str(getattr(tab, "identity", tab.get("identity", "")
                              if isinstance(tab, Mapping) else "")) for tab in tabs or ()]
    if not identities or state.tab in identities:
        return replace(state, tab_order=tuple(identities))
    old = list(state.tab_order)
    try:
        old_index = old.index(state.tab)
    except ValueError:
        old_index = 0
    preferred = old[old_index + 1:] + list(reversed(old[:old_index]))
    selected = next((identity for identity in preferred if identity in identities), identities[0])
    return replace(state, tab=selected, tab_order=tuple(identities))


def _frame(frame):
    if isinstance(frame, Mapping):
        return frame
    return {}


def reduce_key(state, key, frame=None):
    """Apply one key/event and return a new state; performs no I/O."""
    state = state if isinstance(state, ExplorerState) else restore(state)
    event = key if isinstance(key, Mapping) else {"key": key}
    key = event.get("key")
    if key in ("resize", "RESIZE"):
        cols = int(event.get("cols", 100))
        return replace(state, nav_visible=bool(state.nav_visible and cols >= 100))
    if key in ("e", "E"):
        return replace(state, nav_visible=not state.nav_visible)
    if key in ("F", "f"):
        return replace(state, events_visible=not state.events_visible)
    if key in ("left", "h", "right", "l"):
        order = ("nav", "ledger", "output", "events", "system")
        index = order.index(state.active_region) if state.active_region in order else 0
        step = -1 if key in ("left", "h") else 1
        return replace(state, active_region=order[(index + step) % len(order)])
    if key in ("/", "search"):
        return replace(state, search="")
    if key == "escape" and state.search:
        return replace(state, search="")
    if isinstance(key, str) and key.startswith("text:") and state.search is not None:
        return replace(state, search=state.search + key[5:])
    if key in ("up", "k", "down", "j", "pageup", "pagedown", "home", "end"):
        nav_rows = event.get("nav_rows") or ()
        if state.active_region == "nav" and key in ("up", "k", "down", "j") and nav_rows:
            identities = [str(getattr(row, "identity", "")) for row in nav_rows]
            try:
                index = identities.index(state.selection)
            except ValueError:
                index = 0 if key in ("down", "j") else len(identities) - 1
                return replace(state, selection=identities[index])
            step = -1 if key in ("up", "k") else 1
            return replace(state, selection=identities[max(0, min(len(identities) - 1, index + step))])
        offsets = state.scroll_map()
        identity = state.tab if state.active_region in ("ledger", "output") and state.tab else state.active_region
        current = offsets.get(identity, 0)
        viewport = int(event.get("viewport", 1) or 1)
        delta = {"up": -1, "k": -1, "down": 1, "j": 1,
                 "pageup": -viewport, "pagedown": viewport}.get(key, 0)
        if key == "home": current = 0
        elif key == "end": current = max(0, int(event.get("max_scroll", current)))
        else: current = max(0, current + delta)
        offsets[identity] = current
        return replace(state, scrolls=tuple(sorted(offsets.items())))
    if key in ("enter", "\n"):
        folds = state.fold_set()
        selected = state.selection
        if selected:
            folds.symmetric_difference_update((selected,))
        return replace(state, nav_fold=tuple(sorted(folds)))
    return state
