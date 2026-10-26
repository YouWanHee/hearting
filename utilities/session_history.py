#!/usr/bin/env python3
"""Open a predecessor transcript read-only beside the current terminal pane."""
from __future__ import annotations

import argparse
import contextlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import conversation_reader
import session_tidy
from terminal_surface import HerdrSurface, Near, open_pane


def _identity(args):
    resolved = session_tidy.resolve_caller(
        args.harness, args.session_id, os.getcwd(), project=False)
    if resolved:
        seat, harness, sid = resolved
        if harness in session_tidy.HARNESSES and sid and not sid.startswith("unknown-"):
            seat = session_tidy.session_seat(seat, harness, sid)
            if seat.kind == "pane":
                return seat, seat.pane, seat.socket_path
            return seat, "", ""
    if args.session_id:
        return None, "", ""

    # A keybinding without any native identity keeps its read-only active-pane
    # behavior. It cannot override a native caller selected above.
    pane = (os.environ.get("HERDR_ACTIVE_PANE_ID") or os.environ.get("HERDR_PANE_ID") or "").strip()
    socket = os.environ.get("HERDR_SOCKET_PATH", "")
    if pane and socket:
        endpoint = session_tidy._endpoint_scope({"HERDR_SOCKET_PATH": socket})
        seat = session_tidy._pane_seat_of(pane, endpoint=endpoint, socket_path=socket)
        return seat, pane, socket
    return None, "", ""


def _render(entry):
    result = conversation_reader.read_conversation(entry)
    name = f"{entry.get('harness', 'unknown')} {str(entry.get('sid', ''))[:8]}"
    label = entry.get("_label", "이전 대화")
    if result.status != "available":
        return f"{label} · {name} · 원문 {result.status}: {result.reason or '읽을 수 없습니다'}", result
    header = f"{label} · {name} · 읽기 전용 원문 (도구 호출은 접혀 있습니다)"
    return header + "\n\n" + (result.text or "원문 파일은 있지만 표시할 대화가 없습니다"), result


def _show(text):
    less = shutil.which("less")
    if not less:
        print(text)
        return sys.stdin.isatty()
    env = os.environ.copy()
    for key in ("LESS", "LESSOPEN", "LESSCLOSE", "LESSHISTFILE"):
        env.pop(key, None)
    env["LESSSECURE"] = "1"
    env["LESSHISTFILE"] = "-"
    proc = subprocess.run([less, "+G"], input=text, text=True, env=env, check=False)
    return proc.returncode == 0 and sys.stdin.isatty()


def _view(args):
    if args.harness and args.session_id:
        seat = session_tidy.session_seat(
            session_tidy.resolve_seat(args.harness, os.getcwd(), sid=args.session_id,
                                      project=False),
            args.harness, args.session_id)
    else:
        seat = session_tidy.Seat("pane", args.seat_key, args.pane, "codex", "",
                                 endpoint=args.endpoint, socket_path=args.socket_path)
    rows = session_tidy.seat_lineage(seat)
    return _display(rows, args.generation, args.stdout, clear_before_prompt=True)


def _entries_for_generation(rows, generation):
    if not rows:
        return [], "연결 미확인"
    current = rows[0]
    current_id = current.get("harness"), current.get("sid")
    confirmed = session_tidy._confirmed_predecessor(
        rows, current.get("harness", ""), current.get("sid", ""))
    if confirmed is None:
        if len(rows) < 2 or current.get("relation") != "chronological":
            return [], "확정된 이전 대화가 없습니다 · hearting run session_history --harness <harness> --session-id <SID>"
        confirmed = rows[1]
        first_label = "시간순 이웃 · 인계 미확인"
    else:
        first_label = "확정된 이전 대화"

    items = [dict(confirmed, _label=first_label)]
    seen = {current_id, (confirmed.get("harness"), confirmed.get("sid"))}
    relation_labels = {
        "confirmed": "확정된 이전 대화",
        "chronological": "시간순 이웃 · 인계 미확인",
        "unknown": "관계 미확인",
    }
    for index, row in enumerate(rows[1:], 1):
        ident = row.get("harness"), row.get("sid")
        if ident in seen:
            continue
        newer = rows[index - 1]
        relation = newer.get("relation")
        predecessor = newer.get("predecessor")
        if relation == "confirmed" and isinstance(predecessor, dict):
            if (predecessor.get("harness"), predecessor.get("sid")) != ident:
                relation = "unknown"
        items.append(dict(row, _label=relation_labels.get(relation, "관계 미확인")))
        seen.add(ident)
    if generation < 1 or generation > len(items):
        return [], f"세대 범위 밖입니다 (1–{len(items)})"
    return items, ""


def _display(rows, generation, stdout=False, clear_before_prompt=False):
    entries, reason = _entries_for_generation(rows, generation)
    if not entries:
        if stdout:
            print(reason)
        else:
            _show(reason)
        return 0
    index = generation - 1
    boundary_message = ""
    while True:
        output, _ = _render(entries[index])
        if stdout:
            print(output)
        else:
            menu = _show(output)
            if not menu:
                return 0
            if clear_before_prompt:
                print("\x1b[2J\x1b[H", end="")
            try:
                prompt = "p 이전 세대 · n 최근 세대 · q 종료"
                if boundary_message:
                    prompt += " · " + boundary_message
                choice = input(prompt + ": ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return 0
            boundary_message = ""
            if choice == "q":
                return 0
            if choice == "p" and index + 1 < len(entries):
                index += 1
                continue
            if choice == "n" and index > 0:
                index -= 1
                continue
            if choice == "p":
                boundary_message = "더 앞 세대가 없습니다"
            elif choice == "n":
                boundary_message = "더 최근 세대가 없습니다"
            continue
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="hearting run session_history")
    parser.add_argument("--in-pane", action="store_true")
    parser.add_argument("--stdout", action="store_true")
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument("--harness")
    parser.add_argument("--session-id")
    parser.add_argument("--seat-key", default="")
    parser.add_argument("--pane", default="")
    parser.add_argument("--endpoint", default="")
    parser.add_argument("--socket-path", default="")
    args = parser.parse_args(argv)
    if args.in_pane:
        return _view(args)
    seat, pane, socket = _identity(args)
    if seat is None:
        print("연결 미확인 · hearting run session_history --harness <harness> --session-id <SID>")
        return 0
    if pane:
        endpoint = seat.endpoint
        argv = ["exec", sys.executable, str(Path(__file__).resolve()), "--in-pane", "--seat-key", seat.key,
                "--pane", pane, "--endpoint", endpoint, "--socket-path", socket,
                "--generation", str(args.generation)]
        if seat.harness and seat.sid:
            argv += ["--harness", seat.harness, "--session-id", seat.sid]
        active_cwd = os.environ.get("HERDR_ACTIVE_PANE_CWD") or os.getcwd()
        def runner(command):
            env = os.environ.copy()
            env["HERDR_SOCKET_PATH"] = socket
            return subprocess.run(command, capture_output=True, text=True, timeout=15, env=env, check=False)
        near = Near(HerdrSurface(runner=runner), pane=pane)
        result = open_pane(argv, title="이전 대화", near=near, cwd=active_cwd,
                           env={"HERDR_SOCKET_PATH": socket})
        if result.pane:
            if result.placement == "split":
                layout = near.surface.layout(pane)
                panes = layout.get("panes", []) if isinstance(layout, dict) else []
                rects = {row.get("pane_id"): row.get("rect") for row in panes
                         if isinstance(row, dict)}
                origin_rect, opened_rect = rects.get(pane), rects.get(result.pane)
                if (isinstance(origin_rect, dict) and isinstance(opened_rect, dict)
                        and all(type(rect.get(key)) is int for rect in (origin_rect, opened_rect)
                                for key in ("x", "y", "width", "height"))):
                    ox = origin_rect["x"] + origin_rect["width"] / 2
                    oy = origin_rect["y"] + origin_rect["height"] / 2
                    nx = opened_rect["x"] + opened_rect["width"] / 2
                    ny = opened_rect["y"] + opened_rect["height"] / 2
                    direction = ("right" if nx > ox else "left") if abs(nx - ox) >= abs(ny - oy) \
                        else ("down" if ny > oy else "up")
                    with contextlib.suppress(OSError, subprocess.SubprocessError):
                        runner(["herdr", "pane", "focus", "--pane", pane,
                                "--direction", direction])
            return 0
        print("옆 창을 열지 못했습니다: " + ", ".join(result.notes or [result.placement]))
    rows = session_tidy.seat_lineage(seat)
    return _display(rows, args.generation, args.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
