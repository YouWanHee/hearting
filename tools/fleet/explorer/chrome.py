"""Header, navigation rail, feed, key legend and status rows."""
from collections.abc import Mapping
from .grid import CellGrid

NAV_WIDTH = 32


def _clip(text, width):
    text = str(text)
    width = max(0, int(width))
    if len(text) <= width:
        return text
    return "…" if width == 1 else text[:width - 1] + "…"


def draw_chrome(grid: CellGrid, vm, state, beat=1):
    width, height = grid.cols, grid.rows
    hosts = tuple(vm.hosts or ())
    x = 1
    x = grid.put(x, 0, "hosts ", "label")
    census_x = width - 1
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
    install = system.get("install", {}) if isinstance(system, Mapping) else {}
    freshness = (system.get("usage_api", {}).get("freshness")
                 if isinstance(system, Mapping) and isinstance(system.get("usage_api"), Mapping) else None)
    if install:
        census_parts.extend([("hearting", "hearting"),
                             (" " + str(install.get("version", "")), "ver")])
    if freshness:
        census_parts.append(("  " + str(freshness), "dim"))
    census_width = sum(len(part[0]) for part in census_parts)
    census_x = max(1, width - 1 - census_width)
    for index, host in enumerate(hosts):
        if isinstance(host, Mapping):
            mark, name = host.get("mark", "◇"), host.get("name", "·")
            key = host.get("key", "base_host")
        else:
            mark, name, key = (tuple(host) + ("base_host",))[:3]
        mark_key = "dim" if mark == "◇" else "work"
        x = grid.put(x, 0, f"{mark} ", mark_key, limit=max(x, census_x - 2))
        x = grid.put(x, 0, name, mark_key if mark != "◇" else "soft",
                     limit=max(x, census_x - 2))
        separator = " " if index == len(hosts) - 1 else "  "
        x = grid.put(x, 0, separator, "dim" if len(separator) == 1 else "branch_s",
                     limit=max(x, census_x - 2))
    if x + 4 < census_x:
        x = grid.put(x, 0, f"{len(hosts)}/{len(hosts)}", "dim", limit=census_x - 1)
    cx = max(x + 1, width - 1 - census_width)
    for text, key in census_parts:
        grid.put(cx, 0, text, key)
        cx += len(text)
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
            extras = [(window[0], int(window[1])) for window in item.get("windows", ())
                      if window and window[0] != "week" and int(window[1]) >= 80]
            extra_width = sum(3 + len(name) + len(str(value)) + 2 for name, value in extras)
            fragment_width = len(str(text)) + 1 + 10 + 1 + 4 + 3 + len(str(reset)) + extra_width
            if x + fragment_width >= width - 12:
                continue
            x = grid.put(x, 1, str(text) + " ", key, limit=width - 12)
            filled = max(1, min(10, round(10 * int(percent) / 100))) if int(percent) else 0
            x = grid.put(x, 1, "━" * filled, "done", limit=width - 12)
            x = grid.put(x, 1, "─" * (10 - filled), "dim", limit=width - 12)
            x = grid.put(x, 1, f" {int(percent):3d}%", "done", limit=width - 12)
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
            grid.put(1, y, str(item.get("text", "")), item.get("key", "dim"))
            for win_index, window in enumerate(item.get("windows", ())):
                if not window:
                    continue
                name, percent, reset = window
                x = 14 + win_index * 40
                x = grid.put(x, y, str(name).rjust(5), "label")
                x = grid.put(x + 1, y, "[", "dim")
                filled = max(1, min(12, round(12 * int(percent) / 100))) if int(percent) else 0
                gauge_key = "g_blocked" if int(percent) >= 80 else "lvl_y" if int(percent) >= 50 else "lvl_g"
                x = grid.put(x, y, "━" * filled, gauge_key)
                x = grid.put(x, y, "─" * (12 - filled), "dim")
                x = grid.put(x, y, f"{int(percent):4d}%", gauge_key)
                x = grid.put(x, y, "]", "dim")
                if reset:
                    grid.put(x + 1, y, "↻ " + str(reset), "dim")
        grid.put(width - 9, 1, "u", "hearting", limit=width - 1)
        grid.put(width - 8, 1, " less ▾", "dim", limit=width - 1)
    if height >= 3:
        title_y = 4 if state.usage_open else 2
        grid.fill(0, width, title_y, "━", "line")
        grid.put(0, title_y, "━", "line")
        grid.put(1, title_y, " EXPLORER ", "title_dim")
        grid.put(NAV_WIDTH, title_y, "┳", "line")
        grid.put(NAV_WIDTH + 1, title_y, "━", "line")
        grid.put(NAV_WIDTH + 2, title_y, " ", "dim")
        grid.put(NAV_WIDTH + 3, title_y, "{ ", "dim")
        grid.put(NAV_WIDTH + 5, title_y, str(state.title or state.view), "head",
                 limit=width - 1)
        title_end = NAV_WIDTH + 5 + len(str(state.title or state.view))
        grid.put(title_end, title_y, " ", "dim", limit=width - 1)
        grid.put(title_end + 1, title_y, "} ", "dim", limit=width - 1)
        grid.put(title_end + 3, title_y, "━" * max(0, width - title_end - 4), "line", limit=width - 1)
        for row in range(title_y + 1, max(title_y + 1, height - 4)):
            grid.put(NAV_WIDTH, row, "┃", "line")
        if height > 9:
            grid.put(NAV_WIDTH, 11 if state.usage_open else 9, "┫", "line")
        if height > 34:
            grid.put(NAV_WIDTH, 34, "┫", "line")
        grid.fill(0, width, height - 4, "━", "line")
        grid.put(NAV_WIDTH, height - 4, "┻", "line")
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
            status = (f"{len(mine)} sessions · "
                      f"{sum(1 for item in mine_routes if item.get('state') != 'done')} open · "
                      f"{sum(1 for item in mine_routes if item.get('state') == 'done')} done")
        elif scope_kind in ("session", "route"):
            status = "esc: back up"
        elif state.view == "OVERVIEW" and state.scope == "-":
            status = (f"{len(sessions)} sessions · "
                      f"{sum(1 for item in routes if item.get('state') != 'done')} routes · "
                      f"{len(questions)} question")
        elif state.view == "ROUTES":
            status = (f"{sum(1 for item in routes if item.get('state') != 'done')} open · "
                      f"{sum(1 for item in routes if item.get('state') == 'dead')} stopped legs")
        elif state.view == "QUESTIONS":
            total = (system.get("answered_total") if isinstance(system, Mapping) else None)
            status = f"{len(questions)} waiting · {total if total is not None else len(questions)} decision records today"
        elif state.view == "HISTORY":
            seats = tuple(vm.seats or ())
            status = f"{len(seats)} seats · {sum(int(seat[2]) for seat in seats if len(seat) > 2)} generations"
        elif state.view == "PEERS":
            edges = tuple(vm.peer_edges.get("links", ())) if isinstance(vm.peer_edges, Mapping) else ()
            status = f"{len(edges)} links · {sum(1 for item in sessions if item.get('steward'))} stewards · {sum(1 for edge in edges if len(edge) > 2 and edge[2] == 'handover')} handovers"
        elif state.view == "COMPUTE":
            hosts = tuple(vm.compute or ())
            busy = sum(1 for host in hosts for gpu in (host[5] if len(host) > 5 else ())
                       if len(gpu) > 1 and gpu[1] == "working")
            total = sum(len(host[5]) for host in hosts if len(host) > 5)
            status = f"{len(hosts)} hosts · GPU {busy}/{total}"
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
            status = f"{profile_count} profiles × {harness_count} harnesses · {strategy} allocation"
        elif state.view == "SYSTEM":
            install = system.get("install", {}) if isinstance(system, Mapping) else {}
            delivery = system.get("peer_delivery", {}) if isinstance(system, Mapping) else {}
            status = f"{install.get('version', '·')} · {delivery.get('pending', '·')} deliveries pending"
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
    for key, label in (("↑↓", "move"), ("←→", "focus"), ("⏎", "go to pane"),
                       ("Tab", "next view"), ("/", "filter"), ("u", "usage"),
                       ("F", "feed"), ("q", "quit")):
        if x > 1:
            x = grid.put(x, height - 2, " · ", "line")
        x = grid.put(x, height - 2, key, "hearting")
        x = grid.put(x + 1, height - 2, label, "dim")
    return grid


def draw_explorer(grid: CellGrid, vm, state):
    width, height = min(NAV_WIDTH, grid.cols), grid.rows - 3
    if width <= 0 or height <= 0:
        return
    scope_kind, _, scope_id = str(getattr(state, "scope", "-") or "-").partition(":")
    counts = {"PEERS": len(vm.peer_edges.get("links", ())),
              "ROUTES": sum(1 for route in vm.routes if route.get("state") != "done"),
              "QUESTIONS": len(vm.questions or ()), "HISTORY": len(vm.seats or ())}
    views = ("OVERVIEW", "PEERS", "ROUTES", "QUESTIONS", "HISTORY")
    y = 5 if getattr(state, "usage_open", False) else 3
    for view in views:
        page_scope = getattr(state, "scope", "-") != "-"
        selected = not page_scope and getattr(state, "view", "OVERVIEW") == view
        grid.add_hit(y, 0, width, ("tab", view))
        if selected:
            grid.fill(0, width, y, " ", tint=236)
        key = "head" if selected else "loc_repo" if page_scope and view == "OVERVIEW" else "soft"
        grid.put(1, y, view, key, tint=236 if selected else None,
                 limit=width - 1)
        if view != "OVERVIEW" and counts.get(view):
            count_key = "blocked" if view == "QUESTIONS" else "dim"
            grid.put(width - len(str(counts[view])) - 1, y, str(counts[view]), count_key,
                     limit=width - 1)
        if selected:
            for xx in range(width):
                glyph, key, _tint = grid.cells[y][xx]
                grid.cells[y][xx] = (glyph, key, 236)
        y += 1
    x = 1
    for view in ("MEMORY", "MODELS", "SYSTEM"):
        left, right = x - 1, x + len(view) + 1
        grid.add_hit(y, left, right, ("tab", view))
        selected = state.view == view
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
    sessions = tuple(vm.sessions or ())
    working = sum(1 for item in sessions if item.get("state") == "working")
    idle = sum(1 for item in sessions if item.get("state") == "idle")
    blocked = sum(1 for item in sessions if item.get("state") == "blocked")
    grid.put(0, y, "━", "line")
    grid.put(1, y, " SESSIONS ", "title_dim")
    grid.put(11, y, "━━━━━━━━━━", "line")
    grid.put(21, y, " ", "branch_s")
    grid.put(22, y, "⠏" + str(working), "spin")
    grid.put(24, y, " ●" + str(idle), "idle")
    grid.put(27, y, " ●" + str(blocked), "blocked")
    grid.put(30, y, " ", "branch_s")
    grid.put(31, y, "━", "line")
    y += 1
    tree_bottom = min(height - 1, 34)
    route_scope = (next((item for item in vm.routes if str(item.get("id")) == scope_id), None)
                   if scope_kind == "route" else None)
    listed = {str(route.get("id")) for session in sessions for route in vm.routes
              if route.get("session_tag") == session.get("tag") and route.get("state") not in ("done", "dead")}
    # An ended route is not in the tree: light the session that ran it, or its repo.
    hidden = route_scope is not None and str(route_scope.get("id")) not in listed
    lit_tag = str((route_scope or {}).get("session_tag") or "")
    for repo_index, repo in enumerate(vm.repos):
        name = str(repo.get("name", "·"))
        repo_id = repo.get("id", name)
        children = [session for session in sessions
                    if session.get("repo_id", session.get("repo")) == repo_id]
        last_repo = repo_index == len(vm.repos) - 1
        if y >= tree_bottom:
            break
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
        y += 1
        for session_index, session in enumerate(children):
            if y >= tree_bottom:
                break
            tag = str(session.get("tag") or "·")
            last_session = session_index == len(children) - 1
            stem = "   " if last_repo else "┃  "
            grid.add_hit(y, 0, width, ("scope", tag))
            session_lit = (scope_kind == "session" and tag == scope_id) or (hidden and tag == lit_tag)
            if session_lit:
                grid.fill(0, width, y, " ", tint=236)
            grid.put(1, y, stem, "line")
            grid.put(4, y, "┗━ " if last_session else "┣━ ", "line")
            grid.put(7, y, "[", "dim")
            grid.put(8, y, tag, "steward" if session.get("steward") else "soft")
            grid.put(8 + len(tag), y, "]", "dim")
            harness = str(session.get("harness") or "·")
            hx = grid.put(12, y, harness, "hb_" + harness)
            status = session.get("state")
            marker, marker_key = {"working": ("⠏", "g_spin_dim"), "blocked": ("●", "lvl_r_dim"),
                                  "dead": ("✕", "dead"), "done": ("✓", "done")}.get(
                                      status, ("●", "idle"))
            grid.put(hx + 1, y, marker, marker_key)
            if session_lit:
                for col in range(width):
                    glyph, key, _tint = grid.cells[y][col]
                    grid.cells[y][col] = (glyph, key, 236)
            y += 1
            session_routes = sorted(
                (route for route in vm.routes if route.get("session_tag") == tag
                 and route.get("state") not in ("done", "dead")),
                key=lambda route: route.get("state") != "working")
            for route_index, route in enumerate(session_routes):
                if y >= tree_bottom:
                    break
                grid.add_hit(y, 0, width, ("route", route.get("id"), tag))
                if scope_kind == "route" and str(route.get("id")) == scope_id:
                    grid.fill(0, width, y, " ", tint=236)
                grid.put(1, y, stem, "line")
                if last_session:
                    grid.put(4, y, "   ", "line")
                    grid.put(7, y, "  ", None)
                    branch_x = 9
                else:
                    grid.put(4, y, "┃  ", "line")
                    grid.put(7, y, "  ", None)
                    branch_x = 9
                grid.put(branch_x, y, "┗━ ", "line")
                label = route.get("tree_label") or route.get("title") or "·"
                rh = route.get("harness") or "·"
                label_x = branch_x + 3
                route_label = str(label) + ": "
                grid.put(label_x, y, route_label, "dim", limit=width - 1)
                grid.put(label_x + len(route_label), y, str(rh), "h_" + str(rh), limit=width - 1)
                mark, mark_key = {"running": ("⠏", "g_spin_dim"), "working": ("⠏", "g_spin_dim"),
                                  "blocked": ("●", "lvl_r_dim")}.get(
                    route.get("activity", route.get("state")), ("●", "idle"))
                grid.put(label_x + len(route_label) + len(str(rh)) + 1, y, mark, mark_key)
                if scope_kind == "route" and str(route.get("id")) == scope_id:
                    for col in range(width):
                        glyph, key, _tint = grid.cells[y][col]
                        grid.cells[y][col] = (glyph, key, 236)
                y += 1
                subworker = route.get("subworker")
                if subworker and y < tree_bottom:
                    name, who, activity = subworker
                    grid.add_hit(y, 0, width, ("route", route.get("id"), tag))
                    grid.put(1, y, stem, "line")
                    grid.put(4, y, "   " if last_session else "┃  ", "line")
                    grid.put(9, y, "   ", "line")
                    grid.put(12, y, "┗━ ", "line")
                    sub_label = str(name) + ": "
                    grid.put(15, y, sub_label, "dim", limit=width - 1)
                    grid.put(15 + len(sub_label), y, str(who), "h_" + str(who), limit=width - 1)
                    sub_mark = "g_spin_dim" if activity == "running" else "g_work_off"
                    grid.put(15 + len(sub_label) + len(str(who)) + 1, y, "⠏" if activity == "running" else "●", sub_mark)
                    y += 1

    # COMPUTE is a fixed explorer section. Percentages are source facts; draw the
    # compact host summary from the immutable fixture without probing resources.
    compute_y = 34
    if compute_y < height:
        hosts = tuple(vm.compute or ())
        grid.add_hit(compute_y, 0, width, ("tab", "COMPUTE"))
        for hit_y in range(compute_y + 1, grid.rows - 4):
            grid.add_hit(hit_y, 0, width, ("root", "COMPUTE"))
        grid.fill(0, 27, compute_y, "━", "line")
        grid.put(0, compute_y, "━", "line")
        grid.put(1, compute_y, " COMPUTE ", "head" if state.view == "COMPUTE" else "title_dim")
        grid.put(27, compute_y, " ", "dim")
        grid.put(28, compute_y, f"{len(hosts)}/{len(hosts)}", "dim")
        grid.put(31, compute_y, " ", "dim")
        grid.put(32, compute_y, "┫", "line")
        grid.put(14, compute_y + 1, "cpu", "dim")
        grid.put(20, compute_y + 1, "gpu0", "dim")
        grid.put(27, compute_y + 1, "gpu1", "dim")
        host_labels = tuple(vm.hosts or ())
        for offset, host in enumerate(hosts[:5]):
            row_y = compute_y + 2 + offset
            if row_y >= grid.rows - 4:
                break
            _name, base, cpu, _memory, _disk, gpus = host
            host_record = host_labels[offset] if offset < len(host_labels) else {}
            name = host_record.get("name", _name) if isinstance(host_record, Mapping) else _name
            cpu_load, cpu_total = cpu
            cpu_pct = round(100 * float(cpu_load) / max(1, float(cpu_total)))
            cpu_key = "g_blocked" if cpu_pct >= 80 else "g_idle" if cpu_pct >= 50 else "grp_live"
            busy = any(gpu[1] == "working" for gpu in gpus)
            grid.put(1, row_y, str(name), "work" if base else "soft", limit=12)
            grid.put(13, row_y, f" {cpu_pct:2d}%", cpu_key)
            grid.put(18, row_y, "●", "spin" if busy else "idle")
            for gpu_index, gpu in enumerate(gpus[:2]):
                _, gpu_state, _model, _gpu_key, _util, used, total, _sessions = gpu
                pct = round(100 * float(used) / max(1, float(total)))
                gx = 20 + gpu_index * 7
                key = ("dim" if pct == 0 else "g_blocked" if pct >= 80
                       else "g_idle" if pct >= 50 else "grp_live")
                grid.put(gx, row_y, f" {pct:2d}%", key)
                if gpu_index == 0:
                    grid.put(gx + 5, row_y, "●", "spin" if gpu_state == "working" else "idle")
