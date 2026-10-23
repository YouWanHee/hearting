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
    drag: tuple = ()

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
        for key in ("folds", "scroll", "drag"):
            values[key] = tuple(tuple(item) for item in values.get(key, ()))
        values["parents"] = tuple(values.get("parents", ()))
        return cls(**values)


VIEWS = ("OVERVIEW", "PEERS", "ROUTES", "QUESTIONS", "HISTORY", "COMPUTE", "MEMORY", "MODELS", "SYSTEM")


def reduce(state, event, frame=None):
    """Apply one normalized input or hit-map action and preserve its order."""
    before = NavState.from_value(state)
    after, effects = _reduce(before, event, frame)
    folds = after.fold_map()
    if (after.scope, after.view) != (before.scope, before.view):
        # A generation pick belongs to the page it was made on.
        folds = {key: value for key, value in folds.items() if not key.startswith("ui:gen:")}
    # The open OVERVIEW session is a view-local fold; it is not cleared by moving away.
    after = replace(after, opened=str(folds.get("sopen:OVERVIEW") or ""),
                    folds=tuple(sorted(folds.items())))
    return after, effects


def _reduce(state, event, frame=None):
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
            if action[1] == "sel":
                folds = {key: value for key, value in folds.items()
                         if key not in ("ui:sel", "ui:model", "ui:effort")}
            folds["ui:" + str(action[1])] = str(action[2])
            return replace(state, folds=tuple(sorted(folds.items()))), ()
        if action[0] == "usage":
            return replace(state, usage_open=not state.usage_open), ()
        if action[0] in ("page", "scope") and len(action) > 1:
            scope = str(action[1]) if action[0] == "scope" else f"{action[1]}:{action[2]}"
            if action[0] == "scope" and scope != "-" and ":" not in scope:
                scope = "session:" + scope
            parents = state.parents + (state.scope,)
            return replace(state, scope=scope, opened="", title=scope.split(":", 1)[-1],
                           parents=parents), ()
        if action[0] == "route" and len(action) > 1:
            offsets = dict(state.scroll)
            offsets["rawoff"] = 0
            return replace(state, scope=f"route:{action[1]}", opened="",
                           title=str(action[1]), parents=state.parents + (state.scope,),
                           scroll=tuple(sorted(offsets.items()))), ()
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
        if kind == "wheel":
            x, y = int(event.get("x", -1)), int(event.get("y", -1))
            hit = next((item for item in getattr(frame, "hits", ())
                        if item.y == y and item.x0 <= x < item.x1 and item.action[:1] == ("scroll",)), None)
            if hit is None:
                return state, ()
            _name, slot, maximum, *sign = hit.action
            direction = int(event.get("direction", 0))
            amount = 3 * direction * (int(sign[0]) if sign else 1)
            offsets = dict(state.scroll)
            offsets[slot] = max(0, min(int(maximum), int(offsets.get(slot, 0)) + amount))
            return replace(state, scroll=tuple(sorted(offsets.items()))), ()
        if kind == "mouse":
            phase = event.get("phase")
            x, y = int(event.get("x", -1)), int(event.get("y", -1))
            button = int(event.get("button", 0))
            offsets = dict(state.scroll)
            if phase == "release":
                return replace(state, drag=()), ()
            if event.get("motion") and state.drag:
                slot, maximum, origin_y, origin_offset, sign = state.drag
                offsets[slot] = max(0, min(int(maximum), int(origin_offset) +
                                           (y - int(origin_y)) * int(sign)))
                return replace(state, scroll=tuple(sorted(offsets.items()))), ()
            if phase != "press" or button != 0:
                return state, ()
            hit = next((item for item in getattr(frame, "hits", ())
                        if item.y == y and item.x0 <= x < item.x1), None)
            if hit is None:
                return state, ()
            if hit.action[:1] == ("scroll",):
                _name, slot, maximum, *sign = hit.action
                return replace(state, drag=(slot, int(maximum), y,
                                            min(int(offsets.get(slot, 0)), int(maximum)),
                                            int(sign[0]) if sign else 1)), ()
            return reduce(state, {"type": "click", "action": hit.action}, frame)
        return state, ()
    if key in ("tab", "shift-tab", "left", "right"):
        if state.scope != "-":
            return replace(state, scope="-", opened=""), ()
        step = -1 if key in ("shift-tab", "left") else 1
        tabs = tuple(str(item) for item in event.get("tabs", VIEWS))
        if not tabs:
            return state, ()
        index = tabs.index(state.view) if state.view in tabs else 0
        view = tabs[(index + step) % len(tabs)]
        return replace(state, view=view, scope="-", opened="", title=view), ()
    if key in ("escape", "esc"):
        if state.scope != "-":
            parent = event.get("parent")
            if parent is None:
                parent_hit = next((hit for hit in getattr(frame, "hits", ())
                                   if hit.y == -1 and hit.action[:1] == ("parent",)), None)
                parent = parent_hit.action[1] if parent_hit else (
                    state.parents[-1] if state.parents else "-")
            if parent not in (None, "-") and ":" not in str(parent):
                parent = "session:" + str(parent)
            parents = state.parents[:-1] if state.parents else ()
            return replace(state, scope=str(parent), opened="", parents=parents,
                           title=(str(parent).split(":", 1)[-1] if parent != "-" else state.view)), ()
        return state, ()
    if key in ("backspace", "delete"):
        return reduce(state, {"type": "key", "key": "escape"}, frame)
    if key in ("up", "down"):
        order = tuple(str(item) for item in event.get("order", ()))
        if not order:
            return state, ()
        step = -1 if key == "up" else 1
        index = order.index(state.scope) + step if state.scope in order else (0 if step > 0 else -1)
        scope = order[index % len(order)]
        return replace(state, scope=scope, opened="", title=scope.partition(":")[2] or scope), ()
    if key in ("u", "usage"):
        return replace(state, usage_open=not state.usage_open), ()
    if key in ("/", "F"):
        return state, (("command", "filter" if key == "/" else "feed"),)
    if key in ("enter", "\n"):
        hit = event.get("hit")
        action = hit.action if hasattr(hit, "action") else hit
        if state.scope == "-" and not action:
            return state, ()
        if state.scope != "-":
            tag = state.scope.partition(":")[2]
            pane_hit = next((item for item in getattr(frame, "hits", ())
                             if item.action[:1] == ("pane",) and
                             len(item.action) > 2 and str(item.action[1]) == tag), None)
            pane = pane_hit.action[2] if pane_hit else ""
            return state, (("focus", tag, pane),)
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
