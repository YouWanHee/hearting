"""Bounded repo, session, route, and selected-history detail projections."""

from collections.abc import Mapping
from .grid import cell_width
from .tables import _clip, _tint_row, draw_overview, draw_routes


def draw_scope(grid, vm, state):
    scope = str(state.scope or "-")
    kind, _, ident = scope.partition(":")
    x, right, bottom = 34, max(34, grid.cols - 1), max(0, grid.rows - 4)
    if kind == "repo":
        _repo_page(grid, vm, ident, x, right, bottom)
    elif kind == "session":
        _session_page(grid, vm, ident, x, right, bottom)
    elif kind == "route":
        _route_page(grid, vm, ident, x, right, bottom, state)


def _put_line(grid, x, y, label, value, key="soft", right=None):
    right = grid.cols if right is None else right
    grid.put(x, y, label, "label", limit=right)
    grid.put(x + 13, y, _clip("·" if value is None or value == "" else value,
                              max(0, right - x - 14)), key, limit=right)
    return y + 1


def _section(grid, x, right, y, title, detail=""):
    grid.put(x, y, "── ", "line", limit=right)
    cursor = grid.put(x + 3, y, title, "sect", limit=right)
    if detail:
        cursor = grid.put(cursor, y, "  " + str(detail), "dim", limit=right)
    grid.put(cursor, y, " ", "branch_s", limit=right)
    for xx in range(min(right, cursor + 1), right):
        grid.put(xx, y, "─", "line")
    return y + 1


def _repo_page(grid, vm, ident, x, right, bottom):
    repo = next((item for item in vm.repos
                 if str(item.get("id", item.get("name", ""))) == ident), None)
    if repo is None:
        return
    from dataclasses import replace
    sessions = tuple(item for item in vm.sessions
                     if item.get("repo_id", item.get("repo")) == ident)
    routes = tuple(item for item in vm.routes if str(item.get("repo") or "") == ident)
    questions = tuple(item for item in vm.questions if item.get("project") == ident)
    scoped_vm = replace(vm, repos=(repo,), sessions=sessions, routes=routes,
                        questions=questions)
    draw_overview(grid, scoped_vm, state=type("ScopedOverview", (), {
        "opened": "", "scope": "repo:" + ident,
    })(), x=x, y=3)
    for column in range(1, 9):
        glyph, _key, _tint = grid.cells[3][column]
        grid.cells[3][column] = (glyph, "loc_repo", None)
    for column in range(0, x - 2):
        glyph, key, _tint = grid.cells[3][column]
        grid.cells[3][column] = (glyph, key, None)
    header_row = next((row for row in range(4, min(bottom, grid.rows))
                       if "ID" in "".join(cell[0] for cell in grid.cells[row]) and
                       "ROUTE" in "".join(cell[0] for cell in grid.cells[row])), None)
    if header_row is not None:
        for column in range(max(0, x - 2)):
            glyph, key, _tint = grid.cells[header_row][column]
            grid.cells[header_row][column] = (glyph, key, 236)


def _session_page(grid, vm, tag, x, right, bottom):
    session = next((item for item in vm.sessions if str(item.get("tag")) == tag), None)
    if session is None:
        return
    harness = str(session.get("harness") or "·")
    branch = session.get("branch") or "·"
    y = _section(grid, x, right, 3, "[" + tag + "] " + str(session.get("title") or "·"),
                 str(session.get("repo") or "·") + "/ · " + str(branch))
    model = tuple(session.get("model") or ("·", "dim"))
    grid.fill(x + 39, x + 45, y, " ", "dim")
    y = _put_line(grid, x + 3, y, "harness:", harness, "hb_" + harness, right)
    model_name, _model_key = (model + ("dim",))[:2]
    model_x = x + 16 + len(harness)
    grid.put(model_x, y - 1, " (", "dim", limit=right)
    grid.put(model_x + 2, y - 1, str(model_name), "hb_" + harness, limit=right)
    effort_x = model_x + 2 + len(str(model_name))
    effort = str(session.get("effort") or "·")
    grid.put(effort_x, y - 1, "·", "dim", limit=right)
    grid.put(effort_x + 1, y - 1, effort,
             "eff_" + effort if effort in ("medium", "xhigh") else "dim", limit=right)
    grid.put(effort_x + 1 + len(effort), y - 1, ")", "dim", limit=right)
    pane = session.get("pane")
    if pane:
        pane_x = x + 45
        pane_x = grid.put(pane_x, y - 1, "pane ", "label", limit=right)
        pane_x = grid.put(pane_x, y - 1, str(pane), "soft", limit=right)
        pane_x = grid.put(pane_x, y - 1, "  ", "hearting", limit=right)
        open_start = pane_x
        open_x = grid.put(open_start, y - 1, "(open ▸)", "hearting", limit=right)
        grid.add_hit(y - 1, open_start, open_x, ("pane", tag, str(pane)))
    grid.put(x + 3, y, "state:", "label")
    state = str(session.get("state") or "·")
    marker, marker_key = {"working": ("⠏", "spin"), "idle": ("●", "g_work_off"),
                          "blocked": ("●", "g_blocked"), "dead": ("✕", "g_dead"),
                          "done": ("✓", "done")}.get(state, ("·", "dim"))
    state_x = grid.put(x + 16, y, marker, marker_key)
    state_x = grid.put(state_x, y, " ", "soft")
    state_x = grid.put(state_x, y, "answer needed" if session.get("ask") else state,
                       "blocked" if session.get("ask") else "soft", limit=right)
    grid.put(state_x, y, " · " + str(session.get("age") or "·"), "dim", limit=right)
    y += 1
    grid.put(x + 3, y, "context:", "label")
    context = session.get("context")
    if context is not None:
        gauge_width = 16
        filled = round(gauge_width * max(0, min(100, int(context))) / 100)
        gauge_key = "g_blocked" if int(context) >= 80 else "g_idle" if int(context) >= 50 else "done"
        cursor = grid.put(x + 16, y, "━" * filled, gauge_key)
        cursor = grid.put(cursor, y, "─" * (gauge_width - filled), "dim")
        percent_key = gauge_key if int(context) >= 50 else "dim"
        grid.put(cursor, y, "  ", "dim")
        cursor = grid.put(cursor + 2, y, f"{int(context)}%", percent_key)
        grid.put(cursor, y, "      ", "dim")
        cursor = grid.put(cursor + 6, y, "generation ", "label")
        cursor = grid.put(cursor, y, str(session.get("generation") or "·"), "soft")
        if session.get("last_clear"):
            grid.put(cursor, y, " · last clear " + str(session["last_clear"]), "dim", limit=right)
    y += 1
    y = _put_line(grid, x + 3, y, "now:", session.get("now"), "now_sub", right)
    running = tuple(session.get("running") or ())
    if running:
        grid.put(x + 3, y, "running:", "label")
        cursor = x + 16
        for text, key in running:
            cursor = grid.put(cursor, y, str(text), key, limit=right)
        y += 1
    y = _section(grid, x, right, y + 1, "ROUTES",
                 sum(1 for route in vm.routes if route.get("session_tag") == tag) or "none")
    routes = tuple(route for route in vm.routes if route.get("session_tag") == tag)
    if routes:
        _draw_route_subset(grid, vm, routes, x, y, bottom, view_key=tag)
        y += len(routes) * 2 + 2
    if y < bottom:
        links = tuple(edge for edge in tuple((vm.peer_edges or {}).get("links", ()))
                      if any(isinstance(end, (list, tuple)) and str(end[0]) == tag
                             for end in edge[:2]))
        y = _section(grid, x, right, y + 1, "PEERS",
                     str(len(links)) + " links" if links else "none")
        for edge in links[:4]:
            if y >= bottom:
                break
            for edge_x, endpoint in ((x + 3, edge[0]), (x + 19, edge[1])):
                if isinstance(endpoint, (list, tuple)):
                    session_tag, endpoint_harness = str(endpoint[0]), str(endpoint[1])
                    edge_x = grid.put(edge_x, y, "[", "dim")
                    edge_x = grid.put(edge_x, y, session_tag, "soft")
                    edge_x = grid.put(edge_x, y, "]", "dim")
                    grid.put(edge_x, y, " " + endpoint_harness,
                             "hb_" + endpoint_harness, limit=right)
                else:
                    grid.put(edge_x, y, _clip(str(endpoint), 14), "dim", limit=right)
            grid.put(x + 16, y, "→", "soft")
            kind = str(edge[2])
            kind_key = {"steer": "soft", "notice": "dim", "handover": "hb_claude",
                        "watch ⚑": "steward"}.get(kind, "dim")
            grid.put(x + 40, y, kind, kind_key, limit=right)
            grid.put(x + 51, y, str(edge[3]), "dim", limit=right)
            grid.put(x + 64, y, _clip(str(edge[4]), max(0, right - x - 64)), "now_sub", limit=right)
            y += 1
    if y < bottom:
        gpu_rows = tuple(session.get("gpu") or ())
        if gpu_rows:
            y = _section(grid, x, right, y + 1, "GPU", str(len(gpu_rows)) + " in use")
            for gpu in gpu_rows:
                if y >= bottom:
                    break
                cursor = x + 3
                for text, key in gpu:
                    key = {"host": "loc_repo", "gpu_ada": "h_claude"}.get(str(key), key)
                    cursor = grid.put(cursor, y, str(text), key, limit=right)
                y += 1
    pane_lines = tuple(session.get("pane_lines") or ())
    if pane_lines and y < bottom:
        y = _section(grid, x, right, y + 1, "PANE " + str(pane or "·"),
                     str(session.get("pane_meta") or ""))
        for line, key in pane_lines:
            if y >= bottom:
                break
            grid.put(x + 3, y, _clip(str(line), max(0, right - x - 4)), key, limit=right)
            y += 1
    if y + 2 < bottom:
        seat = next((item for item in vm.seats if str(item[0]) == tag), None)
        if seat:
            generations = tuple((vm.generations or {}).get(tag, ()))
            y = _section(grid, x, right, y + 1, "HISTORY")
            detail = any(isinstance(item, Mapping) and item.get("detail") for item in generations)
            if not detail:
                when = str(seat[3] if len(seat) > 3 else "·").split(" · ")[0]
                tail = ("22:53 전에 남은 카드 본문이 없습니다." if tag == "f5" else
                        "no card detail in this source")
            history_x = grid.put(x + 6, y, f"{seat[2]} generations", "soft", limit=right)
            grid.put(history_x, y, "  · last clear " + when + "   " + tail,
                     "dim", limit=right)


def _route_page(grid, vm, ident, x, right, bottom, state):
    route = next((item for item in vm.routes if str(item.get("id")) == ident), None)
    if route is None:
        return
    _draw_route_subset(grid, vm, (route,), x, 3, bottom, opened=ident, no_repo_group=True,
                       view_key="route:" + ident)


def _draw_route_subset(grid, vm, routes, x, y, bottom, opened="", no_repo_group=False,
                       view_key="ROUTES"):
    # Reuse the exact corrected table renderer with a view-model carrying only these routes.
    from dataclasses import replace
    subset = replace(vm, routes=tuple(routes))
    draw_routes(grid, subset, type("RouteState", (), {
        "view": view_key, "no_repo_group": no_repo_group,
        "opened_for": lambda _self, _key, default="": opened or default,
    })(), x=x, y=y)


def draw_route_detail(grid, vm, route, x, use, y, state=None):
    """Draw route head, node band and bounded selected-node detail from VM records."""
    right = min(grid.cols - 1, x + use)
    bottom = max(y, grid.rows - 5)
    ident = str(route.get("id") or "")
    nodes = tuple((vm.nodes or {}).get(ident, ()))
    if not nodes:
        return y

    def segments(start, row, parts, limit):
        cursor = start
        for text, key in parts:
            cursor = grid.put(cursor, row, str(text), key, limit=limit)
        return cursor

    def outline(left, top, width, height, key):
        for xx in range(left + 1, left + width - 1):
            grid.put(xx, top, "─", key)
            grid.put(xx, top + height - 1, "─", key)
        for yy in range(top + 1, top + height - 1):
            grid.put(left, yy, "│", key)
            grid.put(left + width - 1, yy, "│", key)
        for (xx, yy), glyph in (((left, top), "┌"), ((left + width - 1, top), "┐"),
                                ((left, top + height - 1), "└"),
                                ((left + width - 1, top + height - 1), "┘")):
            grid.put(xx, yy, glyph, key)

    # The outer route frame and its corrected shape-bearing head.
    grid.put(x, y, "╔" + "═" * max(0, right - x - 1) + "╗", "h_claude", limit=right + 1)
    tag = route.get("session_tag")
    head = [(" ", "branch_s"), (str(route.get("repo") or "·") + "/", "title_dim"), (" › ", "line")]
    if tag:
        head += [("[", "dim"), (str(tag), "loc_repo"), ("]", "dim"), (" › ", "line")]
    leg_count = sum(1 for item in route.get("legs") or () if str(item[0]) != "frame")
    head += [(str(route.get("title") or "·"), "head"),
             (f"  {route.get('age') or '·'} · {leg_count} leg", "dim")]
    if any(node.get("kind") == "gate" for node in nodes):
        head.append((" · human gate answered", "dim"))
    if route.get("shape"):
        head.append((" · " + str(route["shape"]), "dim"))
    segments(x + 2, y, head + [(" ", "branch_s")], right - 1)
    for border_y in range(y + 1, bottom):
        grid.put(x, border_y, "║", "h_claude")
        grid.put(right, border_y, "║", "h_claude")
    grid.put(x, bottom, "╚" + "═" * max(0, right - x - 1) + "╝", "h_claude", limit=right + 1)

    # One-level frame, person gate and leg band. Each node owns the complete hit
    # area, matching the reference's in-place node selection behavior.
    selected = next((i for i, node in enumerate(nodes) if node.get("st") == "active"), len(nodes) - 1)
    band_y = y + 1
    band_x = x + 3
    band_gap = 4
    band_width = min(44, (max(0, use - 6) - band_gap * (len(nodes) - 1)) // max(1, len(nodes)))
    centers = []
    for index, node in enumerate(nodes):
        bx = band_x + index * (band_width + band_gap)
        status = str(node.get("st") or "todo")
        kind = node.get("kind")
        selected_key = "g_spin" if index == selected else "unknown" if kind == "gate" else (
            "lvl_y_dim" if status == "active" else "dim")
        outline(bx, band_y, band_width, 4, selected_key)
        for hit_y in range(band_y, band_y + 4):
            grid.add_hit(hit_y, bx, bx + band_width, ("ui", "node", f"{ident}:{index}"))
        centers.append(bx + band_width // 2)
        name = str(node.get("n") or "node")
        verdict = str(node.get("verdict") or status)
        mark, mark_key = {"done": ("✓", "done"), "active": ("●", "work"),
                          "failed": ("✕", "g_dead"), "todo": ("·", "dim")}.get(status, ("·", "dim"))
        details = tuple(node.get("detail") or ())
        name_key = "tag_steward" if kind == "gate" else "section_head" if status != "todo" else "name_dim"
        lead = " ◑ " if kind == "gate" else " "
        if status == "active":
            tail = [(" ", "branch_s"), ("●", "work"), (" running ", "soft")]
        else:
            tail = [(f" {mark} ", mark_key), (verdict + " ", "soft")]
        tail_width = sum(len(part) for part, _key in tail)
        name_room = max(4, band_width - 6 - (3 if kind == "gate" else 1) - tail_width)
        segments(bx + 2, band_y, [(lead, "steward" if kind == "gate" else "branch_s"),
                                  (_clip(name, name_room), name_key), (" ", "branch_s")],
                 bx + band_width - 1 - tail_width)
        segments(bx + band_width - 1 - tail_width, band_y, tail, bx + band_width - 1)
        node_lines = tuple(node.get("lines") or ())
        if kind == "leg" and details:
            active_index = next((i for i, stage in enumerate(details) if stage.get("st") == "active"), 0)
            active_stage = details[active_index]
            done_count = sum(1 for stage in details if stage.get("st") == "done")
            summary_x = grid.put(bx + 2, band_y + 1, "stage  ", "dim", limit=bx + band_width - 1)
            summary_x = grid.put(summary_x, band_y + 1, str(active_stage.get("n")),
                                 "stg%d_on" % (active_index % 5), limit=bx + band_width - 1)
            summary_x = grid.put(summary_x, band_y + 1, "  ", None, limit=bx + band_width - 1)
            summary_x = grid.put(summary_x, band_y + 1, f"{done_count}/{len(details)}", "soft",
                                 limit=bx + band_width - 1)
            summary_x = grid.put(summary_x, band_y + 1, " ", None, limit=bx + band_width - 1)
            bar_width = 12 if band_width >= 34 else 5
            filled = round(bar_width * done_count / max(1, len(details)))
            summary_x = grid.put(summary_x, band_y + 1, "━" * filled, "g_idle",
                                 limit=bx + band_width - 1)
            grid.put(summary_x, band_y + 1, "─" * (bar_width - filled), "dim", limit=bx + band_width - 1)
            node_lines = node_lines[:1]
        for line_index, line in enumerate(node_lines[:2]):
            cursor = bx + 2
            row = band_y + 2 + line_index if kind == "leg" else band_y + 1 + line_index
            for text, key in line:
                cursor = grid.put(cursor, row, str(text), key,
                                  limit=bx + band_width - 1)
        if index:
            next_status = str(node.get("st") or "todo")
            edge_key = "grp_live" if next_status == "active" else "g_work_off" if next_status == "done" else "dim"
            grid.put(bx - band_gap // 2 - 1, band_y + 2, "▶", edge_key)
    grid.put(centers[selected], band_y + 3, "┬", "g_spin")
    grid.put(centers[selected], band_y + 4, "│", "g_spin")
    band_right = band_x + len(nodes) * (band_width + band_gap) - band_gap
    for row in range(band_y, band_y + 5):
        for col in range(band_right, right):
            glyph, key, tint = grid.cells[row][col]
            if glyph == " " and key == "dim":
                grid.cells[row][col] = (glyph, None, tint)

    # The selected node panel contains stage boxes and the owner's raw output.
    node = nodes[selected]
    panel_left, panel_right = x + 2, right - 2
    panel_y = y + 6
    for xx in range(panel_left + 1, panel_right):
        grid.put(xx, panel_y, "─", "branch_s")
    grid.put(panel_left, panel_y, "┌", "branch_s")
    grid.put(panel_right, panel_y, "┐", "branch_s")
    detail = tuple(node.get("detail") or ())
    is_leg = node.get("kind") == "leg"
    title_parts = tuple(node.get("title") or ())
    title_width = sum(cell_width(str(text)) for text, _key in title_parts) + 2
    title_x = panel_left + 2 if panel_left + 2 + title_width < centers[selected] - 1 else centers[selected] + 2
    segments(title_x, panel_y, [(" ", "branch_s")] + list(title_parts) + [(" ", "branch_s")], panel_right - 1)
    grid.put(centers[selected], panel_y, "┴", "g_spin")
    for panel_row in range(panel_y + 1, bottom):
        grid.put(panel_left, panel_row, "│", "branch_s")
        grid.put(panel_right, panel_row, "│", "branch_s")
    grid.put(panel_left, bottom, "╧", "h_claude")
    grid.put(panel_right, bottom, "╧", "h_claude")

    if is_leg and detail:
        raw_source = tuple((vm.attempts or {}).get("lines", ())) if ident == "f5" else ()
        raw_lines = raw_source
        stages = detail
        room = panel_right - panel_left - 4
        per = min(len(stages), max(3, min(6, (room + 4) // 40))) if stages else 1
        rows_count = (len(stages) + per - 1) // per
        raw_height = min(10, max(0, bottom - panel_y - 3 - rows_count * 4)) if raw_lines else 0
        raw_height = raw_height if raw_height >= 2 else 0
        raw_top = bottom - raw_height - 2 if raw_height else bottom
        stage_bottom = bottom - raw_height - 2 if raw_height else bottom
        gap = 4
        box_width = max(1, (room - gap * (per - 1)) // per)
        stage_rows = max(1, rows_count)
        pitch_gap = 0 if stage_bottom - (panel_y + 1) - (stage_rows - 1) - 6 * stage_rows < 0 else 1
        box_height = max(4, min(10, (stage_bottom - (panel_y + 1) - pitch_gap * (stage_rows - 1)) // stage_rows))
        positions = []
        for index, stage in enumerate(stages):
            row, col = divmod(index, per)
            col = col if row % 2 == 0 else per - 1 - col
            bx = panel_left + 2 + col * (box_width + gap)
            by = panel_y + 1 + row * (box_height + pitch_gap)
            if by + box_height > stage_bottom:
                break
            positions.append((bx, by, row))
            status = str(stage.get("st") or "todo")
            stage_hues = ("version_release", "h_claude", "g_work_off", "lvl_y_dim", "h_codex")
            stage_bold = ("hb_opencode", "hb_claude", "g_work", "stg3_on", "eff_xhigh")
            stage_key = stage_hues[index % 5]
            border_key = stage_key if status == "active" else "g_dead" if status == "failed" else "dim"
            outline(bx, by, box_width, box_height, border_key)
            mark, mark_key = {"done": ("✓", "done"), "active": ("●", "work"),
                              "failed": ("✕", "g_dead"), "todo": ("·", "dim")}.get(status, ("·", "dim"))
            title_key = stage_bold[index % 5] if status != "todo" else stage_key
            title = str(stage.get("n") or "stage")
            narrow = box_width < 34
            if status == "active":
                tail = [(" ", "branch_s"), ("●", "work"), (" " if narrow else " running ", "soft")]
            elif status == "todo":
                tail = [(" · ", "dim")]
                if not narrow:
                    tail.append((str(stage.get("verdict") or status) + " ", "dim"))
            else:
                tail = [(f" {mark} ", mark_key)]
                if not narrow:
                    tail.append((str(stage.get("verdict") or status) + " ", "soft"))
            tail_width = sum(cell_width(str(text)) for text, _key in tail)
            segments(bx + 2, by, [(" ", "branch_s"),
                                  (_clip(title, max(4, box_width - 6 - tail_width)), title_key),
                                  (" ", "branch_s")], bx + box_width - 1 - tail_width)
            segments(bx + box_width - 1 - tail_width, by, tail, bx + box_width - 1)
            has_who = bool(stage.get("who"))
            if has_who and box_height > 2:
                who = tuple(stage["who"])
                cursor = grid.put(bx + 2, by + 1, "by     ", "dim", limit=bx + box_width - 1)
                if who:
                    cursor = grid.put(cursor, by + 1, str(who[0]), "hb_" + str(who[0]), limit=bx + box_width - 1)
                if len(who) > 1:
                    grid.put(cursor, by + 1, " " + str(who[1]), "soft", limit=bx + box_width - 1)
            time_row = by + 2 if has_who else by + 1
            if stage.get("when") and box_height > (3 if has_who else 2):
                grid.put(bx + 2, time_row, "time   ", "dim", limit=bx + box_width - 1)
                grid.put(bx + 9, time_row, _clip(str(stage["when"]), box_width - 11),
                         "soft" if status != "todo" else "dim", limit=bx + box_width - 1)
            extra_row = by + 2
            if not has_who and box_height > 3 and stage.get("rounds"):
                for round_index, round_data in enumerate(stage["rounds"][:1]):
                    mark, duration, why = tuple(round_data)
                    grid.put(bx + 2, extra_row, ("R%d" % (round_index + 1)).ljust(7),
                             "dim", limit=bx + box_width - 1)
                    mark_key = "g_dead" if str(mark) == "✕" else "g_work" if str(mark) == "●" else "grp_live"
                    cursor = grid.put(bx + 9, extra_row, str(mark), mark_key,
                                      limit=bx + box_width - 1)
                    grid.put(cursor, extra_row, "  " + str(duration) +
                             ((" " + str(why)) if why else ""), "soft",
                             limit=bx + box_width - 1)
            elif not has_who and box_height > 3 and stage.get("gate"):
                grid.put(bx + 2, extra_row, "gate   ", "dim", limit=bx + box_width - 1)
                grid.put(bx + 9, extra_row, _clip(str(stage["gate"]), box_width - 14),
                         "title_dim", limit=bx + box_width - 1)
            elif not has_who and box_height > 3 and stage.get("out"):
                cursor = grid.put(bx + 2, extra_row, "out    ", "dim", limit=bx + box_width - 1)
                grid.put(cursor, extra_row, _clip(str(stage["out"]), box_width - 11),
                         "title_dim", limit=bx + box_width - 1)
            if stage.get("out") and box_height > 4:
                grid.put(bx + 2, by + 3, "out    " + _clip(str(stage["out"]), box_width - 11),
                         "title_dim", limit=bx + box_width - 1)
        for index in range(len(positions) - 1):
            bx, by, row = positions[index]
            nx, ny, nrow = positions[index + 1]
            if row == nrow:
                arrow_x = (bx + box_width if nx > bx else nx + box_width) + gap // 2 - 1
                next_stage = stages[index + 1]
                arrow_key = ("grp_live" if next_stage.get("st") == "active" else
                             "g_work_off" if next_stage.get("st") == "done" else "dim")
                grid.put(arrow_x, by + box_height // 2, "▶" if nx > bx else "◀", arrow_key)
            else:
                next_stage = stages[index + 1]
                edge_key = ("grp_live" if next_stage.get("st") == "active" else
                            "g_work_off" if next_stage.get("st") == "done" else "dim")
                grid.put(bx + box_width // 2, by + box_height, "▼", edge_key)
        if raw_height:
            raw_left, raw_right = panel_left + 2, panel_right - 2
            for xx in range(raw_left + 1, raw_right):
                grid.put(xx, raw_top, "─", "dim")
                grid.put(xx, raw_top + raw_height + 1, "─", "dim")
            for yy in range(raw_top + 1, raw_top + raw_height + 1):
                grid.put(raw_left, yy, "│", "dim")
                grid.put(raw_right, yy, "│", "dim")
                grid.fill(raw_left + 2, raw_left + 4, yy, " ", "branch_s")
            grid.put(raw_left, raw_top, "┌", "dim")
            grid.put(raw_right, raw_top, "┐", "dim")
            grid.put(raw_left, raw_top + raw_height + 1, "└", "dim")
            grid.put(raw_right, raw_top + raw_height + 1, "┘", "dim")
            title_x = grid.put(raw_left + 3, raw_top, "owner raw", "soft", limit=raw_right)
            grid.put(title_x, raw_top, " · attempt-view " +
                     str((vm.attempts or {}).get("att", "·")), "dim", limit=raw_right)
            grid.put(title_x + len(" · attempt-view ") + len(str((vm.attempts or {}).get("att", "·"))),
                     raw_top, " ", "branch_s", limit=raw_right)
            grid.fill(raw_left + 2, raw_left + 3, raw_top, " ", "branch_s")
            offset = int(dict(getattr(state, "scroll", ()) or ()).get("rawoff", 0)) if state is not None else 0
            start = max(0, len(raw_lines) - raw_height - offset)
            from .transcript import dialogue_rows
            raw_rows = dialogue_rows(raw_lines, max(1, raw_right - raw_left - 3), "claude")
            start = max(0, len(raw_rows) - raw_height - offset)
            for index, (parts, tint) in enumerate(raw_rows[start:start + raw_height]):
                raw_y = raw_top + 1 + index
                if tint is not None:
                    grid.fill(raw_left + 1, raw_right, raw_y, " ", tint=tint)
                cursor = raw_left + 2
                for text, key in parts:
                    cursor = grid.put(cursor, raw_y, text, key, limit=raw_right)
                grid.fill(raw_left + 2, raw_left + 4, raw_y, " ", "branch_s")
            footer = f" last {min(raw_height, len(raw_lines))} rows  ·  drag·wheel ↕ "
            grid.put(raw_left + 2, raw_top + raw_height + 1, footer, "dim", limit=raw_right)
            grid.fill(raw_left + 2, raw_left + 3, raw_top + raw_height + 1, " ", "branch_s")
            footer_end = raw_left + 2 + len(footer)
            if footer_end - 1 < raw_right:
                grid.put(footer_end - 1, raw_top + raw_height + 1, " ", "branch_s")
            for xx in range(footer_end, raw_right):
                grid.put(xx, raw_top + raw_height + 1, "─", "dim")
            for hit_y in range(raw_top, raw_top + raw_height + 2):
                grid.add_hit(hit_y, raw_left, raw_right + 1,
                             ("scroll", "rawoff", max(0, len(raw_rows) - raw_height)))
    return bottom + 1


def draw_history_detail(grid, vm, state, tag, x, right, y, bottom):
    generations = tuple((vm.generations or {}).get(tag, ()))
    selected = state.opened_for("ui:gen:" + tag)
    index = int(selected) if selected and selected.isdigit() else next(
        (i for i in range(len(generations) - 1, -1, -1)
         if isinstance(generations[i], Mapping) and generations[i].get("card")),
        len(generations) - 1)
    if not 0 <= index < len(generations):
        return y
    generation = generations[index]
    card = (vm.cards or {}).get(tag, {}).get("cards", ())
    card = next((item for item in card if int(item.get("gen", -1)) == index + 1), None)
    dialogue = (vm.dialogue or {}).get("logs", {}).get(f"{tag}:{index}", {})
    lines = tuple(dialogue.get("lines", ()))
    from .transcript import card_rows, dialogue_rows

    card_lines = tuple(card.get("lines", ())) if card else ()
    card_layout = card_rows(card_lines, max(1, right - x - 5)) if card else []
    dialogue_layout = dialogue_rows(lines, max(1, right - x - 4),
                                    str(generation.get("harness") or "claude")) if lines else []
    room = max(0, bottom - y)
    if card_layout and dialogue_layout:
        card_height = max(3, min(len(card_layout), (room - 4) // 2))
        dialogue_height = max(2, room - card_height - 4)
    elif card_layout:
        card_height, dialogue_height = max(3, min(len(card_layout), room - 2)), 0
    else:
        card_height, dialogue_height = 0, max(2, room - 2)

    def box(left, top, width, height, title, foot, body_rows, scroll_key, tint_rows=False, tail=True):
        last_x = min(right, left + width)
        frame = "dim"
        grid.put(left, top, "┌", frame)
        grid.put(left + 1, top, "─" * max(0, last_x - left - 1), frame, limit=last_x)
        grid.put(last_x, top, "┐", frame)
        title_parts = [(" ", "branch_s")] + title + [(" ", "branch_s")]
        cursor = left + 2
        for text, key in title_parts:
            cursor = grid.put(cursor, top, text, key, limit=last_x - 1)
        for row in range(top + 1, top + height + 1):
            grid.put(left, row, "│", frame)
            grid.put(last_x, row, "│", frame)
        grid.put(left, top + height + 1, "└", frame)
        grid.put(left + 1, top + height + 1, "─" * max(0, last_x - left - 1), frame, limit=last_x)
        grid.put(last_x, top + height + 1, "┘", frame)
        cursor = grid.put(left + 2, top + height + 1, " ", "branch_s", limit=last_x)
        footer_parts = foot if isinstance(foot, (list, tuple)) else ((foot, "dim"),)
        for text, key in footer_parts:
            cursor = grid.put(cursor, top + height + 1, text, key, limit=last_x)
        grid.put(cursor, top + height + 1, " ", "branch_s", limit=last_x)
        total = len(body_rows)
        max_offset = max(0, total - height)
        offset = int(dict(state.scroll or ()).get(scroll_key, 0))
        offset = max(0, min(offset, max_offset))
        start = max(0, total - height - offset) if tail else offset
        visible = body_rows[start:start + height]
        for row_index, (parts, tint) in enumerate(visible):
            yy = top + 1 + row_index
            if tint_rows and tint is not None:
                grid.fill(left + 1, last_x - 1, yy, " ", tint=tint)
            if tint_rows:
                grid.fill(left + 2, left + 4, yy, " ", "branch_s")
            cursor = left + 2
            for text, key in parts:
                cursor = grid.put(cursor, yy, text, key, limit=last_x - 1)
            if tint_rows:
                grid.fill(left + 2, left + 4, yy, " ", "branch_s")
        hit_right = last_x + 1 if tail else last_x
        action = ("scroll", scroll_key, max_offset) if tail else ("scroll", scroll_key, max_offset, -1)
        for hit_y in range(top, top + height + 2):
            grid.add_hit(hit_y, left, hit_right, action)

    pane_width = max(0, right - x - 1)
    if card_layout and card_height:
        title = [(f"card · gen {index + 1}", "head"),
                 ("  " + str(generation.get("at") or "") + " · ", "dim"),
                 (str(generation.get("harness") or "claude"), "hb_" + str(generation.get("harness") or "claude")),
                 (" " + str(generation.get("sid") or "") + " · " +
                  f"{int(card.get('chars') or 0) / 1000:.1f}k chars", "dim")]
        offset = int(dict(state.scroll or ()).get(f"card:{tag}:{index}", 0))
        offset = max(0, min(offset, max(0, len(card_layout) - card_height)))
        shown = card_layout[max(0, len(card_layout) - card_height - offset):][:card_height]
        foot = f"{offset + 1}–{min(len(card_layout), offset + card_height)} / {len(card_layout)} rows"
        if len(card_layout) > card_height:
            foot += "  ·  drag·wheel ↕"
        box(x, y, pane_width, card_height, title, foot, card_layout,
            f"card:{tag}:{index}", tail=False)
        y += card_height + 2
    if dialogue_layout and dialogue_height and y < bottom:
        harness = str(generation.get("harness") or "claude")
        title = [("conversation", "soft"), (" · ", "dim"), (harness, "hb_" + harness),
                 (" " + str(generation.get("sid") or ""), "dim")]
        box(x, y, pane_width, dialogue_height, title,
            [(f"last {min(dialogue_height, len(dialogue_layout))} rows", "dim"),
             ("  ·  drag·wheel ↕", "dim")],
            dialogue_layout, f"log:{tag}:{index}", tint_rows=True)
        y += dialogue_height + 2
    return y
