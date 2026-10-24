"""Peer graph and filtered LINKS list."""

from .output import cell_width
from .tables import _clip, _tint_row, repo_rule_label


def _card_height(session, cards):
    card = cards.get(str(session.get("tag")), {})
    owners = card.get("owners", ())
    extra = card.get("extra", ())
    return 3 + len(extra) + (1 + len(owners) if owners else 0)


def _draw_links_compact(grid, vm, state, links, opened, x, y, use, right, bottom):
    grid.put(x, y, "── LINKS", "head", limit=right)
    grid.put(x + 10, y, f"{len(links)}" + (f" · [{opened}]" if opened else ""), "dim", limit=right)
    y += 2
    for link in links:
        if y + 1 >= bottom:
            grid.put(x + 2, bottom - 1, f"… {len(links)} links", "dim", limit=right)
            return
        frm, to, kind, when, note = link[:5]
        start0, start1 = x + 2, x + 20
        endpoint_text = lambda value: (f"[{value[0]}] {value[1]}" if isinstance(value, (tuple, list))
                                      else str(value))
        for start, value, limit in ((start0, frm, start1 - 2), (start1, to, right)):
            text = endpoint_text(value)
            grid.put(start, y, _clip(text, max(0, limit - start)), "soft", limit=limit)
        grid.put(start1 - 2, y, "→", "soft")
        for offset, endpoint in ((start0, frm), (start1, to)):
            if isinstance(endpoint, (tuple, list)) and endpoint and any(
                    session.get("tag") == str(endpoint[0]) for session in vm.sessions):
                grid.add_hit(y, offset, min(right, offset + cell_width(endpoint_text(endpoint))),
                             ("fold", "pfocus:" + state.view, str(endpoint[0])))
        y += 1
        grid.put(x + 4, y, f"{kind} · {when} · {note}", "dim", limit=right)
        y += 1


def draw_peers(grid, vm, state, x=34, y=3):
    """Draw data-sized repository frames, session cards and the complete link list.

    ``peer_edges`` keeps its existing links/cross/side_lanes fields. Optional
    renderer additions are cards[tag] = {extra: [(text, style)], owners:
    [{state, label, stages:[{name, state}]}]}, lane_labels = [{from, to,
    kind, when}], generations = [{tag, text, harness}], and watches =
    [{repo, text}]. These carry reference facts that have no earlier VM field.
    """
    use = max(0, grid.cols - x - 2)
    right, bottom = x + use + 1, grid.rows - 4
    grid.fill(x, grid.cols - 1, y, " ", tint=234)
    grid.put(x - 2, y, "┃", "line")
    y += 1
    nodes = tuple(vm.peer_nodes or ())
    edges = vm.peer_edges or {}
    cards = edges.get("cards", {})
    opened = state.opened_for("pfocus:" + state.view)
    session_by_tag = {str(item.get("tag")): item for item in vm.sessions}
    repo_by_tag = {str(tag): str(repo) for repo, columns in nodes
                   for column in columns for tag in column}
    title_width = max((cell_width(str(s.get("title") or "·")) for s in session_by_tag.values()), default=0)
    box_width = max(28, min(44, title_width + 4))
    frame_gap, column_gap, lane_gap, stack_gap = 3, 2, 6, 2
    layouts = {}
    for repo, columns in nodes:
        cols = [tuple(str(tag) for tag in col if str(tag) in session_by_tag) for col in columns]
        cols = [col for col in cols if col]
        if not cols:
            continue
        col_gaps = [lane_gap if any(tuple(pair) == tuple(col[i:i + 2])
                                    for pair in edges.get("side_lanes", ())
                                    for i in range(max(0, len(col) - 1))) else column_gap
                    for col in cols]
        has_lane = any(gap == lane_gap for gap in col_gaps)
        dx = 2
        col_x = []
        for gap in col_gaps:
            col_x.append(dx)
            dx += box_width + gap
        frame_width = dx + 1 if has_lane else dx - column_gap + 2
        if frame_width > use and len(cols) > 1:
            cols = [tuple(tag for col in cols for tag in col)]
            has_lane = any(tuple(pair) == tuple(cols[0][i:i + 2])
                           for pair in edges.get("side_lanes", ())
                           for i in range(max(0, len(cols[0]) - 1)))
            col_gaps = [lane_gap if has_lane else column_gap]
            frame_width = 2 + box_width + col_gaps[0] + (1 if has_lane else -column_gap + 2)
            col_x = [2]
        row_count = max(map(len, cols))
        row_height = [max(_card_height(session_by_tag[col[row]], cards) + 1
                          for col in cols if row < len(col)) for row in range(row_count)]
        positions = {}
        for ci, col in enumerate(cols):
            yy = 1
            for ri, tag in enumerate(col):
                positions[tag] = (col_x[ci], yy,
                                  _card_height(session_by_tag[tag], cards))
                yy += row_height[ri] + stack_gap
        frame_height = 1 + sum(row_height) + stack_gap * (row_count - 1) + 1
        layouts[str(repo)] = {"repo": str(repo), "columns": cols, "positions": positions,
                              "width": frame_width, "height": frame_height, "col_gaps": col_gaps}

    def band_width(band):
        return sum(column["width"] for column in band) + frame_gap * max(0, len(band) - 1)

    def band_height(band):
        return max((offset + layouts[repo]["height"]
                    for column in band for repo, offset in column["items"]), default=0)

    # Reserve a shared row for explicit cross-repo edges when their frames fit.
    bands, band, placed = [], [], set()
    for edge in edges.get("cross", ()):
        if len(edge) < 2:
            continue
        src, dst = str(edge[0]), str(edge[1])
        src_repo, dst_repo = repo_by_tag.get(src), repo_by_tag.get(dst)
        if src_repo not in layouts or dst_repo not in layouts:
            continue
        source, target = layouts[src_repo], layouts[dst_repo]
        src_y = source["positions"][src][1]
        dst_y = target["positions"][dst][1]
        lo = max(0, dst_y - src_y - (_card_height(session_by_tag[src], cards) - 3))
        hi = dst_y - src_y + (_card_height(session_by_tag[dst], cards) - 3)
        if band and band_width(band) + frame_gap + source["width"] + frame_gap + target["width"] > use:
            bands.append(band); band = []
        if source["width"] + frame_gap + target["width"] > use:
            bands.append([{"width": source["width"], "items": [[src_repo, 0]]}])
            band = [{"width": target["width"], "items": [[dst_repo, 0]]}]
        else:
            band.extend([{"width": source["width"], "items": [[src_repo, lo]], "hi": hi},
                         {"width": target["width"], "items": [[dst_repo, 0]]}])
        placed.update((src_repo, dst_repo))
    if band:
        bands.append(band)

    def fits(candidate, repo):
        frame = layouts[repo]
        for column in candidate:
            grow = max(0, frame["width"] - column["width"])
            if band_width(candidate) + grow > use:
                continue
            if "hi" in column and len(column["items"]) == 1 and frame["height"] + 1 <= column["hi"]:
                column["items"][0][1] = max(column["items"][0][1], frame["height"] + 1)
                column["items"].insert(0, [repo, 0]); column["width"] += grow
                return True
            lowest = max(offset + layouts[name]["height"] for name, offset in column["items"])
            if frame["width"] <= column["width"] and lowest + 1 + frame["height"] <= band_height(candidate):
                column["items"].append([repo, lowest + 1])
                return True
        if band_width(candidate) + frame_gap + frame["width"] <= use or not candidate:
            candidate.append({"width": frame["width"], "items": [[repo, 0]]})
            return True
        return False

    for repo, _columns in nodes:
        repo = str(repo)
        if repo not in layouts or repo in placed:
            continue
        if not any(fits(candidate, repo) for candidate in bands if candidate):
            bands.append([{"width": layouts[repo]["width"], "items": [[repo, 0]]}])
    placements = []
    for candidate in bands:
        offset_x = 0
        row = []
        for column in candidate:
            for repo, offset_y in column["items"]:
                row.append((layouts[repo], offset_x, offset_y))
            offset_x += column["width"] + frame_gap
        placements.append((row, band_height(candidate)))

    frame_boxes, session_boxes = {}, {}
    cursor_y = y
    for band, band_h in placements:
        for frame, offset_x, offset_y in band:
            repo, fw, fh = frame["repo"], frame["width"], frame["height"]
            cursor_x, frame_y = x + offset_x, cursor_y + offset_y
            last_x = min(right - 1, cursor_x + fw - 1)
            repo_name = _clip(repo_rule_label(repo), max(0, last_x - cursor_x - 5))
            grid.put(cursor_x, frame_y, "╔═ " + repo_name + " ", "line", limit=last_x + 1)
            grid.put(last_x, frame_y, "╗", "line")
            for xx in range(cursor_x + 3 + cell_width(repo_name), last_x):
                grid.put(xx, frame_y, "═", "line")
            for row in range(frame_y + 1, min(bottom, frame_y + fh - 1)):
                grid.put(cursor_x, row, "║", "line")
                grid.put(last_x, row, "║", "line")
            for xx in range(cursor_x + 1, last_x):
                grid.put(xx, frame_y + fh - 1, "═", "line")
            grid.put(cursor_x, frame_y + fh - 1, "╚", "line")
            grid.put(last_x, frame_y + fh - 1, "╝", "line")
            frame_boxes[repo] = (cursor_x, frame_y, last_x, frame_y + fh - 1)
            for tag, (dx, dy, card_h) in frame["positions"].items():
                session = session_by_tag[tag]
                bx, by, br = cursor_x + dx, frame_y + dy, min(last_x - 1, cursor_x + dx + box_width - 1)
                card = cards.get(tag, {})
                session_boxes[tag] = (bx, by, br, by + card_h, repo)
                harness = str(session.get("harness") or "other")
                style = "h_" + harness
                selected = opened == tag
                for xx in range(bx + 1, br):
                    grid.put(xx, by, "─", style)
                    grid.put(xx, by + card_h, "─", style)
                for yy in range(by + 1, by + card_h):
                    grid.put(bx, yy, "│", style); grid.put(br, yy, "│", style)
                for xx, yy, glyph in ((bx, by, "┌"), (br, by, "┐"),
                                      (bx, by + card_h, "└"), (br, by + card_h, "┘")):
                    grid.put(xx, yy, glyph, style)
                grid.add_hit(by, bx, br + 1, ("fold", "pfocus:" + state.view, tag))
                session_state = str(session.get("state") or "")
                status, status_style = {"working": ("⠏", "spin"), "active": ("⠏", "spin"),
                    "idle": ("●", "idle"), "blocked": ("◑", "blocked"),
                    "failed": ("✕", "dead"), "done": ("✓", "done"), "dead": ("✕", "dead")}.get(
                        session_state, ("·", "dim"))
                title = _clip(str(session.get("title") or "·"), max(0, br - bx - 3))
                grid.put(bx + 2, by, " ", "branch_s")
                grid.put(bx + 3, by, status, status_style)
                grid.put(bx + 4, by, " ", "branch_s")
                grid.put(bx + 5, by, "[", "tag_b")
                grid.put(bx + 6, by, tag, "steward" if session.get("steward") else "text")
                grid.put(bx + 6 + cell_width(tag), by, "]", "tag_b")
                header_end = bx + 7 + cell_width(tag)
                if session.get("steward"):
                    grid.put(header_end, by, "⚑", "steward")
                    header_end += 1
                else:
                    grid.put(header_end, by, " ", "branch_s")
                grid.put(header_end + 1, by, harness, "hb_" + harness, limit=br)
                harness_end = header_end + 1 + cell_width(harness)
                if harness_end < br:
                    grid.put(harness_end, by, " ", "branch_s")
                grid.put(bx + 2, by + 1, title, "hb_" + harness, limit=br)
                context = session.get("context")
                gauge_width = max(1, min(8, int(round(8 * float(session.get("window") or 1.0)))))
                percent = float(context or 0)
                filled = 0 if percent <= 0 else gauge_width if percent >= 100 else max(
                    1, min(gauge_width - 1, int(percent * gauge_width / 100 + .5)))
                gauge_style = "lvl_r" if percent >= 80 else "lvl_y" if percent >= 50 else "lvl_g"
                grid.put(bx + 2, by + 2, "━" * filled, gauge_style, limit=br)
                grid.put(bx + 2 + filled, by + 2, "─" * (gauge_width - filled), "dim", limit=br)
                if context is not None:
                    grid.put(bx + 11, by + 2, f"{int(context):3d}%", "dim", limit=br)
                yy = by + 3
                for text, key in card.get("extra", ()):
                    grid.put(bx + 2, yy, _clip(str(text), br - bx - 3), key, limit=br); yy += 1
                owners = card.get("owners", ())
                if owners:
                    grid.put(bx, yy, "├", style)
                    for xx in range(bx + 1, br): grid.put(xx, yy, "┈", style)
                    grid.put(br, yy, "┤", style); yy += 1
                    for owner in owners:
                        owner_state = str(owner.get("state") or "")
                        state_mark, owner_style = {"working": ("⠏", "g_spin"), "active": ("⠏", "g_spin"),
                            "dead": ("✕", "g_dead"), "failed": ("✕", "g_dead"),
                            "done": ("✓", "grp_live"), "idle": ("●", "g_work_off"),
                            "blocked": ("◑", "g_blocked")}.get(owner_state, ("·", "loc_repo"))
                        stages = tuple(owner.get("stages", ()))
                        passed = sum(str(stage.get("state")) == "done" for stage in stages)
                        label = str(owner.get("label") or "")
                        grid.put(bx + 2, yy, state_mark, owner_style)
                        grid.put(bx + 4, yy, _clip(label, max(0, br - bx - 12)), "title_dim", limit=br)
                        stage_x = max(bx + 4, br - 2 - len(stages) - 5)
                        hues = ("stg1_off", "stg2_off", "stg3_off", "stg4_off", "stg5_off")
                        for index, stage in enumerate(stages):
                            state_value = str(stage.get("state") or "")
                            if state_value == "done":
                                grid.put(stage_x + index, yy, "✓", "grp_live")
                            elif state_value == "failed":
                                grid.put(stage_x + index, yy, "✕", "g_dead")
                            elif state_value == "active":
                                grid.put(stage_x + index, yy, "●", "g_work")
                            else:
                                grid.put(stage_x + index, yy, "·", hues[index % len(hues)])
                        grid.put(stage_x + len(stages) + 1, yy, f"{passed}/{len(stages)}", "dim")
                        yy += 1
                if selected:
                    for row in range(by, by + card_h + 1): _tint_row(grid, row, bx + 1, br, 236)
                    grid.put(bx + 2, by, " ", "branch_s")
                    grid.put(bx + 3, by, status, status_style)
                    grid.put(bx + 4, by, " ", "branch_s")
                    grid.put(bx + 5, by, "[", "tag_b")
                    grid.put(bx + 6, by, tag, "steward" if session.get("steward") else "text")
                    grid.put(bx + 6 + cell_width(tag), by, "]", "tag_b")
                for hit_y in range(by, by + card_h + 1):
                    grid.add_hit(hit_y, bx, br + 1, ("fold", "pfocus:" + state.view, tag))
        cursor_y += band_h + 1

    # Cross-repository arrows are drawn only when the two cards share a band.
    for edge in edges.get("cross", ()):
        if len(edge) < 2: continue
        src, dst = str(edge[0]), str(edge[1])
        a, b = session_boxes.get(src), session_boxes.get(dst)
        if not a or not b or a[4] == b[4] or a[2] >= b[0]: continue
        line_y = max(a[1], b[1]) + 2
        if line_y >= min(a[3], b[3]): continue
        grid.put(a[2], line_y, "├", "soft")
        for xx in range(a[2] + 1, b[0] - 1): grid.put(xx, line_y, "─", "line")
        grid.put(b[0] - 1, line_y, "→", "soft")

    # Paired lanes wrap around the frame's right gutter and carry labels from VM data.
    labels = edges.get("lane_labels", ())
    for lane in edges.get("side_lanes", ()):
        if len(lane) < 2: continue
        upper, lower = str(lane[0]), str(lane[1])
        top, tail = session_boxes.get(upper), session_boxes.get(lower)
        frame = frame_boxes.get(top[4]) if top else None
        if not top or not tail or not frame or top[4] != tail[4] or top[1] >= tail[1]: continue
        rail = max(top[2], tail[2]) + 2
        if rail >= frame[2] or top[3] >= tail[1]: continue
        y1, y2 = top[1] + 2, tail[1] + 2
        grid.put(top[2], y1, "├", "soft")
        for xx in range(top[2] + 1, rail): grid.put(xx, y1, "─", "line")
        grid.put(rail, y1, "┐", "line")
        for yy in range(y1 + 1, y2): grid.put(rail, yy, "│", "line")
        grid.put(rail, y2, "┘", "line")
        grid.put(tail[2] - 1, y2, "◀", "soft")
        for label in labels:
            if str(label.get("from")) == upper and str(label.get("to")) == lower:
                grid.put(top[0] + 2, top[3] + 1, f"[{upper}]→[{lower}] {label.get('kind', 'steer')} {label.get('when', '')}", "dim")

    for marker in edges.get("generations", ()):
        box = session_boxes.get(str(marker.get("tag")))
        if box:
            grid.put(box[0] + 1, box[3], str(marker.get("text") or ""),
                     "hb_" + str(marker.get("harness") or "claude"), limit=box[2] + 1)
    for marker in edges.get("watches", ()):
        frame = frame_boxes.get(str(marker.get("repo")))
        if frame:
            grid.put(frame[0] + 3, frame[3], " ⚑ " + str(marker.get("text") or ""), "steward",
                     limit=frame[2] + 1)

    y = cursor_y
    links = tuple(edges.get("links", ()))
    if opened:
        links = tuple(link for link in links
                      if any(isinstance(end, (list, tuple)) and str(end[0]) == opened for end in link[:2]))
    if y + 2 >= bottom: return
    if use < 75:
        return _draw_links_compact(grid, vm, state, links, opened, x, y, use, right, bottom)
    grid.put(x, y, "── LINKS", "head", limit=right)
    grid.put(x + 11, y, (f"[{opened}] · {len(links)} links · click it again: all" if opened
                         else "communication · supervision · handover"), "dim", limit=right)
    y += 2
    for off, label in ((3, "FROM"), (19, "TO"), (40, "KIND"), (51, "WHEN"), (64, "NOTE")):
        grid.put(x + off, y, label, "head", limit=right)
    y += 1
    for link in links:
        if y >= bottom:
            grid.put(x + 3, bottom - 1, f"… {len(links) - (y - (bottom - len(links)))} more links", "dim", limit=right)
            break
        frm, to, kind, when, note = link[:5]
        for start, end, value in ((3, 16, frm), (19, 40, to)):
            if isinstance(value, (tuple, list)) and value:
                tag = str(value[0]); harness = str(value[1]) if len(value) > 1 else "·"
                grid.put(x + start, y, f"[{tag}] {harness}", "hb_" + harness, limit=x + end)
                if tag in session_by_tag:
                    grid.add_hit(y, x + start, x + end, ("fold", "pfocus:" + state.view, tag))
            else:
                grid.put(x + start, y, _clip(str(value), end - start), "dim", limit=x + end)
        grid.put(x + 16, y, "→", "soft"); grid.put(x + 40, y, str(kind), "soft")
        grid.put(x + 51, y, str(when), "dim")
        grid.put(x + 64, y, _clip(str(note), max(0, use - 64)), "now_sub", limit=right)
        y += 1
