"""Compact view-local state and reducer for the Fleet explorer."""

from dataclasses import dataclass, replace
from collections.abc import Mapping


@dataclass(frozen=True)
class NavState:
    view: str = "OVERVIEW"
    scope: str = "-"
    opened: str = ""
    usage_open: bool = False
    scroll: tuple = ()
    title: str = "OVERVIEW"
    status: str = ""
    parents: tuple = ()
    folds: tuple = ()

    def fold_map(self):
        return dict(self.folds)

    def opened_for(self, key, default=""):
        return self.fold_map().get(str(key), default)

    @classmethod
    def from_value(cls, value=None):
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return cls()
        names = set(cls.__dataclass_fields__)
        values = {key: value[key] for key in names if key in value}
        for key in ("folds", "scroll"):
            values[key] = tuple(tuple(item) for item in values.get(key, ()))
        values["parents"] = tuple(values.get("parents", ()))
        return cls(**values)


VIEWS = ("OVERVIEW", "PEERS", "ROUTES", "QUESTIONS", "HISTORY", "COMPUTE", "MEMORY", "MODELS", "SYSTEM")


def reduce(state, event, frame=None):
    """Apply one normalized input or hit-map action and preserve its order."""
    state = NavState.from_value(state)
    if isinstance(event, str):
        event = {"type": "key", "key": event}
    if not isinstance(event, Mapping):
        return state, ()
    kind, key = event.get("type", "key"), event.get("key")
    if kind in ("click", "action"):
        action = tuple(event.get("action", ()))
        if not action:
            return state, ()
        if action[0] in ("view", "tab") and len(action) > 1:
            view = str(action[1])
            return replace(state, view=view, scope="-", opened="", title=view), ()
        if action[0] == "fold" and len(action) > 2:
            slot, identity = str(action[1]), str(action[2])
            folds = state.fold_map()
            folds[slot] = "" if folds.get(slot, "") == identity else identity
            opened = folds[slot] if slot == "sopen:OVERVIEW" else state.opened
            return replace(state, opened=opened, folds=tuple(sorted(folds.items()))), ()
        if action[0] == "ui" and len(action) > 2:
            folds = state.fold_map()
            folds["ui:" + str(action[1])] = str(action[2])
            return replace(state, folds=tuple(sorted(folds.items()))), ()
        if action[0] == "usage":
            return replace(state, usage_open=not state.usage_open), ()
        if action[0] in ("page", "scope") and len(action) > 1:
            scope = str(action[1]) if action[0] == "scope" else f"{action[1]}:{action[2]}"
            parents = state.parents + (state.scope,)
            return replace(state, scope=scope, opened="", title=scope.split(":", 1)[-1],
                           parents=parents), ()
        if action[0] == "route" and len(action) > 1:
            return replace(state, scope=f"route:{action[1]}", opened="",
                           title=str(action[1]), parents=state.parents + (state.scope,)), ()
        if action[0] == "pane" and len(action) > 2:
            return state, (("focus", action[1], action[2]),)
        if action[0] == "root" and len(action) > 1:
            view = str(action[1])
            folds = state.fold_map()
            if len(action) > 2 and isinstance(action[2], Mapping):
                opened = str(action[2].get("ropen:" + view, ""))
                if opened:
                    folds["ropen:" + view] = opened
            return replace(state, view=view, scope="-", opened="",
                           title=view, folds=tuple(sorted(folds.items()))), ()
        return state, ()
    if kind != "key":
        return state, ()
    if key in ("tab", "shift-tab", "left", "right"):
        step = -1 if key in ("shift-tab", "left") else 1
        index = VIEWS.index(state.view) if state.view in VIEWS else 0
        view = VIEWS[(index + step) % len(VIEWS)]
        return replace(state, view=view, scope="-", opened="", title=view), ()
    if key in ("escape", "esc"):
        if state.scope != "-":
            parent = event.get("parent", state.parents[-1] if state.parents else "-")
            parents = state.parents[:-1] if state.parents else ()
            return replace(state, scope=str(parent), opened="", parents=parents,
                           title=(str(parent).split(":", 1)[-1] if parent != "-" else state.view)), ()
        if state.opened:
            return replace(state, opened=""), ()
        return state, ()
    if key in ("u", "usage"):
        return replace(state, usage_open=not state.usage_open), ()
    if key in ("enter", "\n"):
        hit = event.get("hit")
        action = hit.action if hasattr(hit, "action") else hit
        return reduce(state, {"type": "click", "action": action}, frame)
    return state, ()


def serialize(state):
    """Persist only explorer view state; old B cursors are ignored on read."""
    state = NavState.from_value(state)
    return {"schema_version": 3, "view": state.view, "scope": state.scope,
            "opened": state.opened, "usage_open": state.usage_open,
            "scroll": [list(item) for item in state.scroll], "title": state.title,
            "status": state.status, "parents": list(state.parents),
            "folds": [list(item) for item in state.folds],
            "frame_layout": {"schema_version": 3}}


def restore(value=None):
    return NavState.from_value(value)
