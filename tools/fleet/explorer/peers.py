"""Peer graph and filtered LINKS list."""

from .tables import _clip, _tint_row, repo_rule_label


def draw_peers(grid, vm, state, x=34, y=3):
    use = max(0, grid.cols - x - 2)
    right, bottom = x + use + 1, grid.rows - 4
    grid.fill(x, grid.cols - 1, y, " ", tint=234)
    grid.put(x - 2, y, "┃", "line")
    y += 1
    nodes = tuple(vm.peer_nodes or ())
    edges = vm.peer_edges or {}
    opened = state.opened_for("pfocus:" + state.view)
    # Build compact repo frames from the VM's ordered columns. Box width and frame
    # height follow the actual content; no repo or session identifiers are special.
    session_by_tag = {str(item.get("tag")): item for item in vm.sessions}
    edges = vm.peer_edges or {}
    repo_by_tag = {str(tag): str(repo) for repo, columns in nodes
                   for column in columns for tag in column}
    frame_gap, column_gap, stack_gap = 3, 2, 1
    box_width = max(20, min(42, (use - 3) // 2))
    frames = []
    for repo, columns in nodes:
        clean_columns = [tuple(str(tag) for tag in col if str(tag) in session_by_tag)
                         for col in columns]
        clean_columns = [col for col in clean_columns if col]
        if not clean_columns:
            continue
        col_width = box_width + column_gap
        frame_width = len(clean_columns) * col_width - column_gap + 2
        if frame_width > use:
            clean_columns = [tuple(tag for col in clean_columns for tag in col)]
            frame_width = min(use, box_width + 2)
        positions = {}
        frame_height = 2
        for col_index, column in enumerate(clean_columns):
            yy = 1
            for tag in column:
                session = session_by_tag[tag]
                extra = bool(session.get("steward") or session.get("context") is not None)
                height = 3 + int(extra)
                positions[tag] = (1 + col_index * col_width, yy, height)
                yy += height + stack_gap
            frame_height = max(frame_height, yy)
        frames.append((str(repo), clean_columns, positions, frame_width, frame_height))

    # Give explicit cross-repo conversations first claim on a shared band. The
    # relationship comes from VM edges; repositories and tags remain data-only.
    frame_by_repo = {frame[0]: frame for frame in frames}
    ordered_frames, included = [], set()
    for edge in edges.get("cross", ()):
        if len(edge) < 2:
            continue
        source_repo = repo_by_tag.get(str(edge[0]))
        target_repo = repo_by_tag.get(str(edge[1]))
        for repo in (source_repo, target_repo):
            if repo in frame_by_repo and repo not in included:
                ordered_frames.append(frame_by_repo[repo])
                included.add(repo)
    ordered_frames.extend(frame for frame in frames if frame[0] not in included)

    # Pack frames left-to-right, then start a new band when the pane runs out.
    bands, band, band_width, band_height = [], [], 0, 0
    for frame in ordered_frames:
        fw, fh = frame[3], frame[4]
        needed = fw if not band else frame_gap + fw
        if band and band_width + needed > use:
            bands.append((band, band_height))
            band, band_width, band_height = [], 0, 0
        band.append(frame)
        band_width += (fw if len(band) == 1 else frame_gap + fw)
        band_height = max(band_height, fh)
    if band:
        bands.append((band, band_height))
    frame_boxes = {}
    session_boxes = {}
    cursor_y = y
    for band_frames, band_h in bands:
        cursor_x = x
        for repo, columns, positions, fw, fh in band_frames:
            last_x = min(right - 1, cursor_x + fw - 1)
            repo_name = _clip(repo_rule_label(repo), max(0, last_x - cursor_x - 5))
            grid.put(cursor_x, cursor_y, "┌─ " + repo_name, "line", limit=last_x)
            for xx in range(cursor_x + 3 + len(repo_name), last_x):
                grid.put(xx, cursor_y, "─", "line")
            grid.put(last_x, cursor_y, "┐", "line")
            for row in range(cursor_y + 1, min(bottom, cursor_y + fh - 1)):
                grid.put(cursor_x, row, "│", "line")
                grid.put(last_x, row, "│", "line")
            grid.put(cursor_x, cursor_y + fh - 1, "└", "line")
            grid.put(last_x, cursor_y + fh - 1, "┘", "line")
            for xx in range(cursor_x + 1, last_x):
                grid.put(xx, cursor_y + fh - 1, "─", "line")
            frame_boxes[repo] = (cursor_x, cursor_y, last_x, cursor_y + fh - 1)
            for tag, (dx, dy, box_h) in positions.items():
                session = session_by_tag[tag]
                bx, by = cursor_x + dx, cursor_y + dy
                br = min(last_x, bx + box_width - 1)
                session_boxes[tag] = (bx, by, br, by + box_h, repo)
                selected = opened == tag
                style = "hb_" + str(session.get("harness") or "other")
                for xx in range(bx, br + 1):
                    grid.put(xx, by, "─", style)
                    grid.put(xx, by + box_h, "─", style)
                for yy in range(by + 1, by + box_h):
                    grid.put(bx, yy, "│", style)
                    grid.put(br, yy, "│", style)
                for xx, yy, glyph in ((bx, by, "┌"), (br, by, "┐"),
                                      (bx, by + box_h, "└"), (br, by + box_h, "┘")):
                    grid.put(xx, yy, glyph, style)
                grid.add_hit(by, bx, br + 1, ("fold", "pfocus:" + state.view, tag))
                if selected:
                    _tint_row(grid, by, bx, br + 1, 236)
                    grid.put(bx + 1, by, "▶", "soft")
                tag_text = f"[{tag}]"
                harness = str(session.get("harness") or "·")
                grid.put(bx + 2, by, tag_text, "steward" if session.get("steward") else "soft",
                         limit=br)
                grid.put(bx + 2, by + 1, _clip(str(session.get("title") or "·"), br - bx - 3),
                         style, limit=br)
                if box_h > 3:
                    context = session.get("context")
                    meta = (f"{context}% ctx" if context is not None else harness)
                    grid.put(bx + 2, by + 2, _clip(meta, br - bx - 3), "dim", limit=br)
                if selected:
                    for row in range(by + 1, by + box_h):
                        _tint_row(grid, row, bx + 1, br, 236)
                for yy in range(by, by + box_h + 1):
                    grid.add_hit(yy, bx, br + 1, ("fold", "pfocus:" + state.view, tag))
            cursor_x = last_x + frame_gap + 1
        cursor_y += band_h + 2
    y = cursor_y
    # Draw a direct arrow only when both boxes share a band and have clear
    # horizontal space between them. The full relationship list remains below.
    for edge in edges.get("cross", ()):
        if len(edge) < 2:
            continue
        source, target = str(edge[0]), str(edge[1])
        src_box, dst_box = session_boxes.get(source), session_boxes.get(target)
        if not src_box or not dst_box or src_box[4] == dst_box[4]:
            continue
        sx, sy, sr, sb, srepo = src_box
        dx, dy, dr, db, drepo = dst_box
        if sr >= dx:
            continue
        line_y = max(sy, dy) + 1
        if line_y >= min(sb, db) or dx - sr < 3:
            continue
        grid.put(sr, line_y, "├", "soft")
        for xx in range(sr + 1, dx - 1):
            grid.put(xx, line_y, "─", "line")
        grid.put(dx - 1, line_y, "→", "soft")
    # A VM may mark two vertically stacked sessions as a side lane. Route both
    # directions around their shared repository frame when the gutter has room.
    for lane in edges.get("side_lanes", ()):
        if len(lane) < 2:
            continue
        upper, lower = str(lane[0]), str(lane[1])
        top, tail = session_boxes.get(upper), session_boxes.get(lower)
        if not top or not tail or top[4] != tail[4] or top[1] >= tail[1]:
            continue
        frame = frame_boxes.get(top[4])
        if not frame:
            continue
        rail = max(top[2], tail[2]) + 1
        if rail >= frame[2] or top[3] >= tail[1]:
            continue
        top_y, tail_y = top[1] + 1, tail[1] + 1
        grid.put(top[2], top_y, "├", "soft")
        for xx in range(top[2] + 1, rail):
            grid.put(xx, top_y, "─", "line")
        grid.put(rail, top_y, "┐", "line")
        for yy in range(top_y + 1, tail_y):
            grid.put(rail, yy, "│", "line")
        grid.put(rail, tail_y, "┘", "line")
        for xx in range(tail[2] - 1, rail):
            grid.put(xx, tail_y, "─", "line")
        grid.put(tail[2] - 1, tail_y, "←", "soft")
    links = tuple(edges.get("links", ()))
    if opened:
        links = tuple(link for link in links
                      if any(isinstance(end, (list, tuple)) and str(end[0]) == opened for end in link[:2]))
    if y + 2 >= bottom:
        return
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
                tag = str(value[0])
                harness = str(value[1]) if len(value) > 1 else "·"
                grid.put(x + start, y, f"[{tag}] {harness}", "hb_" + harness, limit=x + end)
                if any(session.get("tag") == tag for session in vm.sessions):
                    grid.add_hit(y, x + start, x + end, ("fold", "pfocus:" + state.view, tag))
            else:
                grid.put(x + start, y, _clip(str(value), end - start), "dim", limit=x + end)
        grid.put(x + 16, y, "→", "soft")
        grid.put(x + 40, y, str(kind), "soft")
        grid.put(x + 51, y, str(when), "dim")
        grid.put(x + 64, y, _clip(str(note), max(0, use - 64)), "now_sub", limit=right)
        y += 1


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
        end_text = lambda value: (f"[{value[0]}] {value[1]}" if isinstance(value, (tuple, list))
                                  else str(value))
        from_text, to_text = end_text(frm), end_text(to)
        grid.put(start0, y, _clip(from_text, max(0, start1 - start0 - 2)), "soft",
                 limit=start1 - 2)
        grid.put(start1 - 2, y, "→", "soft")
        grid.put(start1, y, _clip(to_text, max(0, right - start1)), "soft", limit=right)
        for offset, endpoint in ((start0, frm), (start1, to)):
            if isinstance(endpoint, (tuple, list)) and endpoint and any(
                    session.get("tag") == str(endpoint[0]) for session in vm.sessions):
                grid.add_hit(y, offset, min(right, offset + len(end_text(endpoint))),
                             ("fold", "pfocus:" + state.view, str(endpoint[0])))
        y += 1
        grid.put(x + 4, y, f"{kind} · {when} · {note}", "dim", limit=right)
        y += 1
