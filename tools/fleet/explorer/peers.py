"""Peer graph and filtered LINKS list."""

from .tables import _clip, _tint_row


def draw_peers(grid, vm, state, x=34, y=3):
    use = max(0, grid.cols - x - 2)
    right, bottom = x + use + 1, grid.rows - 4
    grid.fill(x, grid.cols - 1, y, " ", tint=234)
    grid.put(1, y, "PEERS", "head", tint=236)
    grid.put(x - 2, y, "┃", "line")
    y += 1
    nodes = tuple(vm.peer_nodes or ())
    edges = vm.peer_edges or {}
    opened = state.opened_for("pfocus:" + state.view)
    # The approved diagram has hand-positioned lanes and crossing lines. Keep this
    # implementation structural: bounded repository boxes carry the same sessions
    # and harnesses, while LINKS below remains the source of directed relationships.
    box_width = max(18, min(38, (use - 3) // 2))
    max_sessions = max((sum(len(column) for column in columns) for _, columns in nodes), default=1)
    box_height = max_sessions + 2
    step_y = box_height + 1
    for index, (repo, columns) in enumerate(nodes):
        sessions = [tag for col in columns for tag in col]
        if not sessions:
            continue
        bx = x + (index % 2) * (box_width + 3)
        by = y + (index // 2) * step_y
        if by + box_height >= bottom - 2:
            break
        box_right = min(right, bx + box_width)
        name = _clip(str(repo) + "/", max(0, box_width - 7))
        grid.put(bx, by, "┌─ " + name + " " + "─" * max(0, box_width - len(name) - 5) + "┐", "line", limit=box_right)
        for row, tag in enumerate(sessions, 1):
            yy = by + row
            selected = opened == str(tag)
            grid.add_hit(yy, bx, box_right, ("fold", "pfocus:" + state.view, str(tag)))
            if selected:
                grid.fill(bx, box_right, yy, " ", "soft", 236)
                grid.put(bx, yy, "▶", "soft")
            session = next((s for s in vm.sessions if s.get("tag") == tag), {})
            harness = str(session.get("harness") or "·")
            grid.put(bx + 2, yy, f"[{tag}]", "soft")
            grid.put(bx + 8, yy, harness, "hb_" + harness)
            grid.put(bx + 8 + len(harness), yy, " " + _clip(str(session.get("title") or "·"),
                     max(0, box_width - 12 - len(harness))), "dim", limit=box_right - 1)
            if selected:
                _tint_row(grid, yy, bx, box_right, 236)
        grid.put(bx, by + len(sessions) + 1, "└" + "─" * max(0, box_right - bx - 2) + "┘", "line", limit=box_right)
    y += ((len(nodes) + 1) // 2) * step_y + 1
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
