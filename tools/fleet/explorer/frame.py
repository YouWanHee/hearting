"""Pure geometry and fault-isolated composition for the Fleet explorer."""

from collections.abc import Mapping
import inspect
from typing import Callable, Iterable, Optional

from .output import cell_width, clip
from .types import Frame, Hit, NavRow, Rect


def _layout(cols: int, rows: int, nav_visible: bool,
            active_region: str = "nav") -> dict[str, Rect]:
    cols, rows = max(1, int(cols)), max(1, int(rows))
    top = min(2, rows)
    bottom = min(4, max(0, rows - top))
    # Keep the design's one-row gap between the body and the bottom bands.
    body_h = max(0, rows - top - bottom - 1)
    regions = {"header": Rect(0, 0, cols, top)}
    nav_w = min(40, max(0, round(cols * 0.26))) if nav_visible and cols >= 100 else 0
    if nav_w and cols - nav_w >= 2:
        regions["nav"] = Rect(0, top, nav_w, body_h)
        main_x, main_w = nav_w + 1, cols - nav_w - 1
    else:
        regions["nav"] = Rect(0, top, 0, body_h)
        main_x, main_w = 0, cols
    if main_w >= 125:
        ledger_w = min(62, max(1, int((main_w - 1) * 0.22)))
        if active_region == "output":
            ledger_w = min(5, ledger_w)
        regions["ledger"] = Rect(main_x, top, ledger_w, body_h)
        regions["output"] = Rect(main_x + ledger_w + 1, top, main_w - ledger_w - 1, body_h)
    else:
        ledger_h = min(body_h, max(0, round(body_h * 0.46)))
        regions["ledger"] = Rect(main_x, top, main_w, ledger_h)
        regions["output"] = Rect(main_x, top + ledger_h + (1 if ledger_h < body_h else 0), main_w,
                                  max(0, body_h - ledger_h - (1 if ledger_h < body_h else 0)))
    event_h = 1 if bottom >= 3 else 0
    regions["events"] = Rect(0, rows - bottom, cols, event_h)
    regions["legend"] = Rect(0, rows - bottom + event_h, cols, max(0, bottom - event_h - 1))
    regions["keys"] = Rect(0, rows - 1, cols, 1)
    return regions


def _lines(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(value.splitlines()) or (value,)
    return tuple(str(line) for line in value)


def _put(canvas: list[list[str]], x: int, y: int, width: int, height: int, text_rows: Iterable[str]) -> None:
    for row_index, text in enumerate(text_rows):
        if row_index >= height or y + row_index >= len(canvas) or width <= 0:
            break
        rendered = clip(str(text), width)
        col = x
        for char in rendered:
            cw = cell_width(char)
            if col + cw > x + width:
                break
            if 0 <= col < len(canvas[y + row_index]):
                canvas[y + row_index][col] = char
            for extra in range(1, cw):
                if col + extra < len(canvas[y + row_index]):
                    canvas[y + row_index][col + extra] = "\x00"
            col += cw


def _call(provider, rect, snapshot, focus_result, state, now):
    if not callable(provider):
        return _lines(provider)
    try:
        signature = inspect.signature(provider)
        count = len([p for p in signature.parameters.values()
                     if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)])
    except (TypeError, ValueError):
        count = 5
    args = (rect, snapshot, focus_result, state, now)
    return _lines(provider(*args[:count]))


def compose(snapshot=None, focus_result=None, state=None, cols=100, rows=45, *, providers=None,
            last_good=None, now=None) -> Frame:
    """Compose fixed regions without I/O or dependencies on render.py module state.

    Providers may be plain row sequences or pure callables. A provider exception only
    affects its region; that region reuses its own last-good rows when available.
    """
    cols, rows = max(1, int(cols)), max(1, int(rows))
    nav_visible = bool(getattr(state, "nav_visible", True)) if state is not None else True
    active_region = getattr(state, "active_region", "nav") if state is not None else "nav"
    if isinstance(state, Mapping):
        nav_visible = bool(state.get("nav_visible", True))
        active_region = str(state.get("active_region", "nav"))
    regions = _layout(cols, rows, nav_visible, active_region)
    providers = providers or {}
    if isinstance(last_good, Frame):
        previous = dict(last_good.last_good)
        if not previous:
            previous = {name: tuple(line for line in last_good.lines[rect.y:rect.y + rect.h])
                        for name, rect in last_good.regions}
    else:
        previous = {name: _lines(value) for name, value in (last_good or {}).items()}
    current, errors = {}, []
    clock = now
    for name, rect in regions.items():
        if rect.w <= 0 or rect.h <= 0:
            current[name] = ()
            continue
        try:
            current[name] = _call(providers.get(name, ()), rect, snapshot, focus_result, state, clock)
        except Exception as exc:  # a single section must not blank the rest of the frame
            errors.append((name, f"{type(exc).__name__}: {exc}"))
            current[name] = previous.get(name, (f"{name}: 그리지 못함",))

    canvas = [[" "] * cols for _ in range(rows)]
    for name, rect in regions.items():
        if name in ("nav", "ledger", "output") and rect.w and rect.h:
            _put(canvas, rect.x, rect.y, rect.w, rect.h, current[name])
    # Borders occupy one terminal cell and never reduce the declared canvas geometry.
    nav, ledger, output = regions["nav"], regions["ledger"], regions["output"]
    if nav.w and cols > nav.x + nav.w:
        for y in range(nav.y, nav.y + nav.h):
            canvas[y][nav.x + nav.w] = "┃"
    if ledger.y == output.y and ledger.w and output.w:
        border_x = ledger.x + ledger.w
        for y in range(ledger.y, min(rows, ledger.y + ledger.h)):
            if border_x < cols:
                canvas[y][border_x] = "│"
    elif ledger.w and output.w and ledger.h < output.y - ledger.y:
        border_y = output.y - 1
        for x in range(ledger.x, min(cols, ledger.x + ledger.w)):
            canvas[border_y][x] = "─"
    for name in ("header", "events", "legend", "keys"):
        rect = regions[name]
        _put(canvas, rect.x, rect.y, rect.w, rect.h, current.get(name, ()))
    lines = tuple("".join(char for char in row if char != "\x00") for row in canvas)
    return Frame(lines=lines, regions=tuple(regions.items()),
                 hits=tuple(getattr(state, "hits", ()) or ()),
                 nav_rows=tuple(getattr(state, "nav_rows", ()) or ()),
                 last_good=tuple((name, rows) for name, rows in current.items()),
                 errors=tuple(errors), geometry=(cols, rows))
