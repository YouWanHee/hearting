#!/usr/bin/env python3
"""Freeze reference provenance and cell-level OVERVIEW captures for explorer tests."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_reference(path: Path):
    resolved = path.resolve()
    # Each renderer load must own fresh mock modules. mock13 mutates these helpers
    # during setup (notably the seat list); retaining them across captures makes
    # the 140/168 goldens depend on capture order.
    for name, module in tuple(sys.modules.items()):
        module_path = getattr(module, "__file__", None)
        if not module_path:
            continue
        try:
            if Path(module_path).resolve().parent == resolved:
                del sys.modules[name]
        except OSError:
            continue
    sys.path.insert(0, str(path))
    spec = importlib.util.spec_from_file_location("fleet_reference_renderer", path / "mock13.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load reference renderer: {path / 'mock13.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _same_width(value: str, width) -> str:
    return "x" * max(0, width(value))


_PRIVATE_TEXT_KEYS = {
    "title", "now", "pane", "text", "body", "request", "note", "command",
    "summary", "message", "answer", "dialogue", "content", "description",
    "repo", "project", "name", "path", "cwd", "home", "file", "route_file",
    "worktree", "says", "lines", "raw_lines", "gen_text", "sid",
}
_STRUCTURAL_NAMES = {
    "frame", "plan", "plan-check", "execute", "impl-review", "test", "report",
    "eval-spec", "eval-smoke", "eval-run", "smoke", "setup", "debug", "dev",
}
_STYLE_KEYS = set()
_STYLE_KEYS.update({
    "dim", "soft", "line", "head", "label", "sect", "title_dim", "loc_repo", "name_dim",
    "hb_claude", "hb_codex", "hb_opencode", "h_claude", "h_codex", "h_opencode",
    "branch_s", "section_head", "tag_steward", "hearting_name", "now_sub", "host", "herdr_on",
    "done", "work", "idle", "spin", "g_spin", "g_spin_dim", "g_work", "g_work_off",
    "g_blocked", "g_dead", "grp_live", "grp_hot", "grp_cool", "g_idle", "lvl_y",
    "lvl_y_dim", "lvl_r", "lvl_r_dim", "blocked", "steward", "accent", "hearting",
    "gpu_ada", "eff_medium", "eff_xhigh", "version_release", "fam_gpt", "code",
    "stg0_on", "stg1_on", "stg2_on", "stg3_on", "stg4_on", "stg0_off", "stg1_off",
    "stg2_off", "stg3_off", "stg4_off", "unknown",
})


def _scrub(value, width, key=""):
    """Copy JSON-like reference data, replacing user text before any rendering."""
    if isinstance(value, dict):
        return {k: _scrub(v, width, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)) and len(value) == 2:
        style = value[1]
        if ((isinstance(style, (list, tuple)) and len(style) == 3) or
                (isinstance(style, str) and style in _STYLE_KEYS)):
            return [_scrub(value[0], width, key), style]
    if isinstance(value, list):
        return [_scrub(item, width, key) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub(item, width, key) for item in value)
    if isinstance(value, set):
        return sorted(_scrub(item, width, key) for item in value)
    if isinstance(value, str) and key in _PRIVATE_TEXT_KEYS and not (
            key == "name" and value in _STRUCTURAL_NAMES):
        return _same_width(value, width)
    return value


def _jsonable(value):
    """Drop runtime objects while retaining the complete serializable source facts."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()
                if not callable(v)}
    if isinstance(value, (list, tuple, set)):
        items = [_jsonable(item) for item in value if not callable(item)]
        return sorted(items) if isinstance(value, set) else items
    return None


def _safe_answered(rows, width):
    result = []
    for repo, entries in rows:
        result.append([_same_width(str(repo), width), [
            [entry[0], _same_width(str(entry[1]), width)] for entry in entries
        ]])
    return result


def _safe_records(records, width, name_index=0):
    result = []
    for record in records:
        row = list(record)
        if len(row) > name_index and isinstance(row[name_index], str):
            row[name_index] = _same_width(row[name_index], width)
        result.append(row)
    return result


def _safe_memory(records, width):
    def scrub(text):
        result = []
        for char in str(text):
            if char == "…":
                result.append(char)
                continue
            cells = max(1, width(char))
            result.append((" " if char.isspace() else "界") if cells == 2 else
                          (" " if char.isspace() else "x"))
        return "".join(result)
    result = []
    for repo, entries in records:
        result.append([scrub(repo), [
            [*entry[:-1], scrub(entry[-1])] for entry in entries
        ]])
    return result


def _redact_blob(value, width):
    """Preserve record shape while replacing every embedded source string."""
    if isinstance(value, str):
        return _same_width(value, width)
    if isinstance(value, dict):
        return {key: _redact_blob(item, width) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_blob(item, width) for item in value]
    if isinstance(value, set):
        return sorted(_redact_blob(item, width) for item in value)
    return value


def sanitize_reference(renderer) -> None:
    """Replace private content in source records before drawing, preserving cell widths."""
    width = renderer.width
    original_projects = list(renderer.PROJECTS)
    project_names = {}
    sanitized_projects = []
    for name, done, age, sessions in original_projects:
        safe_name = _same_width(str(name), width)
        if isinstance(done, str):
            done = _same_width(done, width)
        project_names[name] = safe_name
        safe_sessions = []
        for session in sessions:
            session["title"] = _same_width(str(session.get("title", "")), width)
            if session.get("now") is not None:
                session["now"] = _same_width(str(session["now"]), width)
            if session.get("ask") is not None:
                session["ask"] = _same_width(str(session["ask"]), width)
            safe_sessions.append(session)
        sanitized_projects.append((safe_name, done, age, safe_sessions))
    renderer.PROJECTS[:] = sanitized_projects
    if hasattr(renderer, "PROJECT_OF"):
        old_projects = dict(renderer.PROJECT_OF)
        renderer.PROJECT_OF.clear()
        renderer.PROJECT_OF.update({tag: project_names.get(name, name)
                                    for tag, name in old_projects.items()})
    for row in getattr(renderer, "ROUTE_ROWS", ()):
        row["repo"] = project_names.get(row.get("repo"), row.get("repo"))
        record = row.get("r")
        if isinstance(record, dict):
            record["title"] = _same_width(str(record.get("title", "")), width)
            for key in ("now", "note", "request", "pane", "summary"):
                if isinstance(record.get(key), str):
                    record[key] = _same_width(record[key], width)
    for pick, segments in getattr(renderer, "ROUTE_HEAD", {}).items():
        renderer.ROUTE_HEAD[pick] = [
            ((_same_width(str(text[:-1]), width) + "/" if str(text).endswith("/") else
              _same_width(str(text), width)), style)
            if style in ("head", "title_dim") and text not in _STRUCTURAL_NAMES else (text, style)
            for text, style in segments
        ]
    raw = getattr(renderer, "_RAW", None)
    if isinstance(raw, dict):
        safe_raw = _redact_blob(raw, width)
        renderer._RAW = safe_raw
        owner_says = getattr(renderer, "OWNER_SAYS", None)
        if isinstance(owner_says, dict) and "lines" in safe_raw:
            owner_says["f5"] = (safe_raw.get("att", ""), safe_raw["lines"])
    # Other source modules own independent literal data. Scrub only their
    # content fields; semantic identifiers, state, model, and style keys stay intact.
    for module in tuple(sys.modules.values()):
        module_path = getattr(module, "__file__", None)
        if not module_path or not str(module_path).startswith(str(renderer.path)):
            continue
        palette = getattr(module, "K", None)
        if isinstance(palette, dict):
            _STYLE_KEYS.update(str(name) for name in palette)
        for name, value in tuple(vars(module).items()):
            if name in ("UI", "SESSION_EXTRA"):
                continue  # renderer modules share this mutable interaction-state map
            if name.isupper() and isinstance(value, (dict, list, tuple, set)):
                setattr(module, name, _scrub(value, width))
    extra = getattr(renderer, "SESSION_EXTRA", None)
    if isinstance(extra, dict):
        safe_extra = {}
        for tag, fields in extra.items():
            fields = dict(fields)
            for key in ("running", "gpu"):
                if key in fields:
                    groups = fields[key] if key == "gpu" else [fields[key]]
                    fields[key] = [[(_same_width(str(segment[0]), width), segment[1])
                                    for segment in group]
                                   for group in groups]
                    if key == "running":
                        fields[key] = fields[key][0]
            if "pane" in fields:
                meta, lines = fields["pane"]
                fields["pane"] = (_same_width(str(meta), width),
                                  [(_same_width(str(line), width), style) for line, style in lines])
            safe_extra[tag] = fields
        extra.clear()
        extra.update(safe_extra)
    # These list-shaped source records have user text in positional fields, so
    # apply the same width-preserving scrub used for their serialized fixture.
    if hasattr(renderer, "mock4"):
        renderer.mock4.ANSWERED = _safe_answered(renderer.mock4.ANSWERED, width)
    if hasattr(renderer, "mock5"):
        renderer.mock5.SEATS = _safe_records(renderer.mock5.SEATS, width, 1)
        if hasattr(renderer, "SEAT"):
            renderer.SEAT = {row[0]: row for row in renderer.mock5.SEATS}
    if hasattr(renderer, "PEER_PROJECTS"):
        renderer.PEER_PROJECTS[:] = [(_same_width(str(repo), width), columns)
                                     for repo, columns in renderer.PEER_PROJECTS]
    if hasattr(renderer, "mock6"):
        renderer.mock6.LINKS = [
            [*link[:4], _same_width(str(link[4]), width)] if len(link) > 4 else list(link)
            for link in renderer.mock6.LINKS
        ]
    feed = getattr(renderer.mock3, "FEED", ())
    for index, item in enumerate(feed):
        if isinstance(item, list) and item:
            item[-1] = _same_width(str(item[-1]), width)
        elif isinstance(item, tuple) and item:
            feed[index] = (*item[:-1], _same_width(str(item[-1]), width))


def _fixture_values(renderer):
    """Project complete, already scrubbed reference records into the stable VM shape."""
    projects = []
    sessions = []
    for name, done, age, rows in renderer.PROJECTS:
        projects.append({"id": name, "name": name, "done": done, "age": age})
        for row in rows:
            tag = row.get("tag")
            extra = dict(getattr(renderer, "SESSION_EXTRA", {}).get(tag, {}))
            seat = getattr(renderer, "SEAT", {}).get(tag)
            pane = getattr(renderer, "SESSION_PANE", {}).get(tag)
            pane_preview = extra.get("pane")
            sessions.append({
                "tag": tag, "harness": row.get("h"),
                "model": row.get("m"), "effort": row.get("e"),
                "state": row.get("st"), "steward": row.get("steward", False),
                "title": row.get("title"), "context": row.get("ctx"),
                "window": row.get("win"), "now": row.get("now"), "ask": row.get("ask"),
                "age": row.get("age"), "repo": name, "repo_id": name,
                "branch": ("—" if renderer.session_branch(tag) == "—" else
                           _same_width(str(renderer.session_branch(tag).strip(" ()")), renderer.width)),
                "pane": str(pane) if pane else None,
                "generation": seat[2] if seat and len(seat) > 2 else None,
                "last_clear": str(seat[3].split(" · ")[0])
                              if seat and len(seat) > 3 else None,
                "running": extra.get("running"), "gpu": extra.get("gpu"),
                "pane_meta": pane_preview[0] if pane_preview else None,
                "pane_lines": pane_preview[1] if pane_preview else (),
            })
    hosts = [{"mark": mark, "name": name, "key": key}
             for mark, name, key in renderer.mock12.HOSTS]
    usage = [{"text": str(label), "key": key, "windows": values}
             for label, key, values in renderer.mock3.USAGE]
    routes = []
    for row in renderer.ROUTE_ROWS:
        record = row.get("r", {})
        tree_label = renderer.route_name(record)
        subworker = renderer.depth2(row)
        pick = row.get("pick")
        kind = renderer.ROUTE_KIND.get(pick, ("·", None, None, None))
        worker_kind = renderer.WORKER_KIND.get(pick, ("·", None))
        stage_names = [stage.get("n") or stage.get("name") for stage in record.get("stages", ())]
        hue = (renderer.mock2.STAGE[stage_names.index(worker_kind[0]) % len(renderer.mock2.STAGE)]
               if worker_kind[0] in stage_names else None)
        step_key = [hue, 0, 0] if hue else None
        stage_key = [hue, 1, 0] if hue else None
        routes.append({"id": pick, "repo": row.get("repo"),
                       "session_tag": row.get("from"), "title": record.get("title"),
                       "state": record.get("st"), "worker": row.get("worker"),
                       "age": record.get("age"), "legs": record.get("legs", ()),
                       "stages": record.get("stages", ()), "resume": record.get("resume"),
                       "activity": renderer.route_act(record),
                       "loops": record.get("loops"),
                       "worktree": row.get("wt"), "tree_label": tree_label,
                       "harness": record.get("h"), "model": record.get("m"),
                       "effort": record.get("e"), "mode": kind[1], "intensity": kind[2],
                       "profile": kind[3], "capability": kind[0],
                       "shape": renderer.ROUTE_SHAPE.get(pick, "·"),
                       "past_from": renderer.PAST_FROM.get(pick),
                       "worker_stage": worker_kind[0], "worker_unit": worker_kind[1],
                       "worker_stage_key": stage_key,
                       "worker_step_key": step_key,
                       "owner_context": renderer.OWNER_CTX.get(pick),
                       "subworker": subworker})
    seats = [list(row) for row in renderer.seats_by_repo()]
    generations = {}
    for seat in seats:
        if len(seat) < 5:
            seat.insert(4, False)
        chain = renderer.chain(seat[0])
        cards = [item for item in chain if item.get("card")]
        title = (renderer.card_title(cards[-1]) or "") if cards else ""
        seat.append(_same_width(str(title), renderer.width))
        seat.append(_jsonable(renderer.LINEAGE.get(seat[0], ())))
        generation_items = [{
            "harness": item.get("h"), "sid": _same_width(str(item.get("sid") or ""), renderer.width),
            "at": item.get("at"), "kind": item.get("kind"), "connection": item.get("conn"),
            "note": item.get("note"), "card": bool(item.get("card")), "detail": True,
            "title": _same_width(str(renderer.card_title(item) or ""), renderer.width),
        } for item in chain]
        if not generation_items:
            generation_items = [{"harness": str(item[0]), "kind": str(item[1]), "detail": False,
                                 "card": False, "title": ""}
                                for item in renderer.LINEAGE.get(seat[0], ())]
        generations[seat[0]] = generation_items
    return {
        "identity": "reference-fixture", "clock": "synthetic",
        "hosts": hosts, "usage": usage, "census": {},
        "repos": projects, "sessions": sessions, "routes": routes,
        "nodes": _jsonable(_scrub(renderer.ROUTE_NODES, renderer.width)),
        "attempts": _jsonable(_redact_blob(getattr(renderer, "_RAW", {}), renderer.width)),
        "questions": _scrub(renderer.mock4.QUESTIONS, renderer.width),
        "answered": _safe_answered(renderer.mock4.ANSWERED, renderer.width),
        "seats": seats,
        "generations": generations,
        "cards": _jsonable(_redact_blob(getattr(renderer, "HDATA", {}), renderer.width)),
        "dialogue": _jsonable(_redact_blob({
            "logs": getattr(renderer, "LOGS", {}),
            "generation_text": getattr(renderer, "GEN_TEXT_0C", {}),
        }, renderer.width)),
        "peer_nodes": _safe_records(renderer.PEER_PROJECTS, renderer.width, 0),
        "peer_edges": {"links": _scrub(renderer.mock6.LINKS, renderer.width),
                       "cross": _scrub(renderer.PEER_CROSS, renderer.width),
                       "side_lanes": _scrub(renderer.PEER_SIDE_LANES, renderer.width)},
        "compute": _safe_records(renderer.mock3.COMPUTE, renderer.width, 0),
        "memory": _safe_memory(getattr(renderer.mock4, "MEMORY", []), renderer.width),
        "models": _jsonable({
            "profiles": renderer.PROFILES, "profile_map": renderer.PROFILE_MAP,
            "model_choices": renderer.MODEL_CHOICES, "main_only": renderer.MAIN_ONLY,
            "efforts": renderer.EFFORTS, "pools": renderer.POOLS,
            "effort_keys": renderer.EFF_KEY, "shown": renderer.MODEL_SHOWN,
            "context": renderer.CTX, "context_source": renderer.CTX_HOW,
            "tidy": renderer.TIDY, "token_units": renderer.KTOK,
            "allocation": {"fields": [
                ["strategy:", [["balanced", "soft"], ["   usage gate ", "label"], ["85%", "lvl_y"],
                                ["   window ", "label"], ["30 attempts", "soft"],
                                ["   headroom exponent ", "label"], ["2", "soft"]]],
                ["weights:", [["codex", "hb_codex"], [" ", "dim"], ["▮" * 10, "hb_codex"], [" 1.0    ", "dim"],
                              ["opencode", "hb_opencode"], [" ", "dim"], ["▮" * 4, "hb_opencode"], [" 0.4    ", "dim"],
                              ["claude", "hb_claude"], [" ", "dim"], ["▮" * 2, "hb_claude"], [" 0.2    ", "dim"]]],
                ["affinity:", [["worker → ", "dim"], ["codex", "hb_codex"], [" (0.85)", "dim"],
                               ["   owner → ", "dim"], ["usage-weighted, no fixed harness", "soft"]]],
                ["per capability:", [["autopilot-code  ", "soft"],
                                      ["frame · plan · plan-check · execute · impl-review · test · report", "dim"],
                                      ["  →  ", "line"], ["diverse", "soft"], ["     others: default", "dim"]]],
                ["context window:", [["800k", "soft"], [" claude · codex · opencode", "dim"],
                                      ["   confirmation ", "label"], ["hybrid", "soft"],
                                      ["   permissions ", "label"], ["bypass", "soft"]]]],
                "note": "these keys have no setter command yet — Fleet would edit the YAML and run its validator"},
        }),
        "system": {
            "install": {"version": "v3.41.1", "managed": True, "pinned": True,
                        "distribution": True},
            "runtimes": ["claude", "codex", "opencode"],
            "usage_api": {"state": "on", "freshness": "22:53"},
            "collectors": ["herdr", "jobs"],
            "dispatch": {"governor_active": 1, "governor_cap": 30,
                         "jobs": 2, "resource_running": 1, "resource_exited": 180},
            "dispatch_count": 1,
            "peer_delivery": {"pending": 980, "records": 200},
            "memory_summary": {"added_today": 32, "projects": 6, "expired": 43,
                               "expired_at": "00:03", "latest": 15},
        "answered_total": 32,
            "unregistered_gpu_processes": [
                ["moving4", 0, "python", 25.6, "1d 6h"],
                ["cnn", 0, "python", 20.2, "3d 16h"],
                ["moving4", 1, "python", 19.7, "19h"],
                ["mpwav", 0, "python", 19.7, "3h"]],
            "unregistered_gpu_summary": {"count": 11, "remaining": 7},
        },
        "events": [list(item[:-1]) + [_same_width(str(item[-1]), renderer.width)]
                   for item in renderer.mock3.FEED],
    }


def capture(reference: Path, cols: int, view="OVERVIEW", ui=None, scope=None) -> dict:
    renderer = load_reference(reference)
    renderer.path = reference
    sanitize_reference(renderer)
    renderer.GITV = 0
    renderer.DESIGN = 0
    renderer.BEAT = 1
    renderer.GENSEL = -1
    renderer.UI.clear()
    renderer.UI.update(ui or {})
    original_section = renderer.section
    def scrubbed_section(grid, x, width, y, title, meta=""):
        if title == "ANSWERED TODAY" and meta:
            first, *rest = str(meta).split(" ", 1)
            meta = _same_width(first, renderer.width) + (" " + rest[0] if rest else "")
        return original_section(grid, x, width, y, title, meta)
    renderer.section = scrubbed_section
    if hasattr(renderer, "ORIG") and isinstance(renderer.ORIG, dict):
        renderer.ORIG["section"] = scrubbed_section
    original_card_title = renderer.card_title
    renderer.card_title = lambda item: _same_width(str(original_card_title(item) or ""), renderer.width)
    original_chain = renderer.chain
    def scrubbed_chain(tag):
        items = original_chain(tag)
        for item in items:
            item["sid"] = _same_width(str(item.get("sid") or ""), renderer.width)
        return items
    renderer.chain = scrubbed_chain
    question_draw = renderer.ROOT_FN.get("QUESTIONS")
    if question_draw is not None:
        question_draw.__globals__["section"] = scrubbed_section
    history_draw = renderer.ROOT_FN.get("HISTORY")
    if history_draw is not None:
        history_draw.__globals__["card_title"] = renderer.card_title
        history_draw.__globals__["chain"] = renderer.chain
    original_grammar = renderer.grammar
    def scrubbed_grammar(design):
        original_grammar(design)
        for module in tuple(sys.modules.values()):
            module_path = getattr(module, "__file__", None)
            if module_path and str(module_path).startswith(str(reference)) and hasattr(module, "section"):
                module.section = scrubbed_section
            for function_name in ("t_questions", "t_history_root"):
                function = getattr(module, function_name, None)
                if callable(function):
                    function.__globals__["section"] = scrubbed_section
                    function.__globals__["card_title"] = renderer.card_title
    renderer.grammar = scrubbed_grammar
    grid, hits = renderer.render_state(scope, view, cols, 45, False)
    if view == "MEMORY":
        # Public fixture records keep their original display width but do not
        # publish memory project names or record bodies from the live source.
        for row in range(4, 41):
            if grid.c[row][48][0] not in "0123456789":
                continue
            for cell_x in (*range(35, 47), *range(89, cols - 1)):
                glyph = grid.c[row][cell_x][0]
                if glyph in (" ", ""):
                    continue
                if glyph == "…":
                    continue
                grid.c[row][cell_x][0] = "界" if renderer.width(glyph) == 2 else "x"
    # mock4.chrome owns a literal feed example instead of reading the scrubbed
    # FEED record. Preserve its cell footprint and key/tint while keeping that
    # hard-coded body out of the public golden.
    feed_y = 45 - 3
    feed_cells = grid.c[feed_y]
    feed_text = "".join(cell[0] for cell in feed_cells)
    marker = "mem changed · "
    marker_at = feed_text.find(marker)
    if marker_at >= 0:
        body_start = marker_at + len(marker)
        body_width = renderer.width(str(renderer.mock3.FEED[0][-1]))
        for cell_x in range(body_start, min(len(feed_cells), body_start + body_width)):
            feed_cells[cell_x][0] = "x"
    style_keys = {
        ("d", 0, 1): "dim", ("d", 0, 0): "branch_s", ("w", 0, 0): "loc_repo",
        ("w", 0, 1): "name_dim", ("w", 1, 0): "section_head", ("r", 0, 0): "g_blocked",
        ("r", 0, 1): "lvl_r_dim", ("r", 1, 0): "g_dead", ("g", 0, 0): "grp_live",
        ("g", 0, 1): "g_work_off", ("g", 1, 0): "g_work", ("c", 0, 0): "hb_claude",
        ("c", 0, 1): "h_claude", ("c", 1, 0): "hb_claude",
        ("m", 0, 0): "hb_codex", ("m", 0, 1): "h_codex", ("m", 1, 0): "eff_xhigh",
        ("m", 1, 1): "hb_codex", ("l", 0, 0): "hb_opencode", ("l", 0, 1): "version_release",
        ("l", 1, 0): "hb_opencode",
        ("v", 0, 0): "g_spin", ("v", 0, 1): "g_spin_dim", ("v", 1, 0): "hearting_name",
        ("y", 0, 0): "g_idle", ("y", 0, 1): "lvl_y_dim", ("y", 1, 0): "stg3_on",
        ("p", 1, 0): "tag_steward",
    }
    cells = []
    for row in grid.c:
        cells.append([[cell[0], style_keys.get(tuple(cell[1]), "unknown") if cell[1] is not None else None,
                       cell[2]] for cell in row])
    title = renderer.scope_title(renderer.parse_scope(scope), view)
    return {"cols": cols, "rows": len(cells), "title": title,
            "cells": cells, "hits": [list(hit) for hit in hits]}


def list_captures(reference: Path, cols: int) -> dict:
    states = {view: capture(reference, cols, view) for view in
              ("ROUTES", "QUESTIONS", "HISTORY", "PEERS")}
    states["QUESTIONS-fold-bb"] = capture(reference, cols, "QUESTIONS",
                                           {"qshut:QUESTIONS": "bb"})
    states["HISTORY-open-0c"] = capture(reference, cols, "HISTORY",
                                         {"hopen:HISTORY": "0c"})
    states["PEERS-pick-f5"] = capture(reference, cols, "PEERS",
                                       {"pfocus:PEERS": "f5"})
    for view in ("COMPUTE", "MEMORY", "MODELS", "SYSTEM"):
        states[view] = capture(reference, cols, view)
    root = load_reference(reference)
    root.path = reference
    sanitize_reference(root)
    first_repo = str(root.PROJECTS[0][0])
    states["repo-page"] = capture(reference, cols, scope="repo:" + first_repo)
    states["session-page-f5"] = capture(reference, cols, scope="f5")
    states["route-page-f5"] = capture(reference, cols, scope="route:f5")
    states["ROUTES-open-f5"] = capture(reference, cols, "ROUTES", {"ropen:ROUTES": "f5"})
    states["HISTORY-open-0c-card"] = capture(reference, cols, "HISTORY",
                                               {"hopen:HISTORY": "0c"})
    states["HISTORY-open-0c-picked-generation"] = capture(
        reference, cols, "HISTORY",
        {"hopen:HISTORY": "0c", "gen:f5": 0})
    return states


def _runs(cells):
    """Encode each exact cell row as [glyph, colour key, tint, count] runs."""
    encoded = []
    for row in cells:
        runs = []
        for glyph, colour, tint in row:
            cell = [glyph, colour, tint]
            if runs and runs[-1][:3] == cell:
                runs[-1][3] += 1
            else:
                runs.append([*cell, 1])
        encoded.append(runs)
    return encoded


def _compact_capture(capture_data):
    capture_data["cells"] = _runs(capture_data["cells"])
    return capture_data


def _compact_states(states):
    for state in states.values():
        _compact_capture(state)
    return states


def build(reference: Path, output: Path) -> dict:
    required = ["README.md", "BRIEF.md", "mock13.py", "view.py", "state.json",
                "raw_f5.json", "logs_hist.json", "history_data.json"]
    missing = [name for name in required if not (reference / name).is_file()]
    if missing:
        raise RuntimeError("reference files missing: " + ", ".join(missing))
    pngs = sorted((reference / "png").glob("*.png"))
    if not pngs:
        raise RuntimeError("reference PNG inventory is empty")
    logical_names = {
        "README.md": "readme", "BRIEF.md": "design-brief", "mock13.py": "renderer",
        "view.py": "viewer", "state.json": "snapshot", "raw_f5.json": "route-record",
        "logs_hist.json": "history-records", "history_data.json": "generation-records",
    }
    provenance = {logical_names[name]: digest(reference / name) for name in required}
    provenance.update({f"capture/{i + 1:02d}": digest(p) for i, p in enumerate(pngs)})
    manifest = {
        "schema": 1,
        "reference_files": provenance,
        "reference_png_count": len(pngs),
        "states": ["OVERVIEW", "ROUTES", "QUESTIONS", "HISTORY", "PEERS",
                   "QUESTIONS-fold-bb", "HISTORY-open-0c", "PEERS-pick-f5",
                   "COMPUTE", "MEMORY", "MODELS", "SYSTEM", "repo-page",
                   "session-page-f5", "route-page-f5", "ROUTES-open-f5",
                   "HISTORY-open-0c-card", "HISTORY-open-0c-picked-generation"],
        "widths": [140, 168],
        "rows": 45,
        "variant": {"scope": "-", "view": "OVERVIEW", "uopen": 0,
                    "gitv": 0, "design": 0, "beat": 1, "gen": -1, "ui": {}},
        "privacy": "Only cell captures are stored; source JSON, titles, conversation bodies, and local paths are excluded.",
    }
    renderer = load_reference(reference)
    renderer.path = reference
    sanitize_reference(renderer)
    values = _fixture_values(renderer)
    return {
        "manifest.json": manifest,
        "cells-140.json": {**_compact_capture(capture(reference, 140)),
                           "list_states": _compact_states(list_captures(reference, 140))},
        "cells-168.json": {**_compact_capture(capture(reference, 168)),
                           "list_states": _compact_states(list_captures(reference, 168))},
        "view_model.json": {"schema": 1, "fixture": "synthetic-core", "values": values},
        "source.json": {"schema": 1, "fixture": "synthetic-core", "view_model": "view_model.json"},
        "transitions.json": {"schema": 1, "cases": []},
        "README.md": {
            "schema": 1,
            "text": (
                "These fixtures use width-preserving synthetic text in place of freeform source content. "
                "cells-140.json and cells-168.json store each cell row as exact "
                "[glyph, colour_key, tint, count] runs; the oracle expands them and compares every cell "
                "and ordered hit array for every state. source.json points to view_model.json, which "
                "contains the same source values; test helpers derive the source wrapper from it.\n\n"
                "PEERS is checked structurally because the approved reference hand-places repository "
                "frames and link lanes. The check asserts visible link lanes, every graph node and edge "
                "kind, non-empty full-state hits, fewer hits after selecting f5, the selected link count, "
                "and its selection tint. It covers PEERS-pick-f5 at both 140 and 168 columns.\n\n"
                "Repository-only test runs compare the committed goldens. Running freeze_explorer.py "
                "--check with the external approved reference directory re-verifies provenance using "
                "the input hashes in manifest.json. The reference renderer and PNGs are intentionally "
                "external; no private absolute path is stored here. default-once.json is captured "
                "separately from the archived base CLI."
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference-dir", type=Path, required=True)
    parser.add_argument("--renderer", type=Path, required=True)
    parser.add_argument("--viewer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    reference = args.reference_dir.resolve()
    if args.renderer.resolve() != (reference / "mock13.py").resolve():
        parser.error("renderer must be the approved mock13.py in reference-dir")
    if args.viewer.resolve() != (reference / "view.py").resolve():
        parser.error("viewer must be the approved view.py in reference-dir")
    generated = build(reference, args.output)
    mismatches = []
    for name, value in generated.items():
        target = args.output / name
        indent = None if name in ("cells-140.json", "cells-168.json") else 2
        data = (json.dumps(value, ensure_ascii=False, indent=indent, separators=(",", ":") if indent is None else None) + "\n").encode() if name.endswith(".json") else (value["text"] + "\n").encode()
        if args.check:
            if not target.is_file() or target.read_bytes() != data:
                mismatches.append(name)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    if mismatches:
        print("fixture mismatch: " + ", ".join(mismatches), file=sys.stderr)
        return 1
    baseline_path = args.output / "default-once.json"
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        if baseline.get("baseline_commit") != "00b4ba0168efc35eaf74603b8c9364f259c5adc4" or \
                set(baseline.get("outputs", {})) != {"80", "100", "140", "168"}:
            raise ValueError("missing base commit or width outputs")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"base once fixture invalid: {exc}", file=sys.stderr)
        return 1
    manifest = generated["manifest.json"]
    print(f"reference frozen: {len(generated)} files; {len(manifest['reference_files'])} inputs; {manifest['reference_png_count']} PNGs recorded; base once fixture valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
