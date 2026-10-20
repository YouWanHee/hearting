"""Pure geometry and fault-isolated composition for the Fleet explorer.

``compose`` takes the snapshot, the selected-scope result and the viewer state and returns the
whole screen as text. It never reads a file, a database or the registry and never touches the
renderer's module state, so it can run on the curses thread. Each region (and each tab body) is
drawn on its own: when one raises, only that region shows ``그리지 못함`` with its own previous
rows and their age, and every other region keeps being redrawn.
"""

import inspect
from typing import Iterable

from . import events, focus, ledger, nav, overview, requests, system
from .output import cell_width, clip, render_output
from .state import coerce
from .types import Frame, Hit, Rect

NAV_COLS = 100
SPLIT_COLS = 125
LEDGER_COLS = 62
NARROW_LEDGER = 5
SEPARATOR_CHAR = "━"
KEYS_TEXT = "↑↓ 이동 · ←→ 영역 · Enter 펼침 · Tab 탭 · / 찾기 · e 탐색 · F 사건 · q 끝"
BODY_REGIONS = ("ledger", "relations", "system", "tabs")


def page_of(state):
    """Which page the body shows. The focused region picks 시스템 / 관계 / 요청 목록; otherwise the tab does."""
    region = getattr(state, "active_region", "nav")
    if region == "system":
        return "system"
    if region == "relations":
        return "relations"
    if region == "tabs":
        return "overflow"
    tab = getattr(state, "tab", "")
    if not tab:
        return "overview"
    return "history" if tab in (getattr(state, "history", ()) or ()) else "ledger"


def _layout(cols, rows, nav_visible, active_region="nav", split=True, events_visible=True):
    cols, rows = max(1, int(cols)), max(1, int(rows))
    top = min(2, rows)
    show_events = bool(events_visible)
    bottom = min(5 if show_events else 3, max(0, rows - top - 1))
    show_events = show_events and bottom >= 5
    body_h = max(0, rows - top - bottom)
    regions = {"header": Rect(0, 0, cols, top)}

    if nav_visible:
        nav_w = min(40, max(0, round(cols * 0.26))) if cols >= NAV_COLS else cols
    else:
        nav_w = 0
    if nav_w >= cols:
        regions["nav"] = Rect(0, top, cols, body_h)
        main_x, main_w = cols, 0
    elif nav_w:
        regions["nav"] = Rect(0, top, nav_w, body_h)
        main_x, main_w = nav_w + 1, cols - nav_w - 1
    else:
        regions["nav"] = Rect(0, top, 0, body_h)
        main_x, main_w = 0, cols

    head_h = 1 if (main_w > 0 and body_h >= 1) else 0
    tabs_h = 1 if (main_w > 0 and body_h >= 2) else 0
    regions["request"] = Rect(main_x, top, main_w, head_h)
    regions["tabs"] = Rect(main_x, top + 1, main_w, tabs_h)
    content_y = top + head_h + tabs_h
    content_h = max(0, body_h - head_h - tabs_h)
    if not split or main_w <= 0:
        regions["ledger"] = Rect(main_x, content_y, main_w, content_h)
        regions["output"] = Rect(main_x, content_y + content_h, 0, 0)
    elif main_w >= SPLIT_COLS:
        ledger_w = NARROW_LEDGER if active_region == "output" else min(LEDGER_COLS, main_w // 2)
        regions["ledger"] = Rect(main_x, content_y, ledger_w, content_h)
        regions["output"] = Rect(main_x + ledger_w + 1, content_y, main_w - ledger_w - 1, content_h)
    else:
        ledger_h = min(content_h, max(0, round(content_h * 0.46)))
        gap = 1 if ledger_h < content_h else 0
        regions["ledger"] = Rect(main_x, content_y, main_w, ledger_h)
        regions["output"] = Rect(main_x, content_y + ledger_h + gap, main_w,
                                 max(0, content_h - ledger_h - gap))

    y = top + body_h
    legend_h = max(0, bottom - 1 - (2 if show_events else 0))
    regions["legend"] = Rect(0, y, cols, legend_h)
    y += legend_h
    if show_events:
        regions["separator"] = Rect(0, y, cols, 1)
        regions["events"] = Rect(0, y + 1, cols, 1)
        y += 2
    else:
        regions["separator"] = Rect(0, y, cols, 0)
        regions["events"] = Rect(0, y, cols, 0)
    regions["keys"] = Rect(0, y, cols, 1 if bottom >= 1 else 0)
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


def _previous(last_good):
    """(rows by key, time of last success by key) from the previous frame or a plain mapping."""
    if isinstance(last_good, Frame):
        rows = dict(last_good.last_good)
        if not rows:
            rows = {name: tuple(last_good.lines[rect.y:rect.y + rect.h]) for name, rect in last_good.regions}
        return rows, dict(last_good.good_at)
    return {name: _lines(value) for name, value in (last_good or {}).items()}, {}


def _age_phrase(now, stamp):
    if now is None or stamp is None:
        return "직전 화면 나이 ?"
    return f"직전 화면 {system.age_seconds(now - stamp)} 전"


def _failed_rows(previous, good_at, key, now):
    """What a region shows when it could not be drawn: its own previous rows, with their age."""
    rows = previous.get(key)
    if not rows:
        return ("그리지 못함 · 직전 화면 없음",)
    return (f"그리지 못함 · {_age_phrase(now, good_at.get(key))}", "직전 화면") + tuple(rows)


def _scroll(lines, state, slot, height):
    offset = max(0, min(int(dict(getattr(state, "scrolls", ()) or ()).get(slot, 0) or 0),
                        max(0, len(lines) - max(0, height))))
    return tuple(lines[offset:offset + max(0, height)])


def _body_slot(state):
    region = state.active_region if state.active_region in BODY_REGIONS else "ledger"
    return f"{region}:{state.tab}"


# -- default region content -----------------------------------------------------------------------

def _waiting(focus_result, now):
    """What a region that has no data yet says: still reading, never read successfully, or last-good age."""
    health = getattr(focus_result, "health", None)
    return (focus.status_text(health, now) if health is not None else "") or "읽는 중"


def _unread(focus_result):
    """True for a placeholder result that carries no data (still loading, or never read successfully)."""
    health = getattr(focus_result, "health", None)
    return getattr(health, "state", "") in ("loading", "failed")


def _matching_ledger(focus_result, state):
    view = getattr(focus_result, "request_ledger", None)
    return view if view is not None and state.tab and view.request_id == state.tab else None


def _relation_head(snapshot, focus_result, now):
    lines = []
    health = focus_result.source_health("relations") if hasattr(focus_result, "source_health") else None
    if health is not None and health.state == "failed":
        when = f" {system.age_seconds(now - health.last_success_at)}" if (
            now is not None and health.last_success_at is not None) else ""
        lines.append(f"관계 읽지 못함{when} · 직전 값")
    stewards = [s for s in nav.ordered_mains(snapshot) if nav._get(s, "steward")]
    parts = []
    for session in stewards:
        targets = nav._get(session, "steward_targets", default=()) or ()
        tag = nav._get(session, "session_tag")
        parts.append(f"[{tag}] ⚑ 관찰 대상 {len(targets)}")
    if parts:
        lines.append(" · ".join(parts))
    lines.append("실행 관계 / 최근 메시지")
    return lines


def _overview_lines(snapshot, focus_result):
    cached = tuple(getattr(focus_result, "overview", ()) or ())
    return cached or overview.build_overview(snapshot, focus_result)


def _body_lines(page, snapshot, focus_result, state, now):
    if page == "system":
        return system.system_lines(snapshot)
    if page == "overview":
        return _overview_lines(snapshot, focus_result)
    if page == "relations":
        return tuple(_relation_head(snapshot, focus_result, now)) + tuple(_overview_lines(snapshot, focus_result))
    if page == "overflow":
        tabs = tuple(getattr(focus_result, "tabs", ()) or ())
        keep = {state.tab, nav._selected_request(state, focus_result)} | set(state.keep)
        return requests.overflow_lines(
            tabs, getattr(focus_result, "header", None) or requests.RequestHeader(), keep=keep,
            failures=requests.open_result_lines(getattr(focus_result, "open_results", ()) or ()))
    view = _matching_ledger(focus_result, state)
    if view is None:
        return (_waiting(focus_result, now),)
    return ledger.history_lines(view) if page == "history" else ledger.ledger_lines(view, state.stage or None)


def _resolver(snapshot):
    """The caller's cached, git-aware project-name helper (``snapshot.project_of``), when it supplies one."""
    found = nav._get(snapshot, "project_of")
    return found if callable(found) else None


def _head_text(snapshot, focus_result):
    sid = nav._focus_sid(focus_result)
    session = nav.find_session(snapshot, sid) if sid else None
    header = getattr(focus_result, "header", None)
    if _unread(focus_result):
        header = False                          # a session is selected; its request counts are not read yet
    if session is None or header is None:
        return "요청 ? · 선택한 세션 없음"
    return requests.head_line(nav._get(session, "session_tag"), nav.role_name(session),
                              nav.project_name(nav._get(session, "cwd", "project"), _resolver(snapshot)),
                              header or None)


def _tab_text(focus_result, state, width):
    tabs = tuple(getattr(focus_result, "tabs", ()) or ())
    selected = nav._selected_request(state, focus_result)
    return requests.tab_bar(tabs, active=state.tab, selected=selected or None, keep=state.keep, width=width)


def output_pane(focus_result, state, rect, now=None):
    """(rows, how far the output may scroll) of the raw-output pane.

    The title and what the sources said about themselves stay on top; the records below scroll on
    their own and follow the newest ones until the person scrolls.
    """
    records = tuple(getattr(focus_result, "output", ()) or ())
    notes = tuple(getattr(focus_result, "output_notes", ()) or ())
    title = getattr(focus_result, "output_title", "") or ""
    health = getattr(focus_result, "output_health", None)
    if not (title or records or notes):
        return None, 0
    head = [title] + list(notes)
    if health is not None and health.state in ("stale", "failed", "partial"):
        text = focus.status_text(health, now)
        head.append(text if health.state != "partial" else
                    "일부 읽지 못함" + (f" · {health.error}" if health.error else ""))
    if not records and not notes and (health is None or health.state in ("success", "partial")):
        head.append("출력 기록 없음")
    body_h = max(0, rect.h - len(head))
    scroll = dict(state.scrolls).get(f"output:{state.tab}")
    rows = render_output(records, rect.w, body_h, scroll=scroll)
    return tuple(head) + rows, max(0, len(records) - body_h)


def _output_lines(snapshot, focus_result, state, rect, now=None):
    rows, _limit = output_pane(focus_result, state, rect, now)
    if rows is not None:
        return rows
    view = _matching_ledger(focus_result, state)
    if view is None:
        return (_waiting(focus_result, now),)
    return ledger.summary_lines(view, state.stage or None)


def legend_lines():
    life = " ".join(f"{ledger.LIVENESS_GLYPH[k]} {ledger.LIVENESS_WORD[k]}"
                    for k in ("working", "idle", "unknown", "blocked"))
    result = " ".join(f"{ledger.OUTCOME_GLYPH[k]} {ledger.OUTCOME_WORD[k]}"
                      for k in ("pending", "succeeded", "failed", "review-blocked", "cancelled"))
    harness = " ".join(f"{abbr} {name}" for abbr, name in (("CL", "Claude"), ("CX", "Codex"), ("OC", "OpenCode")))
    return (f"생존 {life} │ 결과 {result}",
            f"{ledger.GATE_GLYPH['passed']} 통과 / {ledger.GATE_GLYPH['hold']} 판정 보류 │ ▸? 요청 수 펼칠 때 읽음 │ {harness}")


def compose(snapshot=None, focus_result=None, state=None, cols=100, rows=45, *, providers=None,
            last_good=None, now=None) -> Frame:
    """Compose the fixed regions without I/O or dependencies on render.py module state.

    ``providers`` may replace any region with plain rows or a pure callable. A provider (or
    default renderer) exception only affects its region; that region shows its own previous
    rows and their age when ``last_good`` (the previous Frame) has them.
    """
    cols, rows = max(1, int(cols)), max(1, int(rows))
    state = coerce(state)
    providers = dict(providers or {})
    page = page_of(state)
    # A caller that supplies its own output renderer always gets an output pane (the legacy entry
    # points do); otherwise only the ledger page has one — the other pages use the whole body.
    split = page == "ledger" or "output" in providers
    layout = lambda is_split: _layout(cols, rows, state.nav_visible, state.active_region,
                                      is_split, state.events_visible)
    regions = layout(split)
    previous, good_at = _previous(last_good)
    current, errors, key_of = {}, [], {}
    nav_rows = ()
    nav_offset = 0
    extent = {}

    def default(name, rect):
        if name == "header":
            return system.status_lines(snapshot, focus_result, now, rect.w)
        if name == "request":
            return (_head_text(snapshot, focus_result),)
        if name == "tabs":
            return (_tab_text(focus_result, state, rect.w),)
        if name == "ledger":
            lines = _body_lines(page, snapshot, focus_result, state, now)
            if split and rect.w <= NARROW_LEDGER + 3 and _matching_ledger(focus_result, state) is not None:
                lines = ledger.narrow_ledger_lines(_matching_ledger(focus_result, state))
            extent[_body_slot(state)] = max(0, len(lines) - rect.h)
            return _scroll(lines, state, _body_slot(state), rect.h)
        if name == "output":
            extent[f"output:{state.tab}"] = output_pane(focus_result, state, rect, now)[1]
            return _output_lines(snapshot, focus_result, state, rect, now)
        if name == "separator":
            return (SEPARATOR_CHAR * rect.w,)
        if name == "events":
            records = tuple(getattr(focus_result, "events", ()) or ())
            return (events.event_bar(records, rect.w) if records else "사건 없음",)
        if name == "legend":
            return legend_lines()
        if name == "keys":
            return (KEYS_TEXT,)
        return ()

    def run(name, rect):
        key = key_of.get(name, name)
        try:
            if name in providers:
                value = _call(providers[name], rect, snapshot, focus_result, state, now)
            else:
                value = default(name, rect)
            return tuple(value), None
        except Exception as exc:  # a single section must not blank the rest of the frame
            errors.append((name, f"{type(exc).__name__}: {exc}"))
            return _failed_rows(previous, good_at, key, now), exc

    key_of["ledger"] = f"ledger:{state.tab or 'overview'}:{page}"
    failed = set()

    def draw_nav(rect):
        nonlocal nav_rows, nav_offset
        if "nav" in providers:
            return _call(providers["nav"], rect, snapshot, focus_result, state, now)
        all_rows = nav.build_nav(snapshot, focus_result, state, width=rect.w,
                                 project_resolver=_resolver(snapshot))
        nav_rows = tuple(nav.visible_rows(all_rows, state))
        text, nav_offset = nav.nav_lines(nav_rows, state, rect.w, rect.h)
        extent["nav"] = max(0, len(nav_rows) - max(0, rect.h - 3))
        return text

    order = ("header", "nav", "request", "tabs", "ledger", "output", "separator", "events", "legend", "keys")
    for name in order:
        rect = regions[name]
        if rect.w <= 0 or rect.h <= 0:
            current[name] = ()
            continue
        if name == "nav":
            try:
                current[name] = tuple(draw_nav(rect))
            except Exception as exc:
                errors.append((name, f"{type(exc).__name__}: {exc}"))
                failed.add(name)
                current[name] = _failed_rows(previous, good_at, "nav", now)
            continue
        if name == "output" and "ledger" in failed:
            current[name] = ()
            continue
        value, exc = run(name, rect)
        current[name] = value
        if exc is not None:
            failed.add(name)
            if name == "ledger" and split:           # the tab's body failed: its previous screen stands alone
                split = False
                regions = layout(False)
                current["output"] = ()

    # Remember each region's rows (the tab body under its tab key) and when they were drawn.
    new_good = dict(previous)
    new_time = dict(good_at)
    for name, value in current.items():
        if name in failed:
            continue
        key = key_of.get(name, name)
        new_good[key] = value
        if now is not None:
            new_time[key] = now

    canvas = [[" "] * cols for _ in range(rows)]
    for name in ("nav", "ledger", "output", "request", "tabs"):
        rect = regions[name]
        if rect.w and rect.h:
            _put(canvas, rect.x, rect.y, rect.w, rect.h, current.get(name, ()))
    # Borders occupy one terminal cell and never reduce the declared canvas geometry.
    nav_rect, led, out = regions["nav"], regions["ledger"], regions["output"]
    if nav_rect.w and cols > nav_rect.x + nav_rect.w:
        for y in range(nav_rect.y, nav_rect.y + nav_rect.h):
            canvas[y][nav_rect.x + nav_rect.w] = "┃"
    if out.w and out.h and led.w and led.h:
        if led.y == out.y:
            border_x = led.x + led.w
            for y in range(led.y, min(rows, led.y + led.h)):
                if border_x < cols:
                    canvas[y][border_x] = "│"
        else:
            for x in range(led.x, min(cols, led.x + led.w)):
                canvas[out.y - 1][x] = "─"
    for name in ("header", "separator", "events", "legend", "keys"):
        rect = regions[name]
        if rect.w and rect.h:
            _put(canvas, rect.x, rect.y, rect.w, rect.h, current.get(name, ()))
    lines = tuple("".join(char for char in row if char != "\x00") for row in canvas)
    window = max(0, nav_rect.h - 3)
    drawn = nav_rows[nav_offset:nav_offset + window]
    hits = tuple(Hit(Rect(nav_rect.x, nav_rect.y + 2 + i, nav_rect.w, 1), row.identity)
                 for i, row in enumerate(drawn)) if (nav_rect.w and "nav" not in failed) else ()
    return Frame(lines=lines, regions=tuple(regions.items()), hits=hits, nav_rows=nav_rows,
                 last_good=tuple(new_good.items()), good_at=tuple(new_time.items()), errors=tuple(errors),
                 geometry=(cols, rows), page=page, nav_offset=nav_offset,
                 scroll_max=tuple(sorted(extent.items())))
