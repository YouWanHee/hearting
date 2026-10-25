"""Header, navigation rail, feed, key legend and status rows."""
import datetime
import math
from collections.abc import Mapping
from .grid import CellGrid, cell_width, clip_cells
from .tables import repo_label

NAV_WIDTH = 32


def count_word(count, word, plural=None):
    """``1 question`` / ``2 questions``: the reference's ``nof`` for every count word on the screen."""
    return f"{count} {word if count == 1 else (plural or word + 's')}"


def _clip(text, width):
    from .output import clip
    return clip(str(text), max(0, int(width)))


def _compact_census_parts(parts):
    """Normalize only fragment separators and dedicated padding fragments."""
    compacted = []
    for text, key in parts:
        text = str(text)
        if not text.strip(" "):
            continue
        text = text.lstrip(" ")
        separator = " " if compacted and not text.startswith("●") else ""
        compacted.append((separator + text, key))
    return compacted


def _host_parts(hosts, *, compact=False):
    if not hosts:
        return []
    selected = hosts[:1] if compact else hosts
    parts = [] if compact else [("hosts ", "label")]
    for host in selected:
        if isinstance(host, Mapping):
            mark, name = host.get("mark", "◇"), host.get("name") or "·"
        else:
            mark, name = (tuple(host) + ("◇", "·"))[:2]
        parts += [(f"{mark} ", "dim" if mark == "◇" else "work"),
                  (str(name), "soft" if mark == "◇" else "work")]
        if not compact:
            parts.append(("  ", "branch_s"))
    if compact and len(hosts) > 1:
        parts.append((f" +{len(hosts) - 1}", "dim"))
    elif not compact:
        parts[-1] = (f" {len(hosts)}/{len(hosts)}", "dim")
    return parts


def _parts_width(parts):
    return sum(cell_width(text) for text, _key in parts)


def _clock_freshness(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(float(value)):
            return None
        return datetime.datetime.fromtimestamp(value).strftime("%H:%M")
    except (OverflowError, OSError, TypeError, ValueError):
        return None


_SPINNER_GLYPHS = frozenset("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⠯⠷⠻")
_CENSUS_STATUS_KEYS = {
    "spin": "spin", "g_spin": "spin",
    "idle": "idle", "g_work_off": "idle",
    "blocked": "blocked", "g_blocked": "blocked",
}
_CENSUS_LABELS = frozenset({"working", "idle", "answer needed"})


def _compact_status_parts(parts):
    """Keep each existing colored state glyph and its observed count; shed only its repeated word."""
    compacted, count_indexes, extra_indexes = [], [], []
    found_status = False
    for text, key in parts:
        text = str(text)
        token = text.strip(" ")
        if not token or token.lower() in _CENSUS_LABELS:
            continue
        if token == "hearting" and key == "hearting":
            # The canonical validated identity is appended below; do not trust a duplicate census label.
            continue
        status = _CENSUS_STATUS_KEYS.get(key)
        marker = token[:1]
        if ((status == "spin" and marker in _SPINNER_GLYPHS) or
                (status in ("idle", "blocked") and marker == "●")):
            found_status = True
            if compacted:
                compacted.append((" ", None))
            compacted.append((marker, key))
            value = token[1:].strip(" ")
            if value:
                count_indexes.append(len(compacted))
                compacted.append((" " + value, key))
            continue
        if compacted:
            compacted.append((" ", None))
        extra_indexes.append(len(compacted))
        compacted.append((token, key))
    if not found_status:
        compacted = _compact_census_parts(parts)
        count_indexes, extra_indexes = [], list(range(len(compacted)))
    return compacted, count_indexes, extra_indexes


def _clip_part(parts, index, width):
    if index is None or not 0 <= index < len(parts):
        return
    text, key = parts[index]
    parts[index] = (_clip(text, max(0, width)), key)


def _join_compact_groups(groups):
    parts, starts = [], []
    for group in groups:
        if not group:
            starts.append(None)
            continue
        if parts and not str(group[0][0]).startswith(" "):
            parts.append((" ", None))
        starts.append(len(parts))
        parts.extend(group)
    return parts, starts


def _fit_compact_host(hosts, width):
    """Bound only the header token; the original host record remains in the VM/Compute data."""
    parts = _host_parts(hosts, compact=True)
    if _parts_width(parts) <= width:
        return parts
    if not parts or width <= 0:
        return []
    if len(parts) < 2:
        return [(_clip("".join(text for text, _key in parts), width), parts[0][1])]
    prefix, name = parts[0], parts[1]
    suffix = parts[2] if len(parts) > 2 else None
    prefix_width = cell_width(prefix[0])
    suffix_width = cell_width(suffix[0]) if suffix else 0
    name_width = max(0, width - prefix_width - suffix_width)
    fitted_name = (_clip(name[0], name_width), name[1]) if name_width else ("", name[1])
    fitted = [prefix, fitted_name]
    if suffix:
        fitted.append(suffix)
    while _parts_width(fitted) > width and suffix:
        suffix_width = max(0, width - prefix_width - cell_width(fitted_name[0]))
        fitted[2] = (_clip(suffix[0], suffix_width), suffix[1])
        suffix = None
    if _parts_width(fitted) > width:
        fitted = [( _clip("".join(text for text, _key in fitted), width), prefix[1])]
    return fitted


def draw_chrome(grid: CellGrid, vm, state, beat=1):
    width, height = grid.cols, grid.rows
    if width <= 0:
        return
    hosts = tuple(vm.hosts or ())
    census_parts = []
    census = vm.census or {}
    if isinstance(census, Mapping) and census:
        census_parts = list(census.get("parts", ()))
    if not census_parts:
        sessions = tuple(vm.sessions or ())
        working = sum(1 for item in sessions if item.get("state") == "working")
        blocked = sum(1 for item in sessions if item.get("state") == "blocked")
        idle = sum(1 for item in sessions if item.get("state") == "idle")
        census_parts = [(f"⠏ {working}", "spin"), (" working", "dim"),
                        (f"  ● {idle}", "idle"), (" idle", "dim"),
                        (f"  ● {blocked}", "blocked"), (" answer needed", "dim"),
                        ("   ", "branch_s")]
    system = vm.system or {}
    fallback_install = system.get("install", {}) if isinstance(system, Mapping) else {}
    if not isinstance(fallback_install, Mapping):
        fallback_install = {}
    install = vm.hearting if isinstance(getattr(vm, "hearting", None), Mapping) else fallback_install
    version = str(install.get("version") or fallback_install.get("version") or "unknown")
    method = str(install.get("install_method") or "unmanaged")
    freshness = (system.get("usage_api", {}).get("freshness")
                 if isinstance(system, Mapping) and isinstance(system.get("usage_api"), Mapping) else None)
    if not freshness:
        freshness = _clock_freshness(getattr(vm, "clock", None))
    identity_parts = [("hearting", "hearting"), (" " + version, "ver"),
                      (" · ", "dim"), (method, "version_method")]
    freshness_parts = [("  " + str(freshness), "dim")] if freshness else []
    full_census_parts = list(census_parts) + identity_parts + freshness_parts
    full_width = _parts_width(full_census_parts)
    full_hosts = _host_parts(hosts)
    right_edge = width - 1 if width > 1 else width
    host_start = 1 if hosts else 0
    host_end = host_start + _parts_width(full_hosts)
    full_start = max(host_end + 1, right_edge - full_width) if hosts else max(0, right_edge - full_width)
    full_fits = (full_width <= right_edge and full_start + full_width <= right_edge and
                 (not hosts or host_end + 1 <= full_start))
    if full_fits:
        x = host_start
        for text, key in full_hosts:
            x = grid.put(x, 0, text, key, limit=full_start - 1 if hosts else right_edge)
        x = full_start
        for text, key in full_census_parts:
            x = grid.put(x, 0, text, key, limit=right_edge)
    else:
        compact_status, count_indexes, extra_indexes = _compact_status_parts(census_parts)
        identity = list(identity_parts)
        right_parts, starts = _join_compact_groups((compact_status, identity, freshness_parts))
        status_start, identity_start, freshness_start = starts
        version_index = identity_start + 1 if identity_start is not None else None
        method_index = identity_start + 3 if identity_start is not None else None
        count_indexes = [status_start + index for index in count_indexes] if status_start is not None else []
        extra_indexes = [status_start + index for index in extra_indexes] if status_start is not None else []

        compact_host = _host_parts(hosts, compact=True)
        host_width = _parts_width(compact_host)
        right_width = _parts_width(right_parts)
        if compact_host and right_width <= width and host_width + right_width + 1 > width:
            compact_host = _fit_compact_host(hosts, max(0, width - right_width - 1))
            host_width = _parts_width(compact_host)
        if compact_host and _parts_width(right_parts) > width and host_width > max(20, width // 4):
            compact_host = _fit_compact_host(hosts, max(6, width // 4))
            host_width = _parts_width(compact_host)
        group_budget = max(0, width - host_width - (1 if compact_host else 0))

        overflow = _parts_width(right_parts) - group_budget
        if overflow > 0:
            current = cell_width(right_parts[version_index][0]) if version_index is not None else 0
            _clip_part(right_parts, version_index, max(0, current - overflow))
        for index in count_indexes:
            overflow = _parts_width(right_parts) - group_budget
            if overflow <= 0:
                break
            current = cell_width(right_parts[index][0])
            _clip_part(right_parts, index, max(0, current - overflow))
        for index in extra_indexes:
            overflow = _parts_width(right_parts) - group_budget
            if overflow <= 0:
                break
            current = cell_width(right_parts[index][0])
            _clip_part(right_parts, index, max(0, current - overflow))
        if _parts_width(right_parts) > group_budget and method_index is not None:
            overflow = _parts_width(right_parts) - group_budget
            current = cell_width(right_parts[method_index][0])
            _clip_part(right_parts, method_index, max(0, current - overflow))
        if _parts_width(right_parts) > group_budget and freshness_start is not None:
            clock_index = freshness_start
            overflow = _parts_width(right_parts) - group_budget
            current = cell_width(right_parts[clock_index][0])
            _clip_part(right_parts, clock_index, max(0, current - overflow))
        if _parts_width(right_parts) > group_budget:
            right_parts = [(_clip("".join(text for text, _key in right_parts), group_budget), "dim")]

        right_width = _parts_width(right_parts)
        host_budget = max(0, width - right_width - (1 if compact_host else 0))
        if _parts_width(compact_host) > host_budget:
            compact_host = _fit_compact_host(hosts, host_budget)
        group_start = width - right_width
        x = 0
        for text, key in compact_host:
            x = grid.put(x, 0, text, key, limit=max(0, group_start - 1))
        x = group_start
        for text, key in right_parts:
            x = grid.put(x, 0, text, key, limit=width)
    usage = tuple(vm.usage or ())
    if not state.usage_open:
        more_x = width - 9
        grid.put(more_x, 1, "u", "hearting", limit=width - 1)
        grid.put(more_x + 1, 1, " more ▸", "dim", limit=width - 1)
        x = 1
        grid.put(x, 1, "usage(week)", "label")
        x = 14
        for item in usage:
            text = item.get("text", "")
            key = item.get("key", "dim")
            week = next((window for window in item.get("windows", ())
                         if window and window[0] == "week"), None)
            if week is None:
                continue
            label, percent, reset = week
            reset = "·" if reset is None else reset
            extras = [(window[0], int(window[1])) for window in item.get("windows", ())
                      if window and window[0] != "week" and int(window[1]) >= 80]
            extra_width = sum(3 + len(name) + len(str(value)) + 2 for name, value in extras)
            fragment_width = len(str(text)) + 1 + 10 + 1 + 4 + 3 + len(str(reset)) + extra_width
            if x + fragment_width >= width - 12:
                continue
            x = grid.put(x, 1, str(text) + " ", key, limit=width - 12)
            percent = int(percent)
            filled = max(1, min(10, round(10 * percent / 100))) if percent else 0
            gauge_key = "lvl_r_flat" if percent >= 80 else "lvl_y" if percent >= 50 else "lvl_g"
            x = grid.put(x, 1, "━" * filled, gauge_key, limit=width - 12)
            x = grid.put(x, 1, "─" * (10 - filled), "dim", limit=width - 12)
            x = grid.put(x, 1, f" {percent:3d}%", gauge_key, limit=width - 12)
            x = grid.put(x, 1, " ↻ " + str(reset), "dim", limit=width - 12)
            if extras:
                x = grid.put(x, 1, "  ", "dim", limit=width - 12)
                x = grid.put(x, 1, "·", "dim", limit=width - 12)
                x = grid.put(x, 1, " ", "dim", limit=width - 12)
                for name, value in extras:
                    x = grid.put(x, 1, f"{name} {value}%", "blocked", limit=width - 12)
            else:
                x += 4
    else:
        for index, item in enumerate(usage[:3]):
            y = 1 + index
            usage_limit = width - 9 if y == 1 else width - 1
            grid.put(1, y, str(item.get("text", "")), item.get("key", "dim"), limit=usage_limit)
            windows = list(item.get("windows", ()))

            def window_end(slot, window):
                return 14 + slot * 40 + 5 + 1 + 1 + 12 + 5 + 1 + (
                    2 + len(str(window[2])) + 1 if window[2] else 0)
            for win_index, window in enumerate(windows):
                if not window:
                    continue
                name, percent, reset = window
                x = 14 + win_index * 40
                x = grid.put(x, y, str(name).rjust(5), "label", limit=usage_limit)
                x = grid.put(x + 1, y, "[", "dim", limit=usage_limit)
                filled = max(1, min(12, round(12 * int(percent) / 100))) if int(percent) else 0
                gauge_key = "lvl_r_flat" if int(percent) >= 80 else "lvl_y" if int(percent) >= 50 else "lvl_g"
                x = grid.put(x, y, "━" * filled, gauge_key, limit=usage_limit)
                x = grid.put(x, y, "─" * (12 - filled), "dim", limit=usage_limit)
                x = grid.put(x, y, f"{int(percent):4d}%", gauge_key, limit=usage_limit)
                x = grid.put(x, y, "]", "dim", limit=usage_limit)
                if reset:
                    grid.put(x + 1, y, "↻ " + str(reset), "dim", limit=usage_limit)
        grid.put(width - 9, 1, "u", "hearting", limit=width - 1)
        grid.put(width - 8, 1, " less ▾", "dim", limit=width - 1)
    if height >= 3:
        title_y = 5 if state.usage_open else 3
        grid.fill(0, NAV_WIDTH, title_y, " ", "dim", tint=234)
        grid.put(0, title_y, " ", "dim", tint=234)
        grid.put(1, title_y, "VIEWS    ", "title_dim", tint=234)
        grid.put(NAV_WIDTH - 1, title_y, " ", None, tint=234)
        grid.put(NAV_WIDTH, title_y, "┌", "line")
        grid.put(NAV_WIDTH + 1, title_y, "─", "line")
        grid.put(NAV_WIDTH + 2, title_y, " { ", "dim")
        title_text = str(state.title or state.view)
        grid.put(NAV_WIDTH + 5, title_y, title_text, "head", limit=width - 1)
        title_end = NAV_WIDTH + 5 + cell_width(title_text)
        grid.put(title_end, title_y, " ", "dim", limit=width - 1)
        grid.put(title_end + 1, title_y, "} ", "dim", limit=width - 1)
        grid.put(title_end + 3, title_y, "─" * max(0, width - title_end - 3), "line", limit=width)
        for row in range(title_y + 1, max(title_y + 1, height - 4)):
            grid.put(NAV_WIDTH, row, "│", "line")
        if height > 9:
            grid.put(NAV_WIDTH, 12 if state.usage_open else 10, "│", "line")
        if height > 34:
            grid.put(NAV_WIDTH, 34, "│", "line")
        grid.fill(0, width, height - 4, "─", "line")
        grid.put(NAV_WIDTH, height - 4, "┴", "line")
    if height >= 1:
        sessions = tuple(vm.sessions or ())
        routes = tuple(vm.routes or ())
        questions = tuple(vm.questions or ())
        scope_kind, _, scope_id = str(state.scope or "-").partition(":")
        if state.status:
            status = state.status
        elif scope_kind == "repo":
            mine = tuple(item for item in sessions
                         if item.get("repo_id", item.get("repo")) == scope_id)
            mine_routes = tuple(item for item in routes if str(item.get("repo") or "") == scope_id)
            status = (f"{count_word(len(mine), 'session')} · "
                      f"{sum(1 for item in mine_routes if item.get('state') != 'done')} open · "
                      f"{sum(1 for item in mine_routes if item.get('state') == 'done')} done")
        elif scope_kind in ("session", "route"):
            status = "esc: back up"
        elif state.view == "OVERVIEW" and state.scope == "-":
            status = (f"{count_word(len(sessions), 'session')} · "
                      f"{count_word(sum(1 for item in routes if item.get('state') != 'done'), 'route')} · "
                      f"{count_word(len(questions), 'question')}")
        elif state.view == "ROUTES":
            status = (f"{sum(1 for item in routes if item.get('state') != 'done')} open · "
                      f"{count_word(sum(1 for item in routes if item.get('state') == 'dead'), 'stopped leg')}")
        elif state.view == "QUESTIONS":
            total = (system.get("answered_total") if isinstance(system, Mapping) else None)
            records = total if total is not None else len(questions)
            status = f"{len(questions)} waiting · {count_word(records, 'decision record')} today"
        elif state.view == "HISTORY":
            seats = tuple(vm.seats or ())
            status = (f"{count_word(len(seats), 'seat')} · "
                      f"{count_word(sum(int(seat[2]) for seat in seats if len(seat) > 2), 'generation')}")
        elif state.view == "PEERS":
            edges = tuple(vm.peer_edges.get("links", ())) if isinstance(vm.peer_edges, Mapping) else ()
            status = (f"{count_word(len(edges), 'link')} · "
                      f"{count_word(sum(1 for item in sessions if item.get('steward')), 'steward')} · "
                      f"{count_word(sum(1 for edge in edges if len(edge) > 2 and edge[2] == 'handover'), 'handover')}")
        elif state.view == "COMPUTE":
            hosts = tuple(vm.compute or ())
            busy = sum(1 for host in hosts for gpu in (host[5] if len(host) > 5 else ())
                       if len(gpu) > 1 and gpu[1] == "working")
            total = sum(len(host[5]) for host in hosts if len(host) > 5)
            status = f"{count_word(len(hosts), 'host')} · GPU {busy}/{total}"
        elif state.view == "MEMORY":
            summary = system.get("memory_summary", {}) if isinstance(system, Mapping) else {}
            if isinstance(summary, Mapping):
                status = f"today {summary.get('added_today', '·')} added · {summary.get('expired', '·')} expired"
        elif state.view == "MODELS":
            models = vm.models if isinstance(vm.models, Mapping) else {}
            profile_count = len(models.get("profiles", ()))
            profile_map = models.get("profile_map", {})
            harness_count = len(profile_map) if isinstance(profile_map, Mapping) else 0
            fields = tuple((models.get("allocation") or {}).get("fields", ()))
            strategy = next((str(parts[0][0]) for label, parts in fields
                             if label == "strategy:" and parts), "balanced")
            status = (f"{count_word(profile_count, 'profile')} × "
                      f"{count_word(harness_count, 'harness', 'harnesses')} · {strategy} allocation")
        elif state.view == "SYSTEM":
            install = system.get("install", {}) if isinstance(system, Mapping) else {}
            delivery = system.get("peer_delivery", {}) if isinstance(system, Mapping) else {}
            pending = delivery.get("pending", "·")
            waiting = (count_word(pending, "delivery", "deliveries") if isinstance(pending, int)
                       else f"{pending} deliveries")
            status = f"{install.get('version', '·')} · {waiting} pending"
        else:
            status = ""
        grid.fill(0, width, height - 1, " ")
        cursor = grid.put(1, height - 1, "hearting ›", "dim", limit=width - 1)
        cursor = grid.put(cursor, height - 1, " " + str(state.title or state.view),
                          "title_dim", limit=width - 1)
        grid.put(cursor, height - 1, "  " + str(status), "dim", limit=width - 1)
    if height >= 3:
        grid.fill(0, width, height - 3, " ")
        events = tuple(vm.events or ())
        if events:
            entry = events[0]
            if isinstance(entry, Mapping):
                time, tag, source, kind, key, text = (entry.get(k, "") for k in
                                                       ("time", "tag", "source", "kind", "key", "text"))
            else:
                time, tag, source, kind, key, text = (tuple(entry) + ("",) * 6)[:6]
            cursor = grid.put(1, height - 3, f"≋ {time} [", "dim", limit=width - 1)
            cursor = grid.put(cursor, height - 3, str(tag), "soft", limit=width - 1)
            cursor = grid.put(cursor, height - 3, f"] {source}  {kind}", "dim", limit=width - 1)
            cursor = grid.put(cursor, height - 3, " · ", "soft", limit=width - 1)
            grid.put(cursor, height - 3, str(text), "soft", limit=width - 1)
    if height >= 2:
        grid.fill(0, width, height - 2, " ")
    x = 1
    legend = [("↑↓", "move"), ("←→", "focus"), ("⏎", "go to pane"), ("Tab", "next view"),
              ("/", "filter"), ("u", "usage"), ("F", "feed"), ("q", "quit")]
    for key, label in legend:
        if x > 1:
            x = grid.put(x, height - 2, " · ", "line")
        x = grid.put(x, height - 2, key, "hearting")
        x = grid.put(x + 1, height - 2, label, "dim")
    return grid


def _tree_row_count(vm, sessions):
    """Rows the session tree needs when nothing is cut: repos, their sessions, open routes, workers."""
    total = 0
    for repo in vm.repos:
        repo_id = repo.get("id", repo.get("name", "·"))
        if not any(session.get("repo_id", session.get("repo")) == repo_id for session in sessions):
            continue                                    # a repo with no session has no row in the tree
        total += 1 if repo_label(repo.get("name", "·")) else 0     # the nameless group has no repo row
        for session in sessions:
            if session.get("repo_id", session.get("repo")) != repo_id:
                continue
            total += 1
            tag = session.get("tag")
            for route in vm.routes:
                if route.get("session_tag") == tag and route.get("state") not in ("done", "dead"):
                    total += 2 if route.get("subworker") else 1
    return total


def draw_explorer(grid: CellGrid, vm, state):
    width, height = min(NAV_WIDTH, grid.cols), grid.rows - 3
    if width <= 0 or height <= 0:
        return
    scope_kind, _, scope_id = str(getattr(state, "scope", "-") or "-").partition(":")
    counts = {"PEERS": len(vm.peer_edges.get("links", ())),
              "ROUTES": sum(1 for route in vm.routes if route.get("state") != "done"),
              "QUESTIONS": len(vm.questions or ()), "HISTORY": len(vm.seats or ())}
    views = ("OVERVIEW", "PEERS", "ROUTES", "QUESTIONS", "HISTORY", "COMPUTE")
    y = 6 if getattr(state, "usage_open", False) else 4
    for view in views:
        page_scope = getattr(state, "scope", "-") != "-"
        selected = not page_scope and getattr(state, "view", "OVERVIEW") == view
        grid.add_hit(y, 0, width, ("tab", view))
        if selected:
            grid.fill(0, width, y, " ", tint=236)
        key = "head" if selected else "loc_repo" if page_scope and view == "OVERVIEW" else "soft"
        grid.put(1, y, view, key, tint=236 if selected else None,
                 limit=width - 1)
        count = counts.get(view, len(vm.compute or ()) if view == "COMPUTE" else 0)
        if view != "OVERVIEW" and count:
            count_key = "blocked" if view == "QUESTIONS" else "dim"
            shown_count = f"{count}/{count}" if view == "COMPUTE" else str(count)
            grid.put(width - len(shown_count) - 1, y, shown_count, count_key,
                     limit=width - 1)
        if selected:
            for xx in range(width):
                glyph, key, _tint = grid.cells[y][xx]
                grid.cells[y][xx] = (glyph, key, 236)
        y += 1
    x = 1
    page_open = getattr(state, "scope", "-") != "-"
    for view in ("MEMORY", "MODELS", "SYSTEM"):
        left, right = x - 1, x + len(view) + 1
        grid.add_hit(y, left, right, ("tab", view))
        selected = not page_open and state.view == view
        if selected:
            grid.fill(left, right, y, " ", tint=236)
        grid.put(x, y, view, "head" if selected else "soft", tint=236 if selected else None,
                 limit=width - 1)
        if selected:
            for xx in range(left, right):
                glyph, key, _tint = grid.cells[y][xx]
                grid.cells[y][xx] = (glyph, key, 236)
        x += len(view) + 3
    y += 1
    # r14 separates the utility tabs from the SESSIONS band with a blank row.
    y += 1
    sessions = tuple(vm.sessions or ())
    working = sum(1 for item in sessions if item.get("state") == "working")
    idle = sum(1 for item in sessions if item.get("state") == "idle")
    blocked = sum(1 for item in sessions if item.get("state") == "blocked")
    grid.fill(0, width, y, " ", "dim", tint=234)
    grid.put(1, y, "SESSIONS ", "title_dim", tint=234)
    count_x = max(1, width - 12)
    grid.put(count_x, y, " ", "branch_s", tint=234)
    grid.put(count_x + 1, y, "  ", None, tint=234)
    grid.put(count_x + 3, y, f"⠏{working}", "g_spin", tint=234)
    grid.put(count_x + 5, y, " ", "g_work_off", tint=234)
    grid.put(count_x + 6, y, f"●{idle}", "g_work_off", tint=234)
    grid.put(count_x + 8, y, " ", "g_blocked", tint=234)
    grid.put(count_x + 9, y, f"●{blocked}", "g_blocked", tint=234)
    grid.put(width - 1, y, " ", None, tint=234)
    y += 1
    tree_bottom = min(height - 1, 34)
    tree_total = _tree_row_count(vm, sessions)
    cut, tree_top = tree_bottom, y
    if y + tree_total > tree_bottom:
        # A tree taller than its pane stops above COMPUTE and says so on its last row; the rows
        # it hides are neither drawn nor clickable.
        cut = tree_bottom - 1
        hidden_rows = y + tree_total - cut
    route_scope = (next((item for item in vm.routes if str(item.get("id")) == scope_id), None)
                   if scope_kind == "route" else None)
    listed = {str(route.get("id")) for session in sessions for route in vm.routes
              if route.get("session_tag") == session.get("tag") and route.get("state") not in ("done", "dead")}
    # An ended route is not in the tree: light the session that ran it, or its repo.
    hidden = route_scope is not None and str(route_scope.get("id")) not in listed
    lit_tag = str((route_scope or {}).get("session_tag") or "")
    tree_repos = [repo for repo in vm.repos
                  if any(session.get("repo_id", session.get("repo")) == repo.get("id", repo.get("name", "·"))
                         for session in sessions)]
    for repo_index, repo in enumerate(tree_repos):
        name = repo_label(repo.get("name", "·"))
        repo_id = repo.get("id", repo.get("name", "·"))
        children = [session for session in sessions
                    if session.get("repo_id", session.get("repo")) == repo_id]
        last_repo = repo_index == len(tree_repos) - 1
        if y >= cut:
            break
        # The nameless group draws no repo row: its sessions sit at the repo level (three cells left).
        flat = not name
        shift = 3 if flat else 0
        if not flat:
            grid.add_hit(y, 0, width, ("scope", "repo:" + str(repo_id)))
            repo_lit = ((scope_kind == "repo" and str(repo_id) == scope_id) or
                        (hidden and not lit_tag and str(repo_id) == str((route_scope or {}).get("repo"))))
            if repo_lit:
                grid.fill(0, width, y, " ", tint=236)
            grid.put(1, y, "┗━ " if last_repo else "┣━ ", "line")
            grid.put(4, y, _clip(name + "/", width - 6), "soft", limit=width - 1)
            if repo_lit:
                for col in range(width):
                    glyph, key, _tint = grid.cells[y][col]
                    grid.cells[y][col] = (glyph, key, 236)
            drag = tuple(getattr(state, "drag", ()) or ())
            if len(drag) >= 9 and drag[0] == "tree" and drag[1] == "repo" and drag[8]:
                if drag[2] == str(repo_id):
                    for col in range(width):
                        glyph, key, _tint = grid.cells[y][col]
                        grid.cells[y][col] = (glyph, key, 236)
                if drag[7] and drag[6] == str(repo_id):
                    grid.put(1, y, "↳", "accent", limit=width - 1)
            y += 1
        for session_index, session in enumerate(children):
            if y >= cut:
                break
            tag = str(session.get("tag") or "·")
            last_session = session_index == len(children) - 1
            stem = "   " if last_repo else "┃  "
            grid.add_hit(y, 0, width, ("scope", tag))
            session_lit = (scope_kind == "session" and tag == scope_id) or (hidden and tag == lit_tag)
            if session_lit:
                grid.fill(0, width, y, " ", tint=236)
            if flat:
                last_session = last_session and last_repo
                stem = "   " if last_session else "┃  "
                grid.put(1, y, "┗━ " if last_session else "┣━ ", "line")
            else:
                grid.put(1, y, stem, "line")
                grid.put(4, y, "┗━ " if last_session else "┣━ ", "line")
            grid.put(7 - shift, y, "[", "dim")
            grid.put(8 - shift, y, tag, "steward" if session.get("steward") else "soft")
            grid.put(8 - shift + len(tag), y, "]", "dim")
            harness = str(session.get("harness") or "·")
            hx = grid.put(12 - shift, y, harness, "hb_" + harness)
            status = session.get("state")
            marker, marker_key = {"working": ("⠏", "g_spin_dim"), "blocked": ("●", "lvl_r_dim"),
                                  "dead": ("✕", "dead"), "done": ("✓", "done")}.get(
                                      status, ("●", "idle"))
            grid.put(hx + 1, y, marker, marker_key)
            if session_lit:
                for col in range(width):
                    glyph, key, _tint = grid.cells[y][col]
                    grid.cells[y][col] = (glyph, key, 236)
            drag = tuple(getattr(state, "drag", ()) or ())
            if len(drag) >= 9 and drag[0] == "tree" and drag[1] == "session" and drag[8]:
                if drag[2] == tag:
                    for col in range(width):
                        glyph, key, _tint = grid.cells[y][col]
                        grid.cells[y][col] = (glyph, key, 236)
                if drag[7] and drag[6] == tag:
                    grid.put(1, y, "↳", "accent", limit=width - 1)
            y += 1
            session_routes = sorted(
                (route for route in vm.routes if route.get("session_tag") == tag
                 and route.get("state") not in ("done", "dead")),
                key=lambda route: route.get("state") != "working")
            for route_index, route in enumerate(session_routes):
                if y >= cut:
                    break
                grid.add_hit(y, 0, width, ("route", route.get("id"), tag))
                if scope_kind == "route" and str(route.get("id")) == scope_id:
                    grid.fill(0, width, y, " ", tint=236)
                grid.put(1, y, stem, "line")
                if flat:
                    branch_x = 9 - shift
                elif last_session:
                    grid.put(4, y, "   ", "line")
                    grid.put(7, y, "  ", None)
                    branch_x = 9
                else:
                    grid.put(4, y, "┃  ", "line")
                    grid.put(7, y, "  ", None)
                    branch_x = 9
                grid.put(branch_x, y, "┗━ " if route_index == len(session_routes) - 1 else "┣━ ", "line")
                label = route.get("tree_label") or route.get("title") or "·"
                rh = route.get("harness") or "·"
                label_x = branch_x + 3
                mark, mark_key = {"running": ("⠏", "g_spin_dim"), "working": ("⠏", "g_spin_dim"),
                                  "blocked": ("●", "lvl_r_dim")}.get(
                    route.get("activity", route.get("state")), ("●", "idle"))
                route_suffix = ": " + str(rh) + " " + mark
                name_room = max(1, width - 1 - label_x - cell_width(route_suffix))
                route_name = clip_cells(str(label), name_room)
                route_label = route_name + ": "
                grid.put(label_x, y, route_name, "dim", limit=width - 1)
                grid.put(label_x + cell_width(route_name), y, ": ", "dim", limit=width - 1)
                harness_x = label_x + cell_width(route_label)
                grid.put(harness_x, y, str(rh), "h_" + str(rh), limit=width - 1)
                grid.put(harness_x + cell_width(str(rh)) + 1, y, mark, mark_key,
                         limit=width - 1)
                if scope_kind == "route" and str(route.get("id")) == scope_id:
                    for col in range(width):
                        glyph, key, _tint = grid.cells[y][col]
                        grid.cells[y][col] = (glyph, key, 236)
                y += 1
                subworker = route.get("subworker")
                if subworker and y < cut:
                    name, who, activity = subworker
                    grid.add_hit(y, 0, width, ("route", route.get("id"), tag))
                    grid.put(1, y, stem, "line")
                    if not flat:
                        grid.put(4, y, "   " if last_session else "┃  ", "line")
                    grid.put(9 - shift, y, "   ", "line")
                    grid.put(12 - shift, y, "┗━ ", "line")
                    sub_label = str(name) + ": "
                    grid.put(15 - shift, y, sub_label, "dim", limit=width - 1)
                    grid.put(15 - shift + len(sub_label), y, str(who), "h_" + str(who), limit=width - 1)
                    sub_mark = "g_spin_dim" if activity == "running" else "g_work_off"
                    grid.put(15 - shift + len(sub_label) + len(str(who)) + 1, y, "⠏" if activity == "running" else "●", sub_mark)
                    y += 1
    drag = tuple(getattr(state, "drag", ()) or ())
    if y < cut and len(drag) >= 9 and drag[0] == "tree" and drag[8] and drag[7] and drag[6] == "end":
        grid.put(1, y, "↳", "accent", limit=width - 1)
    if cut < tree_bottom and cut >= tree_top:
        grid.put(4, cut, f"… {hidden_rows} more rows", "dim", limit=width - 1)

    # COMPUTE is a fixed explorer section. Percentages are source facts; draw the
    # compact host summary from the immutable fixture without probing resources.
    compute_y = 34
    if compute_y < height:
        hosts = tuple(vm.compute or ())
        grid.add_hit(compute_y, 0, width, ("root", "COMPUTE"))
        for hit_y in range(compute_y + 1, grid.rows - 4):
            grid.add_hit(hit_y, 0, width, ("root", "COMPUTE"))
        grid.fill(0, width, compute_y, " ", "dim", tint=234)
        grid.put(1, compute_y, "COMPUTE ",
                 "head" if state.view == "COMPUTE" and getattr(state, "scope", "-") == "-" else "title_dim",
                 tint=234)
        compute_count = f"{len(hosts)}/{len(hosts)}"
        grid.put(width - 1 - len(compute_count), compute_y, compute_count, "dim", tint=234,
                 limit=width - 1)
        grid.put(width - 5, compute_y, " ", None, tint=234)
        grid.put(width - 1, compute_y, " ", None, tint=234)
        grid.put(14, compute_y + 1, "cpu", "dim")
        grid.put(20, compute_y + 1, "gpu0", "dim")
        grid.put(27, compute_y + 1, "gpu1", "dim")
        host_labels = tuple(vm.hosts or ())
        for offset, host in enumerate(hosts[:5]):
            row_y = compute_y + 2 + offset
            if row_y >= grid.rows - 4:
                break
            _name, base, cpu, _memory, _disk, gpus = (tuple(host) + (None,) * 6)[:6]
            gpus = tuple(gpus or ())
            host_record = host_labels[offset] if offset < len(host_labels) else {}
            name = host_record.get("name", _name) if isinstance(host_record, Mapping) else _name
            cpu_pct = None
            if cpu and len(cpu) > 1 and cpu[0] is not None and cpu[1]:
                cpu_pct = round(100 * float(cpu[0]) / max(1, float(cpu[1])))
            cpu_key = ("dim" if cpu_pct is None else "g_blocked" if cpu_pct >= 80
                       else "g_idle" if cpu_pct >= 50 else "grp_live")
            busy = any(gpu[1] == "working" for gpu in gpus)
            grid.put(1, row_y, str(name), "work" if base else "soft", limit=12)
            grid.put(13, row_y, "  ·" if cpu_pct is None else f" {cpu_pct:2d}%", cpu_key)
            grid.put(18, row_y, "●", "spin" if busy else "idle")
            for gpu_index, gpu in enumerate(gpus[:2]):
                _, gpu_state, _model, _gpu_key, _util, used, total, _sessions = (tuple(gpu) + (None,) * 8)[:8]
                gx = 20 + gpu_index * 7
                if used is None or not total:
                    grid.put(gx, row_y, "  ·", "dim")
                else:
                    pct = round(100 * float(used) / max(1, float(total)))
                    key = ("dim" if pct == 0 else "lvl_r" if pct >= 80
                           else "g_idle" if pct >= 50 else "grp_live")
                    grid.put(gx, row_y, f" {pct:2d}%", key)
                if gpu_index == 0:
                    grid.put(gx + 5, row_y, "●", "spin" if gpu_state == "working" else "idle")
