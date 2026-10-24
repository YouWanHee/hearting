"""Seat and generation list for the HISTORY view."""

from collections.abc import Mapping
from .tables import _clip, _tint_row, repo_label, repo_rule_label


def draw_history(grid, vm, state, x=34, y=3):
    use = max(0, grid.cols - x - 2)
    right, bottom = x + use + 1, grid.rows - 6
    if use < 75:
        return _draw_history_compact(grid, vm, state, x, y, use, right, bottom)
    grid.fill(x, right, y, " ", tint=234)
    seats = tuple(vm.seats or ())
    opened = state.opened_for("hopen:" + state.view)
    c_id = 18
    c_h, c_gen = c_id + 4 + 3, c_id + 4 + 3 + 8 + 3
    gen_width = min(24, max((len((vm.generations or {}).get(str(seat[0]), ())) for seat in seats), default=0),
                    max(8, use - c_gen - 3 - 3 - 14 - 3 - 16))
    gen_width = max(gen_width, len("GENERATIONS"))
    c_last = c_gen + 3 + gen_width + 3
    c_card = c_last + 14 + 3
    for off, label in ((2, "REPO"), (c_id, "ID"), (c_h, "HARNESS"), (c_gen, "GENERATIONS"),
                       (c_last, "LAST CLEAR"), (c_card, "LAST CARD")):
        grid.put(x + off, y, label, "head", tint=234, limit=right)
    y += 1
    previous_repo = None
    for seat in seats:
        if y > bottom:
            break
        tag = str(seat[0])
        repo = str(seat[1])
        generation_count = int(seat[2]) if len(seat) > 2 else 0
        last_clear = str(seat[3]) if len(seat) > 3 else "·"
        cards = tuple((vm.cards or {}).get(tag, {}).get("cards", ()))
        title = str(seat[5] or "·") if len(seat) > 5 else "·"
        is_open = opened == tag
        grid.add_hit(y, x, right, ("fold", "hopen:" + state.view, tag))
        if is_open:
            grid.fill(x - 1, right, y, " ", tint=236)
            grid.put(x, y, "▶", "soft")
        if repo != previous_repo:
            grid.put(x + 2, y, repo_rule_label(repo, 13), "title_dim")
            previous_repo = repo
        session = next((item for item in vm.sessions if item.get("tag") == tag), {})
        harness = str(session.get("harness") or "·")
        tag_style = "steward" if session.get("steward") else "soft"
        grid.put(x + c_id, y, "[", "dim")
        grid.put(x + c_id + 1, y, tag, tag_style)
        grid.put(x + c_id + 1 + len(tag), y, "]", "dim")
        grid.put(x + c_h, y, harness, "hb_" + harness)
        grid.put(x + c_gen, y, f"{generation_count:2d} ", "soft")
        root_marks = tuple(seat[6]) if len(seat) > 6 else tuple((vm.generations or {}).get(tag, ()))
        shown = root_marks[-gen_width:]
        cursor = x + c_gen + 3
        for index, generation in enumerate(shown):
            if isinstance(generation, Mapping):
                gen_harness = str(generation.get("harness") or harness)
                kind = str(generation.get("kind") or "new")
            else:
                details = generation if isinstance(generation, (list, tuple)) else (generation,)
                gen_harness = str(details[0]) if details else harness
                kind = str(details[1]) if len(details) > 1 else "new"
            glyph = {"new": "●", "cont": "○", "existing": "◆"}.get(kind, "●")
            hue = {"claude": "c", "codex": "m", "opencode": "l"}.get(gen_harness, "d")
            cursor = grid.put(cursor, y, glyph, (hue, int(index == len(shown) - 1), 0))
        grid.put(x + c_last, y, _clip(last_clear, 14), "dim")
        grid.put(x + c_card, y, _clip(title, max(0, right - (x + c_card) - 1)),
                 "soft" if title != "·" else "dim", limit=right)
        if is_open:
            _tint_row(grid, y, x - 1, right, 236)
        y += 1
        if is_open:
            y = draw_seat_expand(grid, vm, state, tag, x, use, y, bottom, seat, harness) + 1
    if y + 3 <= bottom:
        footer_end = x + use
        cursor = grid.put(x + 2, y + 1, "●", "soft", limit=footer_end)
        cursor = grid.put(cursor, y + 1, " new session after clear   ", "dim", limit=footer_end)
        cursor = grid.put(cursor, y + 1, "○", "soft", limit=footer_end)
        cursor = grid.put(cursor, y + 1, " same session after compact   ", "dim", limit=footer_end)
        cursor = grid.put(cursor, y + 1, "◆", "soft", limit=footer_end)
        cursor = grid.put(cursor, y + 1, " a running session took over (retire)   ", "dim", limit=footer_end)
        cursor = grid.put(cursor, y + 1, "colour", "soft", limit=footer_end)
        grid.put(cursor, y + 1, " = harness   ·   click a row: its generations here", "dim", limit=footer_end)


def draw_seat_expand(grid, vm, state, tag, x, use, y, bottom, seat, harness):
    """A seat's generations newest first, then the picked card and conversation; returns the next row."""
    x0 = x + 6
    generations = tuple((vm.generations or {}).get(tag, ()))
    has_history_detail = any(isinstance(item, Mapping) and item.get("detail") for item in generations)
    if not generations or not has_history_detail:
        count = int(seat[2]) if len(seat) > 2 else 0
        last_clear = str(seat[3] if len(seat) > 3 else "·").split(" · ")[0]
        cursor = grid.put(x0, y, f"{count} generations", "soft", limit=x + use + 1)
        usage = (vm.system or {}).get("usage_api") if isinstance(vm.system, Mapping) else None
        stamp = usage.get("freshness") if isinstance(usage, Mapping) else None
        note = f"{stamp} 전에 남은 카드 본문이 없습니다." if stamp else "읽은 카드 본문이 없습니다."
        grid.put(cursor, y, _clip(f"  · last clear {last_clear}   {note}", max(0, x + use - cursor)), "dim",
                 limit=x + use + 1)
        return y + 1
    right = x + use + 1
    selected_generation = state.opened_for("ui:gen:" + tag)
    card_indices = [i for i, item in enumerate(generations)
                    if isinstance(item, Mapping) and item.get("card")]
    default_generation = card_indices[-1] if card_indices else len(generations) - 1
    selected = default_generation
    if selected_generation.isdigit() and 0 <= int(selected_generation) < len(generations):
        selected = int(selected_generation)
    for index in range(len(generations) - 1, -1, -1):
        if y > bottom:
            return y
        gen = generations[index]
        grid.add_hit(y, x0, right, ("ui", "gen:" + tag, index))
        if index == selected:
            grid.fill(x0, right, y, " ", tint=234)
            grid.put(x0 - 2, y, "▶", "soft")
        h = str(gen.get("harness") or harness) if isinstance(gen, Mapping) else harness
        at = str(gen.get("at") or "") if isinstance(gen, Mapping) else ""
        kind = str(gen.get("kind") or "new") if isinstance(gen, Mapping) else "new"
        sid = str(gen.get("sid") or "") if isinstance(gen, Mapping) else ""
        connection = str(gen.get("connection") or "seat start") if isinstance(gen, Mapping) else "seat start"
        title = str(gen.get("title") or "") if isinstance(gen, Mapping) else ""
        if not title:
            title = ("from another seat" if isinstance(gen, Mapping) and gen.get("note") else
                     "no card" if at != "now" else "current generation · no card yet")
        grid.put(x0, y, "now" if at == "now" else f"{index + 1:3d}", "soft")
        grid.put(x0 + 6, y, at if at != "now" else "", "dim")
        hue = {"claude": "c", "codex": "m", "opencode": "l"}.get(h, "d")
        grid.put(x0 + 20, y, {"new": "●", "cont": "○", "existing": "◆"}.get(kind, "●"), (hue, 1, 0))
        cursor = grid.put(x0 + 22, y, h, "hb_" + h)
        grid.put(cursor + 1, y, sid, "dim")
        # A narrow pane has no room for both: the card title takes the connection's place.
        wide = right - x0 >= 75 + 12
        if wide:
            grid.put(x0 + 42, y, _clip(connection, 30), "blocked" if kind == "existing" else "dim")
        title_x = x0 + (75 if wide else 42)
        if title_x < right - 1:
            grid.put(title_x, y, _clip(title, max(0, (use - 75 - 6) if wide else right - title_x - 1)),
                     "soft" if isinstance(gen, Mapping) and gen.get("card") else "dim", limit=right)
        if index == selected:
            _tint_row(grid, y, x0 - 1, right, 234)
        y += 1
    if bottom - y < 4:
        grid.put(x0, min(y, bottom), "↓ card · conversation need a taller pane", "dim")
        return y + 1
    from .pages import draw_history_detail
    return draw_history_detail(grid, vm, state, tag, x0, right, y, bottom)


def _draw_history_compact(grid, vm, state, x, y, use, right, bottom):
    seats = tuple(vm.seats or ())
    opened = state.opened_for("hopen:" + state.view)
    grid.fill(x, right, y, " ", tint=234)
    for off, label in ((3, "ID"), (12, "HARNESS"), (24, "GEN"), (32, "LAST CLEAR")):
        grid.put(x + off, y, label, "head", tint=234, limit=right)
    y += 1
    last_repo = None
    for seat in seats:
        if y + 1 >= bottom:
            return
        tag, repo = str(seat[0]), str(seat[1])
        if repo != last_repo:
            if repo_label(repo):
                grid.put(x + 1, y, "┈ ", "line")
                end = grid.put(x + 3, y, repo + "/", "title_dim")
                for xx in range(end + 1, right):
                    grid.put(xx, y, "┈", "line")
                y += 1
            last_repo = repo
        selected = opened == tag
        action = ("fold", "hopen:" + state.view, tag)
        grid.add_hit(y, x, right, action)
        grid.add_hit(y + 1, x, right, action)
        if selected:
            _tint_row(grid, y, x - 1, right, 236)
            _tint_row(grid, y + 1, x - 1, right, 236)
        grid.put(x + 3, y, f"[{tag}]", "soft")
        session = next((item for item in vm.sessions if item.get("tag") == tag), {})
        harness = str(session.get("harness") or "·")
        grid.put(x + 12, y, harness, "hb_" + harness)
        count = int(seat[2]) if len(seat) > 2 else 0
        grid.put(x + 24, y, f"{count:2d}", "soft")
        grid.put(x + 32, y, _clip(str(seat[3] if len(seat) > 3 else "·"), max(0, right - x - 32)),
                 "dim", limit=right)
        y += 1
        title = str(seat[5] or "·") if len(seat) > 5 else "·"
        grid.put(x + 12, y, _clip(title, max(0, use - 14)), "soft", limit=right)
        y += 1
        if selected:
            generations = tuple((vm.generations or {}).get(tag, ()))
            for index in range(len(generations) - 1, -1, -1):
                if y >= bottom:
                    return
                gen = generations[index]
                if isinstance(gen, Mapping):
                    h = str(gen.get("harness") or harness)
                    kind = str(gen.get("kind") or "new")
                else:
                    details = gen if isinstance(gen, (list, tuple)) else (gen,)
                    h = str(details[0]) if details else harness
                    kind = str(details[1]) if len(details) > 1 else "new"
                mark = {"new": "●", "cont": "○", "existing": "◆"}.get(kind, "●")
                grid.put(x + 14, y, mark, "soft")
                grid.put(x + 16, y, h, "hb_" + h)
                grid.put(x + 25, y, "now" if index == len(generations) - 1 else str(index + 1), "dim")
                y += 1
