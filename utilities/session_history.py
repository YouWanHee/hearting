#!/usr/bin/env python3
"""Open a predecessor transcript read-only beside the current terminal pane."""
from __future__ import annotations

import argparse
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
    pane = (os.environ.get("HERDR_ACTIVE_PANE_ID") or os.environ.get("HERDR_PANE_ID") or "").strip()
    socket = os.environ.get("HERDR_SOCKET_PATH", "")
    if pane and socket:
        endpoint = session_tidy._endpoint_scope({"HERDR_SOCKET_PATH": socket})
        seat = session_tidy._pane_seat_of(pane, endpoint=endpoint, socket_path=socket)
        return seat, pane, socket
    resolved = session_tidy.resolve_caller(args.harness, args.session_id, os.getcwd())
    if resolved:
        return resolved[0], "", ""
    return None, "", ""


def _entries(seat):
    rows = session_tidy.seat_lineage(seat)
    if not rows:
        return [], "연결 미확인"
    current = rows[0]
    predecessor = session_tidy._confirmed_predecessor(rows, current.get("harness", ""), current.get("sid", ""))
    if predecessor is None:
        previous = next((row for row in rows[1:] if row.get("relation") == "chronological"), None)
        if previous is None:
            return [], "확정된 이전 대화가 없습니다"
        predecessor = dict(previous)
        predecessor["_label"] = "시간순 이웃 · 인계 미확인"
    else:
        predecessor["_label"] = "확정된 이전 대화"
    seen = {(predecessor.get("harness"), predecessor.get("sid"))}
    result = [predecessor]
    for row in rows:
        ident = row.get("harness"), row.get("sid")
        if ident in seen or ident == (current.get("harness"), current.get("sid")):
            continue
        result.append(dict(row, _label="이전 세대" if row.get("relation") != "unknown" else "관계 미확인"))
        seen.add(ident)
    return result, ""


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


def _show_local(rows, generation, stdout=False):
    entries, reason = _entries_for_generation(rows, generation)
    if not entries:
        print(reason)
        return 0
    index = generation - 1
    while True:
        output, _ = _render(entries[index])
        if stdout:
            print(output)
            return 0
        menu = _show(output)
        if not sys.stdin.isatty():
            return 0
        try:
            choice = input("p 이전 세대 · n 최근 세대 · q 종료: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return 0
        if choice == "q":
            return 0
        if choice == "p" and index + 1 < len(entries):
            index += 1
        elif choice == "n" and index > 0:
            index -= 1


def _view(args):
    seat = session_tidy.Seat("pane", args.seat_key, args.pane, "codex", "",
                             endpoint=args.endpoint, socket_path=args.socket_path)
    rows = session_tidy.seat_lineage(seat)
    entries, reason = _entries_for_generation(rows, args.generation)
    if not entries:
        print(reason)
        return 0
    index = args.generation - 1
    while True:
        output, read = _render(entries[index])
        if args.stdout:
            print(output)
        else:
            menu = _show(output)
            if not menu:
                return 0
            try:
                choice = input("p 이전 세대 · n 최근 세대 · q 종료: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return 0
            if choice == "q":
                return 0
            if choice == "p" and index + 1 < len(entries):
                index += 1
                continue
            if choice == "n" and index > 0:
                index -= 1
                continue
            continue
        return 0


def _entries_for_generation(rows, generation):
    if not rows:
        return [], "연결 미확인"
    current = rows[0]
    confirmed = session_tidy._confirmed_predecessor(rows, current.get("harness", ""), current.get("sid", ""))
    if confirmed is None:
        adjacent = next((row for row in rows[1:] if row.get("relation") == "chronological"), None)
        if adjacent is None:
            return [], "확정된 이전 대화가 없습니다 · hearting run session_history --harness <harness> --session-id <SID>"
        confirmed = dict(adjacent)
        confirmed["_label"] = "시간순 이웃 · 인계 미확인"
    else:
        confirmed["_label"] = "확정된 이전 대화"
    items = [confirmed]
    identities = {(confirmed.get("harness"), confirmed.get("sid")),
                  (current.get("harness"), current.get("sid"))}
    ancestor = confirmed
    while True:
        older = session_tidy._confirmed_predecessor(
            rows, ancestor.get("harness", ""), ancestor.get("sid", ""))
        if older is None:
            break
        ident = older.get("harness"), older.get("sid")
        if ident in identities:
            break
        older["_label"] = "이전 세대"
        items.append(older)
        identities.add(ident)
        ancestor = older
    if generation < 1 or generation > len(items):
        return [], f"세대 범위 밖입니다 (1–{len(items)})"
    return items, ""


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
        argv = [sys.executable, str(Path(__file__).resolve()), "--in-pane", "--seat-key", seat.key,
                "--pane", pane, "--endpoint", endpoint, "--socket-path", socket,
                "--generation", str(args.generation)]
        active_cwd = os.environ.get("HERDR_ACTIVE_PANE_CWD") or os.getcwd()
        def runner(command):
            env = os.environ.copy()
            env["HERDR_SOCKET_PATH"] = socket
            return subprocess.run(command, capture_output=True, text=True, timeout=15, env=env, check=False)
        near = Near(HerdrSurface(runner=runner), pane=pane)
        result = open_pane(argv, title="이전 대화", near=near, cwd=active_cwd,
                           env={"HERDR_SOCKET_PATH": socket})
        if result.pane:
            return 0
        print("옆 창을 열지 못했습니다: " + ", ".join(result.notes or [result.placement]))
    rows, reason = _entries(seat)
    if not rows:
        print(reason or "이전 대화를 찾을 수 없습니다")
        return 0
    return _show_local(rows, args.generation, args.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
