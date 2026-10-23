#!/usr/bin/env python3
"""Exercise Fleet mouse input in an isolated xterm PTY with synthetic sessions.

Run once with FLEET_MOUSE unset and once with an explicit opt-out value. The
parent owns and terminates only the fixture viewer it starts; the child never
collects host sessions or calls a session-control operation.
"""
import argparse
import curses
import datetime as dt
import fcntl
import json
import os
import platform
import pty
import selectors
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = TOOLS_DIR.parent
sys.path.insert(0, str(TOOLS_DIR))


def _utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _write_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8")
    os.replace(temp, path)


def _proc_start(pid):
    try:
        stat = Path("/proc/%d/stat" % pid).read_text(encoding="ascii")
        fields = stat[stat.rfind(")") + 2:].split()
        return fields[19]
    except (OSError, IndexError):
        return None


def _child_main(evidence_dir, explorer=False):
    from fleet import control, gitinfo, render
    from fleet.model import Session
    from fleet.refresh import LiveSnapshot

    evidence_dir = Path(evidence_dir)
    render.set_explorer_mode(explorer)
    event_path = evidence_dir / "events.jsonl"
    state_path = evidence_dir / "state.json"
    event_path.write_text("", encoding="utf-8")
    cfg = {"requested_mask": None, "supported_mask": None, "mouseinterval": None}

    def event(kind, **fields):
        row = {"utc": _utc(), "monotonic": time.monotonic(), "kind": kind}
        row.update(fields)
        with event_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    # The fixture collector contains only synthetic identities and performs no
    # host discovery, provider access, or registry I/O.
    sessions = [Session(harness="codex", pid=61001 + i,
                        proc_start="pty-fixture-start-%02d" % i,
                        cwd="/tmp/fleet-pty-fixture", slug="fixture-%02d" % i,
                        title="synthetic session %02d" % i,
                        session_id="pty-fixture-sid-%02d" % i,
                        liveness="working", elapsed_min=i + 1)
                for i in range(24)]

    def collect_all(**_kwargs):
        return LiveSnapshot(sessions=list(sessions))

    def forbidden(name):
        def reject(*args, **kwargs):
            event("blocked-side-effect", operation=name)
            raise AssertionError("fixture viewer attempted %s" % name)
        return reject

    # Block both Fleet control entry points and any direct process signal in
    # this fixture process. The parent process is separate and only signals its
    # own child if timeout cleanup is needed.
    control.is_excluded = lambda _pid: False
    control.kill_target = forbidden("control.kill_target")
    control.close_registry_row = forbidden("control.close_registry_row")
    os.kill = forbidden("os.kill")
    render._collect_memory = lambda: None
    render._collect_governor = lambda: None
    gitinfo.enrich_entities = lambda *_args, **_kwargs: None
    render._API_DISABLED = True
    render._REPO_FOLD = {}
    render._ROUTE_FOLD = {}
    render._OFFSET = 0

    real_mousemask = getattr(curses, "mousemask", None)
    real_mouseinterval = getattr(curses, "mouseinterval", None)

    if real_mousemask is not None:
        def capture_mousemask(mask):
            result = real_mousemask(mask)
            supported = result[0] if isinstance(result, tuple) else result
            cfg["requested_mask"] = int(mask)
            cfg["supported_mask"] = int(supported or 0)
            event("mousemask", requested_mask=cfg["requested_mask"],
                  supported_mask=cfg["supported_mask"])
            return result
        curses.mousemask = capture_mousemask
    if real_mouseinterval is not None:
        def capture_mouseinterval(interval):
            prior = real_mouseinterval(interval)
            cfg["mouseinterval"] = int(interval)
            event("mouseinterval", requested_interval=int(interval), prior=prior)
            return prior
        curses.mouseinterval = capture_mouseinterval

    original_draw = render._draw
    frame_count = [0]

    def capture_draw(stdscr, *args, **kwargs):
        result = original_draw(stdscr, *args, **kwargs)
        frame_count[0] += 1
        repo_rows = [{"row": int(row), "repo_key": value["repo_key"],
                      "folded": bool(value["folded"]),
                      "spans": [[int(a), int(b)] for a, b in render._REPO_FOLD_SPANS.get(row, [])]}
                     for row, value in render._REPO_FOLD_ROWS.items()]
        if explorer:
            click_rows = [{"row": int(row),
                           "actions": [list(hit.action) for hit in hits]}
                          for row, hits in render._CLICK_ROWS.items()]
        else:
            click_rows = [{"row": int(row), "pid": int(value.get("pid") or 0),
                           "proc_start": value.get("proc_start"),
                           "spans": [[int(a), int(b)] for a, b in render._CLICK_SPANS.get(row, [])]}
                          for row, value in render._CLICK_ROWS.items()]
        h, w = stdscr.getmaxyx()
        state = {"utc": _utc(), "monotonic": time.monotonic(),
                 "frame": frame_count[0], "size": [h, w],
                 "hit_valid": bool(render._HIT_VALID),
                 "offset": int(render._OFFSET),
                 "body_top": int(render._CURRENT_BODY_TOP),
                 "body_height": int(render._CURRENT_BODY_H),
                 "body_count": int(render._CURRENT_BODY_COUNT),
                 "repo_folds": dict(render._REPO_FOLD),
                 "repo_rows": repo_rows, "click_rows": click_rows,
                 "selected": list(render._CURSOR_ID) if render._CURSOR_ID else None,
                 "select_mode": bool(render._SELECT_MODE),
                 "prompt_stage": (render._PROMPT or {}).get("stage"),
                 "mouse_config": dict(cfg)}
        if explorer:
            frame = render._EXPLORER_FRAME
            explorer_state = render._EXPLORER_STATE
            state["explorer"] = {
                "title": getattr(frame, "title", ""),
                "regions": ({"screen": [0, 0, int(w), int(h)]} if frame is not None else {}),
                "hit_count": len(getattr(frame, "hits", ()) or ()),
                "state": ({"view": explorer_state.view,
                           "scope": explorer_state.scope,
                           "opened": explorer_state.opened,
                           "usage_open": explorer_state.usage_open,
                           "folds": [list(item) for item in explorer_state.folds],
                           "scroll": [list(item) for item in explorer_state.scroll]}
                          if explorer_state is not None else None),
            }
        _write_json(state_path, state)
        return result

    render._draw = capture_draw
    original_getmouse = render._getmouse_event

    def capture_getmouse():
        value = original_getmouse()
        event("native-mouse-read", x=value[0], y=value[1], bstate=int(value[2] or 0))
        return value

    render._getmouse_event = capture_getmouse
    original_input_read = render._CursesInput.read

    def capture_input_read(input_reader, stdscr, *args, **kwargs):
        result = original_input_read(input_reader, stdscr, *args, **kwargs)
        _ch, mouse_event = result
        if mouse_event is not None:
            x, y, bstate = mouse_event
            event("decoded-mouse-read", x=x, y=y, bstate=bstate)
        elif _ch != curses.ERR:
            key = render._explorer_key_name(
                _ch, searching=bool(getattr(render._EXPLORER_STATE, "searching", False)))
            event("decoded-key-read", code=int(_ch), key=key)
        return result

    render._CursesInput.read = capture_input_read
    original_mouse_event = render._handle_mouse_event

    def capture_mouse_event(x, y, bstate):
        before = {"hit_valid": bool(render._HIT_VALID),
                  "offset": int(render._OFFSET), "repo_folds": dict(render._REPO_FOLD),
                  "selected": list(render._CURSOR_ID) if render._CURSOR_ID else None,
                  "prompt_stage": (render._PROMPT or {}).get("stage")}
        original_mouse_event(x, y, bstate)
        after = {"hit_valid": bool(render._HIT_VALID),
                 "offset": int(render._OFFSET), "repo_folds": dict(render._REPO_FOLD),
                 "selected": list(render._CURSOR_ID) if render._CURSOR_ID else None,
                 "prompt_stage": (render._PROMPT or {}).get("stage")}
        event("mouse-action", x=int(x), y=int(y), bstate=int(bstate),
              before=before, after=after)

    render._handle_mouse_event = capture_mouse_event
    event("fixture-start", python=sys.version, platform=platform.platform(),
          ncurses=str(getattr(curses, "ncurses_version", "unavailable")),
          term=os.environ.get("TERM"), fleet_mouse=os.environ.get("FLEET_MOUSE"),
          synthetic_sessions=[{"pid": s.pid, "proc_start": s.proc_start,
                               "session_id": s.session_id} for s in sessions])
    try:
        curses.wrapper(render._loop, collect_all, None, "both", 20.0)
        event("fixture-exit", status="normal")
        return 0
    except BaseException as exc:
        event("fixture-exit", status="exception", error=repr(exc))
        raise


class PtyRun:
    def __init__(self, evidence_dir, *, cols=120, rows=40, live_capture=False):
        self.evidence_dir = Path(evidence_dir).resolve()
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.cols, self.rows = max(40, int(cols)), max(12, int(rows))
        self.live_capture = bool(live_capture)
        self.mode = ("live-capture" if self.live_capture else
                     "default" if "FLEET_MOUSE" not in os.environ else "opt-out")
        self.explorer = False
        self.transcript = bytearray()
        self.inputs = []
        self.assertions = []
        self.started_mono = time.monotonic()
        self.started_utc = _utc()
        self.child = None
        self.master = None
        self.selector = None

    def check(self, label, condition, detail=None):
        self.assertions.append({"label": label, "passed": bool(condition),
                                "detail": detail})
        if not condition:
            raise AssertionError("%s: %s" % (label, detail))

    def state(self):
        try:
            return json.loads((self.evidence_dir / "state.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def drain(self, duration=0.04):
        end = time.monotonic() + duration
        while time.monotonic() < end:
            if self.child is not None and self.child.poll() is not None:
                break
            events = self.selector.select(min(0.03, max(0, end - time.monotonic())))
            for key, _mask in events:
                try:
                    data = os.read(key.fd, 65536)
                except OSError:
                    data = b""
                if data:
                    self.transcript.extend(data)

    def wait_for(self, description, predicate, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            state = self.state()
            if state and predicate(state):
                return state
            if self.child is not None and self.child.poll() is not None:
                raise AssertionError("viewer exited before %s (status %s)" %
                                     (description, self.child.returncode))
            self.drain(0.04)
        raise AssertionError("timed out waiting for %s; last state=%r" %
                             (description, self.state()))

    def send(self, data, label):
        self.inputs.append({"utc": _utc(), "monotonic": time.monotonic(),
                            "label": label, "hex": data.hex(), "length": len(data)})
        os.write(self.master, data)

    def observe_without_followup(self, label, data, duration=1.5, min_frames=5):
        before = self.state() or {}
        self.send(data, label)
        started = time.monotonic()
        deadline = started + duration
        samples = []
        while time.monotonic() < deadline and self.child.poll() is None:
            self.drain(min(0.025, max(0.001, deadline - time.monotonic())))
            state = self.state()
            if state:
                samples.append({"at_seconds": round(time.monotonic() - started, 4),
                                "frame": state.get("frame")})
        elapsed = time.monotonic() - started
        after = self.state() or {}
        frames = sorted({row["frame"] for row in samples if row.get("frame") is not None})
        frame_delta = after.get("frame", 0) - before.get("frame", 0)
        alive = self.child.poll() is None
        self.check(label + " keeps frame cadence without follow-up input",
                   alive and elapsed >= duration and frame_delta >= min_frames,
                   {"input_hex": data.hex(), "before_frame": before.get("frame"),
                    "after_frame": after.get("frame"), "frame_delta": frame_delta,
                    "sampled_distinct_frames": frames,
                    "sample_count": len(samples), "elapsed_seconds": round(elapsed, 4),
                    "follow_up_sent_before_deadline": False,
                    "child_alive_at_deadline": alive})
        return after

    def observe_malformed_inert(self, label, data, duration=0.7):
        before = self.state() or {}
        before_events = self.read_events()
        before_decoded = sum(row.get("kind") == "decoded-mouse-read" for row in before_events)
        before_actions = sum(row.get("kind") == "mouse-action" for row in before_events)
        before_side_effects = sum(row.get("kind") == "blocked-side-effect" for row in before_events)
        self.send(data, label)
        started = time.monotonic()
        deadline = started + duration
        while time.monotonic() < deadline and self.child.poll() is None:
            self.drain(min(0.025, max(0.001, deadline - time.monotonic())))
        after = self.state() or {}
        events = self.read_events()
        decoded = sum(row.get("kind") == "decoded-mouse-read" for row in events)
        actions = sum(row.get("kind") == "mouse-action" for row in events)
        side_effects = sum(row.get("kind") == "blocked-side-effect" for row in events)
        unchanged = all(after.get(key) == before.get(key) for key in
                        ("offset", "repo_folds", "selected", "select_mode", "prompt_stage"))
        self.check(label + " is consumed without a command or mouse action",
                   self.child.poll() is None and unchanged and decoded == before_decoded
                   and actions == before_actions and side_effects == before_side_effects,
                   {"input_hex": data.hex(), "before": before, "after": after,
                    "decoded_mouse_reads": [before_decoded, decoded],
                    "mouse_actions": [before_actions, actions],
                    "blocked_side_effects": [before_side_effects, side_effects],
                    "child_alive": self.child.poll() is None,
                    "elapsed_seconds": round(time.monotonic() - started, 4)})
        return after

    def mouse(self, button, x, y, release=False):
        # xterm SGR coordinates are one-based; Fleet/ncurses report zero-based.
        suffix = "m" if release else "M"
        self.send(("\x1b[<%d;%d;%d%s" % (button, x + 1, y + 1, suffix)).encode("ascii"),
                   "mouse-release" if release else "mouse-button-%d" % button)

    def row_point(self, state, key="repo_rows", pred=None):
        for row in state.get(key, []):
            if pred is None or pred(row):
                spans = row.get("spans") or []
                if spans:
                    x0, x1 = spans[0]
                    return x0 + max(0, (x1 - x0 - 1) // 2), row["row"], row
        raise AssertionError("no painted %s hit span in state %r" % (key, state))

    def resize(self, rows, cols):
        packed = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, packed)
        os.kill(self.child.pid, signal.SIGWINCH)
        self.inputs.append({"utc": _utc(), "monotonic": time.monotonic(),
                            "label": "resize", "rows": rows, "cols": cols,
                            "signal_target_pid": self.child.pid})

    def start(self):
        # A prior invocation uses the same evidence directory. Remove its live
        # state snapshot so startup polling cannot mistake it for this child.
        (self.evidence_dir / "state.json").unlink(missing_ok=True)
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", self.rows, self.cols, 0, 0))
        child_env = dict(os.environ)
        child_env["TERM"] = os.environ.get("TERM") or "xterm-256color"
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        if self.live_capture:
            command = [sys.executable, str(TOOLS_DIR / "fleet" / "fleet.py"), "--explorer"]
            child_env["FLEET_PTY_LIVE_CAPTURE"] = "1"
        else:
            command = [sys.executable, str(Path(__file__).resolve()), "--_fixture-child",
                       "--evidence-dir", str(self.evidence_dir)]
            if self.explorer:
                command.append("--explorer")
        self.child = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                                      cwd=str(REPO_ROOT), env=child_env, close_fds=True,
                                      start_new_session=True)
        os.close(slave)
        os.set_blocking(master, False)
        self.master = master
        self.selector = selectors.DefaultSelector()
        self.selector.register(master, selectors.EVENT_READ)
        self.child_identity = {"pid": self.child.pid, "proc_start": _proc_start(self.child.pid),
                               "started_utc": _utc(), "command": command}

    def run_default(self):
        state = self.wait_for("initial painted session frame",
                              lambda s: s["hit_valid"] and s["repo_rows"] and s["click_rows"])
        # Let the first basic and detail snapshots publish and be repainted. A
        # hit map invalidated at the refresh boundary must not drive an action.
        self.drain(0.45)
        state = self.state() or state
        self.check("initial snapshot handoff settled before mouse input",
                   state["hit_valid"] and bool(state["click_rows"]), state)
        repo_x, repo_y, repo = self.row_point(state)
        self.check("default mouse enabled", bool(state["mouse_config"]["supported_mask"]),
                   state["mouse_config"])
        self.check("mouseinterval set to zero",
                   state["mouse_config"]["mouseinterval"] == 0, state["mouse_config"])

        modifier_mask = (getattr(curses, "BUTTON_SHIFT", 0)
                         | getattr(curses, "BUTTON_ALT", 0)
                         | getattr(curses, "BUTTON_CTRL", 0))
        modified_press = getattr(curses, "BUTTON1_PRESSED", 0) | modifier_mask
        modified_release = getattr(curses, "BUTTON1_RELEASED", 0) | modifier_mask
        self.mouse(28, 1, 1)
        self.mouse(28, 1, 1, release=True)
        deadline = time.monotonic() + 1.0
        decoded = []
        while time.monotonic() < deadline and self.child.poll() is None:
            decoded = [row for row in self.read_events()
                       if row.get("kind") == "decoded-mouse-read"]
            if (any(row.get("bstate") == modified_press for row in decoded)
                    and any(row.get("bstate") == modified_release for row in decoded)):
                break
            self.drain(0.025)
        self.check("real curses modifier masks survive SGR decoding",
                   modified_press != modifier_mask and modified_release != modifier_mask
                   and any(row.get("bstate") == modified_press for row in decoded)
                   and any(row.get("bstate") == modified_release for row in decoded),
                   {"button1_pressed": getattr(curses, "BUTTON1_PRESSED", None),
                    "button1_released": getattr(curses, "BUTTON1_RELEASED", None),
                    "button_shift": getattr(curses, "BUTTON_SHIFT", None),
                    "button_alt": getattr(curses, "BUTTON_ALT", None),
                    "button_ctrl": getattr(curses, "BUTTON_CTRL", None),
                    "expected_press": modified_press, "expected_release": modified_release,
                    "decoded": decoded})

        for label, payload in (
                ("malformed-q-command", b"\x1b[<qM"),
                ("malformed-selection-command", b"\x1b[<sM"),
                ("malformed-confirm-command", b"\x1b[<xM"),
                ("malformed-numeric-tail", b"\x1b[<1;q;4M"),
                ("malformed-coordinate", b"\x1b[<0;0;4M"),
                ("malformed-field-count", b"\x1b[<0;12;4;5M")):
            self.observe_malformed_inert(label, payload)
        state = self.state() or state
        click_x, click_y, _click_row = self.row_point(state, "click_rows")
        wheel_y = state["body_top"] + max(0, state["body_height"] // 2)
        for label, button, packet_x, packet_y, suffix in (
                ("reserved-button-bit", 128, click_x, click_y, "M"),
                ("reserved-modifier-button", 132, click_x, click_y, "M"),
                ("horizontal-wheel-left", 66, 2, wheel_y, "M"),
                ("horizontal-wheel-right", 67, 2, wheel_y, "M"),
                ("wheel-release", 64, 2, wheel_y, "m"),
                ("wheel-motion-combination", 96, 2, wheel_y, "M")):
            packet = ("\x1b[<%d;%d;%d%s" % (button, packet_x + 1, packet_y + 1, suffix))
            self.observe_malformed_inert(label, packet.encode("ascii"))

        self.mouse(0, repo_x, repo_y)
        self.mouse(0, repo_x, repo_y, release=True)
        folded = self.wait_for("repo title press to fold its body",
                               lambda s: bool(s["repo_folds"].get(repo["repo_key"])))
        self.check("repo fold uses painted title", bool(folded["repo_folds"].get(repo["repo_key"])),
                   folded["repo_folds"])

        x, y, _repo = self.row_point(folded)
        self.mouse(0, x, y)
        self.mouse(0, x, y, release=True)
        expanded = self.wait_for("repo title press to restore its body",
                                 lambda s: not s["repo_folds"].get(repo["repo_key"], False)
                                 and bool(s["click_rows"]))

        self.check("fixture has scrollable body",
                   expanded["body_count"] > expanded["body_height"] > 0,
                   {"body_count": expanded["body_count"],
                    "body_height": expanded["body_height"]})
        old_offset = expanded["offset"]
        wx, wy = 2, expanded["body_top"] + max(0, expanded["body_height"] // 2)
        self.mouse(65, wx, wy)
        scrolled = self.wait_for("wheel press to scroll body only",
                                 lambda s: s["offset"] > old_offset)
        self.check("wheel changed list offset", scrolled["offset"] > old_offset,
                   {"before": old_offset, "after": scrolled["offset"]})

        down_offset = scrolled["offset"]
        self.mouse(64, wx, wy)
        scrolled_up = self.wait_for("wheel-up press to reverse body scroll",
                                   lambda s: s["offset"] < down_offset)
        self.check("wheel-up changed list offset", scrolled_up["offset"] < down_offset,
                   {"before": down_offset, "after": scrolled_up["offset"]})

        x, y, clicked = self.row_point(scrolled_up, "click_rows")
        self.mouse(0, x, y)
        self.mouse(0, x, y, release=True)
        selected = self.wait_for("session press to select synthetic identity",
                                 lambda s: s["select_mode"] and s["selected"] is not None)
        selected_id = selected["selected"]
        self.check("click selected a synthetic identity",
                   selected_id == [clicked["pid"], clicked["proc_start"]],
                   {"selected": selected_id, "painted_row": clicked})
        x, y, _clicked = self.row_point(selected, "click_rows",
                                        lambda row: row["pid"] == clicked["pid"])
        self.mouse(0, x, y)
        self.mouse(0, x, y, release=True)
        prompt = self.wait_for("session re-click to request first confirmation",
                               lambda s: s["prompt_stage"] == "confirm")
        self.check("re-click reaches only first confirmation",
                   prompt["prompt_stage"] == "confirm", prompt["prompt_stage"])
        self.send(b"n", "cancel-first-confirmation")
        self.wait_for("confirmation cancellation", lambda s: s["prompt_stage"] is None)
        self.send(b"\x1b", "leave-selection-mode")
        self.wait_for("selection mode exit", lambda s: not s["select_mode"])
        self.send(b"s", "keyboard-enter-selection")
        keyboard_select = self.wait_for("keyboard selection fallback",
                                        lambda s: s["select_mode"] and s["selected"] is not None)
        before_key = keyboard_select["selected"]
        self.send(b"j", "keyboard-next-session")
        moved = self.wait_for("keyboard cursor movement",
                              lambda s: s["selected"] is not None
                              and s["selected"] != before_key)
        self.check("keyboard fallback moves selection", moved["selected"] != before_key,
                   {"before": before_key, "after": moved["selected"]})

        before_resize = moved["size"]
        self.resize(18, 80)
        resized = self.wait_for("PTY resize to reach the next painted frame",
                                lambda s: s["size"] == [18, 80])
        self.check("resize updates painted geometry", resized["size"] != before_resize,
                   {"before": before_resize, "after": resized["size"]})
        frames_before_partial = resized["frame"]
        self.send(b"\x1b[", "incomplete-escape-mouse-prefix")
        self.drain(0.35)
        partial_state = self.state() or {}
        self.check("incomplete escape input does not stop refresh",
                   partial_state.get("frame", 0) > frames_before_partial,
                   {"before": frames_before_partial, "after": partial_state.get("frame")})
        self.observe_without_followup(
            "incomplete-sgr-coordinates", bytes.fromhex("1b5b3c303b31323b"))
        self.observe_without_followup("malformed-sgr-discard-expires", b"\x1b[<q")
        for label, payload in (
                ("incomplete-sgr-prefix", b"\x1b[<"),
                ("focus-in-complete", b"\x1b[I"),
                ("focus-out-complete", b"\x1b[O"),
                ("incomplete-focus-prefix", b"\x1b[")):
            self.observe_without_followup(label, payload)

        events = self.read_events()
        decoded = [row for row in events if row.get("kind") == "decoded-mouse-read"]
        pressed = [row for row in decoded if row.get("bstate", 0) &
                   getattr(curses, "BUTTON1_PRESSED", 0)]
        self.check("incremental parser delivered primary press events", len(pressed) >= 4,
                   {"decoded_mouse_events": decoded,
                    "button1_pressed": getattr(curses, "BUTTON1_PRESSED", None),
                    "button1_clicked": getattr(curses, "BUTTON1_CLICKED", None)})
        click_actions = [row for row in events if row.get("kind") == "mouse-action"
                         and row.get("bstate", 0) & getattr(curses, "BUTTON1_PRESSED", 0)]
        self.check("one press maps to one primary action", len(click_actions) == len(pressed),
                   {"press_events": len(pressed), "primary_actions": len(click_actions)})
        side_effects = [row for row in events if row.get("kind") == "blocked-side-effect"]
        self.check("no session signal or registry action", not side_effects, side_effects)

    def run_opt_out(self):
        state = self.wait_for("initial painted opt-out frame",
                              lambda s: s["hit_valid"] and s["repo_rows"] and s["click_rows"])
        self.drain(0.45)
        state = self.state() or state
        cfg = state["mouse_config"]
        self.check("explicit opt-out requested no mouse mask",
                   cfg["requested_mask"] is None and cfg["supported_mask"] is None, cfg)
        self.check("explicit opt-out preserves expanded default",
                   not any(state["repo_folds"].values()), state["repo_folds"])
        for label, payload in (
                ("opt-out-malformed-q-command", b"\x1b[<qM"),
                ("opt-out-malformed-selection-command", b"\x1b[<sM"),
                ("opt-out-malformed-confirm-command", b"\x1b[<xM"),
                ("opt-out-malformed-numeric-tail", b"\x1b[<1;q;4M"),
                ("opt-out-malformed-coordinate", b"\x1b[<0;0;4M")):
            self.observe_malformed_inert(label, payload)
        state = self.state() or state
        click_x, click_y, _click_row = self.row_point(state, "click_rows")
        wheel_y = state["body_top"] + max(0, state["body_height"] // 2)
        for label, button, x, y, suffix in (
                ("opt-out-reserved-button-bit", 128, click_x, click_y, "M"),
                ("opt-out-horizontal-wheel", 66, 2, wheel_y, "M"),
                ("opt-out-wheel-release", 64, 2, wheel_y, "m")):
            packet = ("\x1b[<%d;%d;%d%s" % (button, x + 1, y + 1, suffix)).encode("ascii")
            self.observe_malformed_inert(label, packet)
        repo_key = state["repo_rows"][0]["repo_key"]
        x, y, _repo = self.row_point(state)
        before_offset = state["offset"]
        before_frame = state["frame"]
        self.mouse(0, x, y)
        self.mouse(0, x, y, release=True)
        self.mouse(65, 2, state["body_top"] + max(0, state["body_height"] // 2))
        self.drain(0.45)
        after_mouse = self.state() or {}
        self.check("opt-out mouse bytes do not fold or scroll",
                   not after_mouse.get("repo_folds", {}).get(repo_key, False)
                   and after_mouse.get("offset") == before_offset,
                   {"before_offset": before_offset, "after": after_mouse})
        self.send(b" ", "keyboard-repo-fold-fallback")
        keyboard_fold = self.wait_for("keyboard fold fallback while mouse is opted out",
                                      lambda s: bool(s["repo_folds"].get(repo_key)))
        self.check("keyboard fold works under opt-out",
                   bool(keyboard_fold["repo_folds"].get(repo_key)), keyboard_fold["repo_folds"])
        self.send(b" ", "keyboard-repo-unfold-fallback")
        keyboard_unfold = self.wait_for("keyboard unfold fallback under opt-out",
                                        lambda s: not s["repo_folds"].get(repo_key, False)
                                        and bool(s["click_rows"]))
        self.check("keyboard unfold restores fixture rows", bool(keyboard_unfold["click_rows"]),
                   len(keyboard_unfold["click_rows"]))
        self.send(b"s", "keyboard-enter-selection")
        selected = self.wait_for("keyboard selection fallback under opt-out",
                                 lambda s: s["select_mode"] and s["selected"] is not None)
        before_key = selected["selected"]
        self.send(b"j", "keyboard-next-session")
        moved = self.wait_for("keyboard cursor movement under opt-out",
                              lambda s: s["selected"] is not None and s["selected"] != before_key)
        self.check("keyboard cursor remains available under opt-out",
                   moved["selected"] != before_key,
                   {"before": before_key, "after": moved["selected"]})
        self.resize(18, 80)
        resized = self.wait_for("opt-out PTY resize reaches the next painted frame",
                                lambda s: s["size"] == [18, 80])
        self.check("opt-out resize updates painted geometry",
                   resized["size"] == [18, 80], resized["size"])
        frames_before_partial = resized["frame"]
        self.send(b"\x1b[", "incomplete-escape-mouse-prefix")
        self.drain(0.35)
        partial_state = self.state() or {}
        self.check("opt-out partial input keeps refresh advancing",
                   partial_state.get("frame", 0) > frames_before_partial,
                   {"before": frames_before_partial, "after": partial_state.get("frame")})
        self.observe_without_followup(
            "opt-out-incomplete-sgr-coordinates", bytes.fromhex("1b5b3c303b31323b"))
        self.observe_without_followup("opt-out-malformed-sgr-discard-expires", b"\x1b[<q")
        for label, payload in (
                ("opt-out-incomplete-sgr-prefix", b"\x1b[<"),
                ("opt-out-focus-in-complete", b"\x1b[I"),
                ("opt-out-focus-out-complete", b"\x1b[O"),
                ("opt-out-incomplete-navigation-prefix", b"\x1b[")):
            self.observe_without_followup(label, payload)
        self.check("opt-out did not invoke session controls",
                   not any(row.get("kind") == "blocked-side-effect"
                           for row in self.read_events()), None)

    def read_events(self):
        path = self.evidence_dir / "events.jsonl"
        try:
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                    if line]
        except OSError:
            return []

    def finish(self):
        status = None
        if self.child is not None and self.child.poll() is None:
            try:
                self.send(b"q", "quit-fixture-viewer")
                end = time.monotonic() + 3.0
                while self.child.poll() is None and time.monotonic() < end:
                    self.drain(0.05)
            except OSError:
                pass
        if self.child is not None and self.child.poll() is None:
            # This is the runner's own isolated fixture child, never a Fleet target.
            self.child.terminate()
            try:
                self.child.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=1.0)
        if self.child is not None:
            status = self.child.poll()
        self.drain(0.05)
        try:
            (self.evidence_dir / "transcript.raw").write_bytes(bytes(self.transcript))
        except OSError as exc:
            self.assertions.append({"label": "transcript persisted", "passed": False,
                                    "detail": repr(exc)})
        result = {"mode": self.mode, "started_utc": self.started_utc,
                  "finished_utc": _utc(), "elapsed_seconds": time.monotonic() - self.started_mono,
                  "python": sys.version, "platform": platform.platform(),
                  "ncurses": str(getattr(curses, "ncurses_version", "unavailable")),
                  "TERM": os.environ.get("TERM"),
                  "FLEET_MOUSE": os.environ.get("FLEET_MOUSE", "<unset>"),
                  "child": getattr(self, "child_identity", None),
                  "returncode": status, "initial_size": [self.rows, self.cols],
                  "resize_sizes": [[row, col] for row, col in
                                   ((x.get("rows"), x.get("cols")) for x in self.inputs)
                                   if row is not None and col is not None],
                  "source": "host" if self.live_capture else "synthetic-fixture",
                  "inputs": self.inputs, "assertions": self.assertions,
                  "passed": all(x["passed"] for x in self.assertions),
                  "terminal_limitations": [
                      "This is an xterm-256color PTY with SGR mouse packets; it does not emulate a physical mouse or every terminal emulator.",
                      "Native bstate values and supported mouse mask are captured from this Python/ncurses build."]}
        _write_json(self.evidence_dir / "run.json", result)
        if self.selector:
            self.selector.close()
        if self.master is not None:
            os.close(self.master)
        return result

    def execute(self):
        self.start()
        try:
            if self.live_capture:
                self.run_live_capture()
            elif self.explorer:
                self.run_explorer()
            elif self.mode == "default":
                self.run_default()
            else:
                self.run_opt_out()
        except Exception as exc:
            self.assertions.append({"label": "runner completed", "passed": False,
                                    "detail": repr(exc)})
        finally:
            result = self.finish()
        return result

    def run_live_capture(self):
        """Capture the real read-only Fleet explorer for evidence; the child is ours to stop."""
        self.drain(3.0)
        alive = self.child.poll() is None
        raw = bytes(self.transcript)
        self.check("host explorer produced a colored terminal frame",
                   alive and b"\x1b[" in raw and len(raw) > 256,
                   {"alive": alive, "bytes": len(raw), "cols": self.cols, "rows": self.rows})
        (self.evidence_dir / "capture.ansi").write_bytes(raw)
        self.send(b"q", "quit-owned-live-capture")
        self.drain(0.25)

    def run_explorer(self):
        state = self.wait_for("initial explorer frame",
                              lambda s: s.get("hit_valid") and s.get("explorer", {}).get("hit_count"))
        initial = state["explorer"]["state"]
        self.check("explorer frame has screen bounds and view state",
                   bool(state["explorer"].get("regions")) and bool(initial.get("view")),
                   state.get("explorer"))

        # Split one CSI arrow across PTY writes around the decoder's ESC deadline.
        before_view = initial.get("view")
        self.send(b"\x1b", "split-arrow-prefix")
        self.drain(0.008)
        self.send(b"[B", "split-arrow-tail")
        down = self.wait_for("split CSI arrow is decoded as one down key",
                             lambda _s: any(row.get("kind") == "decoded-key-read"
                                            and row.get("key") == "down"
                                            for row in self.read_events()))
        decoded = [row for row in self.read_events() if row.get("kind") == "decoded-key-read"]
        self.check("split escape bytes decode to one arrow event",
                   sum(row.get("key") == "down" for row in decoded) == 1,
                   {"view_before": before_view,
                    "view_after": down["explorer"]["state"].get("view"),
                    "decoded": decoded[-3:]})

        # Multiple complete inputs in one write remain ordered and each reaches the live loop.
        start_index = len(self.read_events())
        self.send(b"\tu", "batched-tab-usage")
        batched = self.wait_for("both batched keys reach the input loop",
                                lambda _s: len([row for row in self.read_events()[start_index:]
                                                if row.get("kind") == "decoded-key-read"
                                                and row.get("key") in ("tab", "u")]) >= 2)
        keys = [row.get("key") for row in self.read_events()[start_index:]
                if row.get("kind") == "decoded-key-read"]
        self.check("batched tab and usage keys preserve order", keys[:2] == ["tab", "u"], keys)
        self.check("batched tab and usage keys both change explorer state",
                   batched["explorer"]["state"]["view"] != initial["view"] and
                   batched["explorer"]["state"]["usage_open"] != initial["usage_open"],
                   batched["explorer"]["state"])

        before_f = batched["explorer"]["state"]
        self.send(b"u", "toggle-explorer-usage")
        changed = self.wait_for("usage key reaches reducer",
                                lambda s: s.get("explorer", {}).get("state", {}).get("usage_open")
                                != before_f.get("usage_open"))
        self.check("usage toggles in live explorer",
                   changed["explorer"]["state"]["usage_open"] != before_f["usage_open"],
                   changed["explorer"]["state"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--_fixture-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--explorer", action="store_true", help="exercise the explorer in the PTY")
    parser.add_argument("--live-capture", action="store_true",
                        help="capture the host-backed explorer in the PTY")
    parser.add_argument("--cols", type=int, default=120)
    parser.add_argument("--rows", type=int, default=40)
    args = parser.parse_args()
    if args._fixture_child:
        return _child_main(args.evidence_dir, explorer=args.explorer)
    run = PtyRun(args.evidence_dir, cols=args.cols, rows=args.rows,
                 live_capture=args.live_capture)
    run.explorer = args.explorer
    result = run.execute()
    print(json.dumps({"mode": result["mode"], "passed": result["passed"],
                      "returncode": result["returncode"],
                      "evidence_dir": str(run.evidence_dir)}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
