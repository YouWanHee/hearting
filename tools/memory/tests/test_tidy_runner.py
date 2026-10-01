#!/usr/bin/env python3
"""session-tidy runner: enqueue, detached supervisor, registered worker, apply, handoff.

Every subprocess runs inside ``tidy_isolation`` (a ``/var/tmp`` root for HOME, XDG_*,
MEM_STORE; remote off; no worker markers).  The registered worker, the exact-identity
state check and ``peer-steward`` are replaced by fake executables that live under that root; the replacement only works
because the test also sets ``HEARTING_TIDY_TEST_ROOT`` (see
``session_tidy_runner.test_root``).  The fake worker leaves its own finisher process,
so the "nothing is left behind" check at the end of every test is a real PID check.

Worker scenarios (``scenario.json``, one entry per launcher call; ``gate`` is a file the
test creates to let the worker finish, so no assertion depends on timing):
``ok`` · ``governor`` · ``fail-start`` · ``bad-json`` · ``bad-shape`` · ``no-output`` · ``hang``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import textwrap
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "utilities"))

from tidy_isolation import isolated_env  # noqa: E402
import session_tidy as st  # noqa: E402
import session_tidy_runner as runner  # noqa: E402
from test_tidy_isolation import FIXTURES  # noqa: E402

TIDY = ROOT / "utilities" / "session_tidy.py"
MEM = ROOT / "tools" / "memory" / "mem.py"
PANE = "test:pane-a"
CHOICE_SID = "11111111-1111-4111-8111-111111111111"
DAY = 86400
WAIT = 90.0

FAKE_WORKER = textwrap.dedent('''\
    #!{python}
    import json, os, pathlib, stat, subprocess, sys
    root = pathlib.Path(os.environ["FAKE_ROOT"])
    args = sys.argv[1:]
    def arg(name):
        return args[args.index(name) + 1] if name in args else ""
    counter = root / "calls.count"
    n = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(n))
    scenario = json.loads((root / "scenario.json").read_text())
    calls = scenario.get("calls", [])
    spec = calls[n - 1] if n <= len(calls) else scenario.get("default", {{"mode": "ok"}})
    mode = spec.get("mode", "ok")
    prompt_path = pathlib.Path(arg("--prompt-file"))
    prompt = prompt_path.read_text()
    fields = {{}}
    for line in prompt.splitlines():
        for key in ("batch_id", "input (read only)", "input_digest", "output (write exactly this one file)"):
            if line.startswith(key + ":"):
                fields[key] = line.split(":", 1)[1].strip()
    input_path = pathlib.Path(fields["input (read only)"])
    saved = root / "inputs" / (str(n) + ".json")
    saved.parent.mkdir(exist_ok=True)
    saved.write_text(input_path.read_text())
    with open(root / "calls.jsonl", "a") as handle:
        handle.write(json.dumps({{
            "n": n, "mode": mode, "argv": args, "env_keys": sorted(os.environ), "cwd": os.getcwd(),
            "prompt": prompt, "prompt_mode": stat.S_IMODE(prompt_path.stat().st_mode),
            "input_mode": stat.S_IMODE(input_path.stat().st_mode), "fields": fields}}) + "\\n")
    if mode == "governor":
        print("check=failed\\nreason=model-worker-governor-denied\\nretryable=1\\nretry_after_seconds=0\\nchild_spawned=0")
        sys.exit(75)
    if mode == "fail-start":
        print("check=failed\\nreason=invalid-dispatch-worker-mode\\nchild_spawned=0")
        sys.exit(64)
    jobs = root / "jobs.log"
    attempt = "att-fake-" + str(n)
    meta = "capability=session-tidy,attempt_id=" + attempt + ",parent_attempt_id=att-other,worker_type=support"
    with open(jobs, "a") as handle:
        handle.write("2026-10-01T00:00:00Z\\topen\\t/w\\t/w\\t" + arg("--slug") + "\\t" + meta + "\\n")
    finisher = {finisher!r}
    proc = subprocess.Popen(
        [sys.executable, "-c", finisher, json.dumps(spec), fields["output (write exactly this one file)"],
         str(jobs), attempt],
        start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True)
    with open(root / "pids", "a") as handle:
        handle.write(str(proc.pid) + "\\n")
    print("check=ok")
    print("job_registry=" + str(jobs))
    print("attempt_id=" + attempt)
    print("registered=1")
    print("started=1")
    print("child_spawned=1")
    print("child_pid=" + str(proc.pid))
    print("child_pid_start=1")
    ''')

FAKE_FINISHER = textwrap.dedent('''\
    import json, os, sys, time
    spec, output, jobs, attempt = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
    mode = spec.get("mode", "ok")
    if mode == "hang":
        sys.exit(0)
    gate = spec.get("gate")
    deadline = time.time() + 120
    while gate and not os.path.exists(gate) and time.time() < deadline:
        time.sleep(0.05)
    if mode == "ok":
        doc = {"schema_version": 7, "batch_id": "the-worker-is-wrong", "input_digest": "wrong",
               "actions": spec.get("actions", [])}
        open(output, "w").write(json.dumps(doc))
    elif mode == "bad-json":
        open(output, "w").write("{not json at all")
    elif mode == "bad-shape":
        open(output, "w").write(json.dumps({"actions": "x"}))
    with open(jobs, "a") as handle:
        handle.write("2026-10-01T00:00:01Z\\tdone\\t/w\\t/w\\tx\\tcapability=session-tidy,attempt_id=" + attempt + "\\n")
    ''')

FAKE_STATE = textwrap.dedent('''\
    #!{python}
    import os, pathlib, sys
    args = sys.argv[1:]
    def arg(name):
        return args[args.index(name) + 1]
    jobs, attempt = pathlib.Path(arg("--jobs")), arg("--attempt")
    root = pathlib.Path(os.environ["FAKE_ROOT"])
    with open(root / "states.jsonl", "a") as handle:
        handle.write(" ".join(args) + "\\n")
    rows = [l.split("\\t") for l in jobs.read_text().splitlines()] if jobs.exists() else []
    mine = [r for r in rows if len(r) > 5 and ("attempt_id=" + attempt) in r[5].split(",")]
    print("check=ok")
    print("state=" + ("dead" if mine and mine[-1][1] == "done" else "working"))
    ''')

FAKE_STEWARD = textwrap.dedent('''\
    #!{python}
    import json, os, pathlib, sys
    root = pathlib.Path(os.environ["FAKE_ROOT"])
    args = sys.argv[1:]
    body = pathlib.Path(args[args.index("--body-file") + 1]).read_text() if "--body-file" in args else None
    with open(root / "steward.jsonl", "a") as handle:
        handle.write(json.dumps({{"argv": args, "body": body}}) + "\\n")
    verdict = os.environ.get("FAKE_PEER_VERDICT", "true")
    exits = {{"true": 0, "failed": 1, "queued": 3, "unverified": 5}}
    print("prompted=" + verdict + " target=" + args[1] + " state_before=idle verify=observed ms=12")
    sys.exit(exits[verdict])
    ''')


def jsonl(path: Path, rows) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def claude_row(kind: str, text: str, cwd: str = "/w") -> dict:
    return {"type": kind, "cwd": cwd, "sessionId": "s", "message": {"role": kind, "content": text}}


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            return handle.read().rsplit(b") ", 1)[1][:1] != b"Z"
    except OSError:
        return False


class RunnerCase(unittest.TestCase):

    def setUp(self):
        self.iso = isolated_env()
        self.addCleanup(self.iso.cleanup)
        self.addCleanup(self.reap_detached)       # runs before the root is removed
        self.cwd = self.iso.root / "proj"
        self.cwd.mkdir()
        self.state = self.iso.xdg_state / "hearting" / "session-tidy"
        self.fake = self.iso.root / "fake"
        (self.fake / "bin").mkdir(parents=True)
        self.worker_cmd = self.fake / "bin" / "worker.py"
        finisher = FAKE_FINISHER
        self.worker_cmd.write_text(FAKE_WORKER.format(python=sys.executable, finisher=finisher), encoding="utf-8")
        self.steward_cmd = self.fake / "bin" / "steward.py"
        self.steward_cmd.write_text(FAKE_STEWARD.format(python=sys.executable), encoding="utf-8")
        self.state_cmd = self.fake / "bin" / "state.py"
        self.state_cmd.write_text(FAKE_STATE.format(python=sys.executable), encoding="utf-8")
        for path in (self.worker_cmd, self.steward_cmd, self.state_cmd):
            path.chmod(0o755)
        self.projects = self.iso.claude_dir / "projects" / "-proj"
        self.projects.mkdir(parents=True, exist_ok=True)
        self.scenario([])

    # -- fakes -------------------------------------------------------------

    def scenario(self, calls, default=None):
        doc = {"calls": calls, "default": default or {"mode": "ok"}}
        (self.fake / "scenario.json").write_text(json.dumps(doc), encoding="utf-8")

    def calls(self):
        path = self.fake / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def saved_input(self, n=1):
        return json.loads((self.fake / "inputs" / f"{n}.json").read_text(encoding="utf-8"))

    def gate(self, name="gate"):
        return str(self.fake / name)

    def open_gate(self, name="gate"):
        Path(self.gate(name)).write_text("go", encoding="utf-8")

    # -- running -----------------------------------------------------------

    def env(self, **more):
        env = {
            "HEARTING_TIDY_TEST_ROOT": str(self.iso.root),
            "HEARTING_TIDY_WORKER_CMD": str(self.worker_cmd),
            "HEARTING_TIDY_STATE_CMD": str(self.state_cmd),
            "HEARTING_TIDY_PEER_STEWARD": str(self.steward_cmd),
            "HEARTING_TIDY_POLL": "0.05",
            "HEARTING_TIDY_GOVERNOR_SLEEP_MIN": "0.02",
            "HEARTING_TIDY_GOVERNOR_SLEEP_MAX": "0.05",
            "HEARTING_TIDY_WORKER_WAIT": "60",
            "FAKE_ROOT": str(self.fake),
            "HERDR_PANE_ID": PANE,
        }
        env.update(more)
        return env

    def cli(self, *args, input=None, **more):
        return self.iso.run([sys.executable, TIDY, *args], input=input, extra=self.env(**more), cwd=self.cwd)

    def transcript(self, sid, rows):
        path = self.projects / f"{sid}.jsonl"
        jsonl(path, rows)
        return path

    def enqueue(self, sid, **more):
        result = self.cli("enqueue", "--harness", "claude", "--session-id", sid, **more)
        self.assertEqual(result.returncode, 0, result.stderr)
        line = result.stdout.strip()
        self.assertRegex(line, r"^enqueue=tidy-\d{14}-[0-9a-f]{6} seat=[0-9a-f]+ status=queued$")
        return line.split()[0].split("=", 1)[1]

    def item(self, qid):
        return st.read_json(self.state / "queue" / f"{qid}.json")

    def wait_for(self, predicate, what, timeout=WAIT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}")

    def wait_status(self, qid, *statuses):
        return self.wait_for(lambda: (self.item(qid) or {}).get("status") in statuses and self.item(qid),
                             f"{qid} to reach {statuses}")

    def runner_pid(self, qid):
        raw = self.wait_for(lambda: (self.state / "queue" / f"{qid}.pid").exists(), "the runner pid file")
        self.assertTrue(raw)
        return int((self.state / "queue" / f"{qid}.pid").read_text(encoding="ascii"))

    def reap_detached(self):
        """Every detached process this test started must exit by itself; none may remain."""
        pids = []
        if (self.state / "queue").is_dir():
            pids += [int(p.read_text(encoding="ascii")) for p in (self.state / "queue").glob("*.pid")
                     if p.read_text(encoding="ascii").strip().isdigit()]
        if (self.fake / "pids").exists():
            pids += [int(x) for x in (self.fake / "pids").read_text().split()]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and any(alive(p) for p in pids):
            time.sleep(0.1)
        left = [p for p in pids if alive(p)]
        for pid in left:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
        self.assertEqual(left, [], "detached processes were still running at the end of the test")

    # -- memory ------------------------------------------------------------

    def mem(self, *args):
        return self.iso.run([sys.executable, MEM, *args], cwd=self.cwd, extra=self.env())

    def records(self):
        db = self.iso.mem_store / "memory.db"
        if not db.exists():
            return {}
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return {r[0]: r[1:] for r in con.execute("SELECT id, status, type, tier, body FROM records")}
        finally:
            con.close()

    def notices(self):
        out = []
        folder = self.state / "notices"
        if folder.is_dir():
            for path in folder.glob("*.json"):
                out += [i["text"] for i in json.loads(path.read_text(encoding="utf-8")).get("items", [])]
        return out

    def watermark(self, sid, harness="claude"):
        return st.read_json(self.state / "watermarks" / f"{harness}-{st._digest(sid, size=16)}.json")


class EnqueueTest(RunnerCase):

    def test_enqueue_returns_one_line_at_once_and_the_tidy_finishes_on_its_own(self):
        shutil.copy(FIXTURES / "claude-choice.jsonl", self.projects / f"{CHOICE_SID}.jsonl")
        self.scenario([{"mode": "ok", "gate": self.gate(), "actions": [
            {"kind": "add", "type": "decision", "tier": "durable",
             "body": "The worker's own durable decision about storage."}]}])
        qid = self.enqueue(CHOICE_SID)
        # The call returned while the worker is still held at its gate: nothing waited for it.
        self.assertNotIn(self.item(qid)["status"], ("notified", "failed"))
        for path in (self.state / "queue" / f"{qid}.json",):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.wait_status(qid, "waiting-worker")
        self.open_gate()
        self.wait_status(qid, "notified")

        # input bundle seen by the worker
        data = self.saved_input()
        self.assertEqual(data["caller"], {"harness": "claude", "sid": CHOICE_SID})
        self.assertEqual(data["limits"], {"max_new_records": 10, "max_duplicate_groups": 5})
        self.assertIn("정리 시험을 시작합니다", data["sessions"][0]["text"])
        self.assertIn("저장 방식을 어떻게 할까요?", {c["question"] for c in data["user_choices"]})
        self.assertEqual(data["sessions"][0]["cursor_from"], 0)

        # the worker's file was applied by mem tidy-apply, the user's answers were recorded as decisions
        bodies = " ".join(row[3] for row in self.records().values())
        self.assertIn("The worker's own durable decision about storage.", bodies)
        self.assertIn("[결정]", bodies)
        # the watermark moved to where the bundle ended
        mark = self.watermark(CHOICE_SID)
        self.assertEqual(mark["cursor"], data["sessions"][0]["cursor_to"])
        self.assertGreater(mark["cursor"], 0)
        # one result line, with the way back
        notes = self.notices()
        self.assertEqual(len(notes), 1, notes)
        self.assertRegex(notes[0], rf"^\[tidy\] 묶음 {qid}: .*되돌리기: mem tidy-undo {qid}$")
        # and the input bundle (conversation text) is gone afterwards
        self.assertFalse((self.state / "runs" / qid / "input_v1.json").exists())
        self.assertTrue((self.state / "runs" / qid / "result.json").exists())
        # the notice reaches the calling session at its next prompt
        shown = self.cli("hook", "--harness", "claude", "--event", "prompt", "--session-id", CHOICE_SID).stdout
        self.assertIn(f"mem tidy-undo {qid}", shown)

    def test_the_registered_worker_launch_is_one_route_free_support_tuple(self):
        self.transcript("sid-A", [claude_row("user", "무언가 결정했다")])
        qid = self.enqueue("sid-A")
        self.wait_status(qid, "notified")
        (call,) = self.calls()
        argv = call["argv"]

        def value(flag):
            return argv[argv.index(flag) + 1]

        self.assertEqual(argv[0], "--start")
        self.assertEqual(value("--worker-type"), "support")
        self.assertEqual(value("--unit"), "ops/session-tidy-memory")
        self.assertEqual(value("--model-profile"), "balanced-deep")
        self.assertEqual(value("--dispatch-depth"), "1")
        self.assertEqual(value("--capability"), "session-tidy")
        self.assertEqual(value("--slug"), f"session-{qid}")
        for flag in ("--route-file", "--route-id", "--parent", "--worker-mode", "--model", "--effort"):
            self.assertNotIn(flag, argv)
        # the end of the worker is read from the checked exact-identity state, on this very attempt
        states = (self.fake / "states.jsonl").read_text().splitlines()
        self.assertGreaterEqual(len(states), 1)
        words = states[-1].split()
        self.assertEqual(words[words.index("--attempt") + 1], "att-fake-1")
        self.assertEqual(words[words.index("--jobs") + 1], str(self.fake / "jobs.log"))
        self.assertEqual(words[words.index("--pid-start") + 1], "1")
        self.assertEqual(words[-1], "attempt-state")
        # the prompt and the bundle are private files; the prompt names both paths
        self.assertEqual((call["prompt_mode"], call["input_mode"]), (0o600, 0o600))
        self.assertIn(str(self.state / "runs" / qid / "actions.json"), call["prompt"])
        self.assertIn(str(self.state / "runs" / qid / "input_v1.json"), call["prompt"])
        self.assertIn("`artifact: -`", call["prompt"])

    def test_the_worker_starts_without_the_callers_worker_marker_session_or_pane(self):
        self.transcript("sid-A", [claude_row("user", "무언가 결정했다")])
        qid = self.enqueue("sid-A", CLAUDE_CODE_SESSION_ID="sid-A", AGENT_ROUTE_ID="rt-x",
                           AGENT_ARTIFACT_ROOT="/nowhere")
        self.wait_status(qid, "notified")
        (call,) = self.calls()
        keys = set(call["env_keys"])
        for forbidden in ("HERDR_PANE_ID", "CLAUDE_CODE_SESSION_ID", "AGENT_ROUTE_ID", "AGENT_ARTIFACT_ROOT",
                          "AGENT_SESSION_ROLE", "AGENT_DISPATCH_DEPTH", "AGENT_DISPATCH_CHILD"):
            self.assertNotIn(forbidden, keys)

    def test_an_enqueue_from_a_worker_session_does_nothing(self):
        self.transcript("sid-A", [claude_row("user", "x")])
        result = self.cli("enqueue", "--harness", "claude", "--session-id", "sid-A", AGENT_SESSION_ROLE="worker")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "enqueue=none reason=worker"))
        self.assertFalse((self.state / "queue").exists())

    def test_nothing_new_to_tidy_ends_without_a_worker(self):
        qid = self.enqueue("sid-empty")      # no record of this session at all
        self.wait_status(qid, "notified")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.notices(), ["[정리] 새로 정리할 대화가 없습니다."])


class DetachTest(RunnerCase):

    def test_the_runner_survives_the_death_of_the_callers_process_group(self):
        self.transcript("sid-A", [claude_row("user", "죽은 부모의 결정")])
        self.scenario([{"mode": "ok", "gate": self.gate()}])
        # a stand-in for the harness's tool process: its own process group, runs the enqueue, then idles
        parent_code = (
            "import subprocess, sys, time\n"
            f"r = subprocess.run([sys.executable, {str(TIDY)!r}, 'enqueue', '--harness', 'claude', "
            "'--session-id', 'sid-A'], capture_output=True, text=True)\n"
            "print(r.stdout.strip(), flush=True)\n"
            "time.sleep(120)\n")
        parent = subprocess.Popen([sys.executable, "-c", parent_code], env=self.iso.env(self.env()),
                                  cwd=self.cwd, stdout=subprocess.PIPE, text=True, start_new_session=True)
        try:
            line = parent.stdout.readline().strip()
            self.assertRegex(line, r"^enqueue=tidy-\d{14}-[0-9a-f]{6} ")
            qid = line.split()[0].split("=", 1)[1]
            pid = self.runner_pid(qid)
            self.wait_status(qid, "waiting-worker")
            os.killpg(parent.pid, signal.SIGKILL)       # what a harness cleanup of the caller's group does
            parent.wait(timeout=30)
            time.sleep(0.3)
            self.assertTrue(alive(pid), "the detached runner died with the caller's process group")
            self.open_gate()
            self.wait_status(qid, "notified")
        finally:
            if parent.poll() is None:
                os.killpg(parent.pid, signal.SIGKILL)
                parent.wait(timeout=30)
            parent.stdout.close()
        # the runner leaves on its own after the queue is empty
        self.wait_for(lambda: not alive(pid), "the runner to exit")


class SerializeTest(RunnerCase):

    def test_two_reservations_at_once_finish_one_after_the_other_without_a_refusal(self):
        self.transcript("sid-A", [claude_row("user", "첫 결정")])
        self.transcript("sid-B", [claude_row("user", "둘째 결정")])
        self.scenario([{"mode": "ok", "gate": self.gate("gate-a")}, {"mode": "ok", "gate": self.gate("gate-b")}])
        first = self.enqueue("sid-A")
        self.wait_status(first, "waiting-worker")
        second = self.enqueue("sid-B")          # accepted at once; it queues behind the first
        pid_b = self.runner_pid(second)
        time.sleep(0.5)
        self.assertEqual(self.item(second)["status"], "queued")
        self.assertTrue(alive(pid_b))
        self.assertEqual(len(self.calls()), 1)
        self.open_gate("gate-b")
        self.open_gate("gate-a")
        self.wait_status(first, "notified")
        self.wait_status(second, "notified")
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        # strictly one at a time: the second worker started after the first one's row closed
        self.assertEqual([c["fields"]["batch_id"] for c in calls], [first, second])
        self.assertEqual(len(self.notices()), 2)

    def test_the_same_session_tidied_twice_reads_only_what_came_after_the_first(self):
        path = self.transcript("sid-A", [claude_row("user", "옛 메시지 하나"), claude_row("assistant", "옛 답")])
        first = self.enqueue("sid-A")
        self.wait_status(first, "notified")
        mark = self.watermark("sid-A")["cursor"]
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(claude_row("user", "새 메시지 둘"), ensure_ascii=False) + "\n")
        second = self.enqueue("sid-A")
        self.wait_status(second, "notified")
        one, two = self.saved_input(1), self.saved_input(2)
        self.assertIn("옛 메시지 하나", one["sessions"][0]["text"])
        text = two["sessions"][0]["text"]
        self.assertIn("새 메시지 둘", text)
        self.assertNotIn("옛 메시지 하나", text)
        self.assertEqual(two["sessions"][0]["cursor_from"], mark)
        self.assertGreater(self.watermark("sid-A")["cursor"], mark)


class RecentSessionsTest(RunnerCase):

    def test_recent_untidied_sessions_of_the_seat_join_and_older_ones_do_not(self):
        now = time.time()
        fresh = self.transcript("sid-fresh", [claude_row("user", "어제 닫은 세션의 결정")])
        stale = self.transcript("sid-stale", [claude_row("user", "나흘 전 세션의 결정")])
        os.utime(fresh, (now - 1 * DAY, now - 1 * DAY))
        os.utime(stale, (now - 4 * DAY, now - 4 * DAY))
        self.transcript("sid-A", [claude_row("user", "지금 세션의 결정")])
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            seat = st.resolve_seat("claude", str(self.cwd))
            st.record_event(seat, "claude", "sid-fresh", "start", transcript=str(fresh), now=now - 1 * DAY)
            st.record_event(seat, "claude", "sid-stale", "start", transcript=str(stale), now=now - 4 * DAY)
        qid = self.enqueue("sid-A")
        self.wait_status(qid, "notified")
        got = {s["sid"]: s["role"] for s in self.saved_input()["sessions"]}
        self.assertEqual(got, {"sid-A": "caller", "sid-fresh": "recent"})
        for sid in ("sid-A", "sid-fresh"):
            self.assertGreater(self.watermark(sid)["cursor"], 0)
        self.assertIsNone(self.watermark("sid-stale"))


class FailureTest(RunnerCase):

    def setUp(self):
        super().setUp()
        self.transcript("sid-A", [claude_row("user", "실패해도 남아야 하는 결정")])
        self.mem("add", "durable", "lesson", "an existing lesson that must stay exactly as it is")
        self.card_line = self.cli("card", "--harness", "claude", "--session-id", "sid-A",
                                  "--text", "진행 중: 시험\n다음: 계속").stdout.strip()
        self.card_path = Path(self.card_line.split()[0].split("=", 1)[1])
        self.card_before = self.card_path.read_bytes()
        self.records_before = self.records()
        self.assertEqual(len(self.records_before), 1)

    def assert_untouched_and_told(self, qid, reason_part):
        item = self.wait_status(qid, "failed")
        self.assertIn(reason_part, item["error"])
        self.assertEqual(self.card_path.read_bytes(), self.card_before)
        self.assertEqual(self.records(), self.records_before)
        self.assertIsNone(self.watermark("sid-A"))
        notes = self.notices()
        self.assertEqual(len(notes), 1, notes)
        self.assertRegex(notes[0], r"^\[정리\] 기억 정리를 끝내지 못했습니다\. 카드와 기존 기억은 그대로이고 다음 정리 때 이어서 처리됩니다\. \(사유: ")
        self.assertNotIn("\n", notes[0])
        self.assertFalse((self.state / "runs" / qid / "input_v1.json").exists())

    def test_a_worker_that_wrote_something_that_is_not_json(self):
        self.scenario([{"mode": "bad-json"}])
        self.assert_untouched_and_told(self.enqueue("sid-A"), "not valid JSON")

    def test_a_worker_that_wrote_the_wrong_shape(self):
        self.scenario([{"mode": "bad-shape"}])
        self.assert_untouched_and_told(self.enqueue("sid-A"), "actions list")

    def test_a_worker_that_finished_without_writing(self):
        self.scenario([{"mode": "no-output"}])
        self.assert_untouched_and_told(self.enqueue("sid-A"), "without writing actions.json")

    def test_a_launcher_that_refuses_for_a_reason_other_than_capacity(self):
        self.scenario([{"mode": "fail-start"}])
        self.assert_untouched_and_told(self.enqueue("sid-A"), "invalid-dispatch-worker-mode")
        self.assertEqual(len(self.calls()), 1)         # no retry for a real refusal

    def test_a_worker_that_never_finishes_is_given_up_on_after_the_wait_limit(self):
        self.scenario([{"mode": "hang"}])
        self.assert_untouched_and_told(self.enqueue("sid-A", HEARTING_TIDY_WORKER_WAIT="1"), "did not finish")

    def test_the_next_tidy_picks_the_same_conversation_up_again(self):
        self.scenario([{"mode": "bad-json"}, {"mode": "ok", "actions": [
            {"kind": "add", "type": "decision", "tier": "durable", "body": "Recovered on the next tidy."}]}])
        failed = self.enqueue("sid-A")
        self.wait_status(failed, "failed")
        again = self.enqueue("sid-A")
        self.wait_status(again, "notified")
        self.assertIn("실패해도 남아야 하는 결정", self.saved_input(2)["sessions"][0]["text"])
        self.assertIn("Recovered on the next tidy.", " ".join(r[3] for r in self.records().values()))
        self.assertIsNotNone(self.watermark("sid-A"))


class PartialApplyTest(RunnerCase):
    """A batch that stopped after some writes must say so and keep its undo command."""

    def run_item(self, apply_stub, **patches):
        patches.setdefault("advance_watermarks", runner.advance_watermarks)
        with self.iso.patched_environ(self.env()):
            seat = st.resolve_seat("claude", str(self.cwd), os.environ)
            item = {"schema": runner.SCHEMA, "id": runner.new_id(), "created": runner._now(),
                    "status": "queued", "updated": runner._now(), "seat": seat.as_dict(),
                    "harness": "claude", "sid": "sid-A", "cwd": str(self.cwd), "transcript": "", "attempts": 0}
            runner.write_item(item)
            bundle = mock.Mock(empty=False, cursors=[])
            with mock.patch.object(runner, "assemble", return_value=bundle), \
                    mock.patch.object(runner, "dispatch_worker", return_value={"attempt_id": "att-x"}), \
                    mock.patch.object(runner, "wait_worker"), \
                    mock.patch.object(runner, "validate_actions"), \
                    mock.patch.object(runner, "apply_actions", side_effect=apply_stub), \
                    mock.patch.multiple(runner, **patches):
                runner.process_item(item)
            return item["id"], self.item(item["id"])

    @staticmethod
    def journal(run_dir, done, intent=1):
        ops = ([{"op": "add", "state": "done", "id": f"r{n}"} for n in range(done)]
               + [{"op": "add", "state": "intent"}] * intent + [{"op": "add", "state": "dropped"}])
        st.atomic_write_json(run_dir / "undo.json", {"ops": ops})

    def test_an_applier_that_stopped_halfway_is_reported_as_partial_with_the_undo_command(self):
        def stopped(item, run_dir, bundle):
            self.journal(run_dir, 2)
            raise runner.RunnerFailure(f"묶음 {run_dir.name}: 부분 적용 2건 · RuntimeError: boom "
                                       f"— 되돌리기: mem tidy-undo {run_dir.name}")
        qid, item = self.run_item(stopped)
        self.assertEqual((item["status"], item["applied"]), ("failed", [2, 1]))
        notes = self.notices()
        self.assertEqual(len(notes), 1, notes)
        self.assertRegex(notes[0], rf"^\[정리\] 기억 정리가 중간에 멈췄습니다\(2건 반영, 1건은 반영 여부 불명\)\. "
                                   rf".*RuntimeError: boom\) — 되돌리기: mem tidy-undo {qid}$")
        self.assertNotIn("그대로", notes[0])
        self.assertEqual(notes[0].count("mem tidy-undo"), 1)

    def test_a_failure_after_a_full_apply_still_names_the_writes_and_the_undo_command(self):
        def applied(item, run_dir, bundle):
            self.journal(run_dir, 1, intent=0)
            return f"[tidy] 묶음 {run_dir.name}: 추가 1 — 되돌리기: mem tidy-undo {run_dir.name}"
        qid, item = self.run_item(applied, advance_watermarks=mock.Mock(side_effect=OSError("disk full")))
        self.assertEqual((item["status"], item["applied"]), ("failed", [1, 0]))
        self.assertRegex(self.notices()[0], rf"^\[정리\] 기억 정리가 중간에 멈췄습니다\(1건 반영\)\. .*"
                                            rf"— 되돌리기: mem tidy-undo {qid}$")

    def test_a_write_that_landed_before_the_journal_could_mark_it_finished_is_not_called_untouched(self):
        def unmarked(item, run_dir, bundle):
            self.journal(run_dir, 0, intent=1)        # the store changed, the "done" flush did not
            raise runner.RunnerFailure("apply stopped: OSError: journal write failed")
        qid, item = self.run_item(unmarked)
        self.assertEqual(item["applied"], [0, 1])
        note = self.notices()[0]
        self.assertRegex(note, rf"^\[정리\] 기억 정리가 중간에 멈췄습니다\(0건 반영, 1건은 반영 여부 불명\)\. .*"
                               rf"— 되돌리기: mem tidy-undo {qid}$")
        self.assertNotIn("그대로", note)

    def test_an_unreadable_journal_is_reported_as_unknown_with_the_undo_command(self):
        def garbled(item, run_dir, bundle):
            (run_dir / "undo.json").write_text("{not json", encoding="utf-8")
            raise runner.RunnerFailure("apply stopped")
        qid, _item = self.run_item(garbled)
        note = self.notices()[0]
        self.assertRegex(note, rf"^\[정리\] 기억 정리가 중간에 멈췄습니다\(반영 여부를 확인하지 못했습니다\)\. .*"
                               rf"— 되돌리기: mem tidy-undo {qid}$")

    def test_a_failure_before_any_write_still_says_memory_is_untouched(self):
        def refused(item, run_dir, bundle):
            raise runner.RunnerFailure("apply did not run: boom")
        _qid, item = self.run_item(refused)
        self.assertEqual(item["applied"], [0, 0])
        self.assertRegex(self.notices()[0], r"^\[정리\] 기억 정리를 끝내지 못했습니다\. 카드와 기존 기억은 그대로")


class GovernorTest(RunnerCase):

    def test_governor_refusals_are_waited_out_not_reported_as_failures(self):
        self.transcript("sid-A", [claude_row("user", "자리가 날 때까지 기다린다")])
        self.scenario([{"mode": "governor"}, {"mode": "governor"}, {"mode": "ok"}])
        qid = self.enqueue("sid-A")
        self.wait_status(qid, "notified")
        self.assertEqual([c["mode"] for c in self.calls()], ["governor", "governor", "ok"])
        self.assertEqual(self.item(qid)["result"], "applied")
        self.assertEqual(len(self.notices()), 1)

    def test_a_governor_that_never_frees_a_seat_ends_after_the_wait_limit_with_one_line(self):
        self.transcript("sid-A", [claude_row("user", "끝내 자리가 안 난다")])
        self.scenario([], default={"mode": "governor"})
        qid = self.enqueue("sid-A", HEARTING_TIDY_GOVERNOR_WAIT="0.4")
        item = self.wait_status(qid, "failed")
        self.assertIn("no free worker seat", item["error"])
        self.assertGreaterEqual(len(self.calls()), 2)
        self.assertIsNone(self.watermark("sid-A"))
        self.assertEqual(len(self.notices()), 1)


class LockWaitTest(RunnerCase):

    def hold_lock(self):
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.state / "runner.lock", os.O_RDWR | os.O_CREAT, 0o600)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        return fd

    def test_a_runner_that_waits_too_long_leaves_its_entry_queued_and_the_next_tidy_finishes_it(self):
        import fcntl
        self.transcript("sid-A", [claude_row("user", "잠금 때문에 밀린 결정")])
        fd = self.hold_lock()
        first = self.enqueue("sid-A", HEARTING_TIDY_LOCK_WAIT="1.0")
        pid = self.runner_pid(first)
        self.wait_for(lambda: not alive(pid), "the waiting runner to give up")
        self.assertEqual(self.item(first)["status"], "queued")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.notices(), [])
        fcntl.flock(fd, fcntl.LOCK_UN)
        second = self.enqueue("sid-A")          # the next normal reservation continues the queue
        self.wait_status(second, "notified")
        self.assertEqual(self.item(first)["status"], "notified")
        self.assertEqual(self.item(first)["result"], "applied")
        self.assertEqual(self.item(second)["result"], "nothing-new")
        self.assertEqual(len(self.calls()), 1)       # one worker, for the conversation both entries share


class HandoffTest(RunnerCase):

    def card(self, text="진행 중: 인계 시험\n다음 할 일: 받아서 계속"):
        result = self.cli("card", "--harness", "claude", "--session-id", "sid-A", "--text", text)
        self.assertEqual(result.returncode, 0, result.stderr)
        return text

    def test_the_typed_verdict_and_the_exit_code_of_peer_steward_are_reported_as_they_are(self):
        text = self.card()
        for verdict, code in (("true", 0), ("failed", 1), ("queued", 3), ("unverified", 5)):
            with self.subTest(verdict=verdict):
                result = self.cli("handoff", "peer-pane", "--harness", "claude", "--session-id", "sid-A",
                                  FAKE_PEER_VERDICT=verdict)
                self.assertEqual(result.returncode, code, result.stderr)
                lines = result.stdout.strip().splitlines()
                self.assertEqual(len(lines), 1, result.stdout)
                self.assertRegex(lines[0], rf"^prompted={verdict} target=peer-pane .* exit={code}$")
        sent = [json.loads(x) for x in (self.fake / "steward.jsonl").read_text().splitlines()]
        self.assertEqual(len(sent), 4)
        for row in sent:       # only ever `prompt <target> --body-file <card>`; the body is the card text
            self.assertEqual(row["argv"][:2], ["prompt", "peer-pane"])
            self.assertEqual(row["argv"][2], "--body-file")
            self.assertEqual(len(row["argv"]), 4)
            self.assertEqual(row["body"].strip(), text.strip())
        self.assertEqual(list((self.state / "handoff").glob("*")), [])      # the body file does not linger

    def test_without_a_card_nothing_is_sent(self):
        result = self.cli("handoff", "peer-pane", "--harness", "claude", "--session-id", "sid-A")
        self.assertEqual((result.returncode, result.stdout.strip()), (1, "prompted=false reason=no-card"))
        self.assertFalse((self.fake / "steward.jsonl").exists())


class TestHooksAreInertOutsideATestRoot(RunnerCase):

    def test_overrides_need_a_test_root_that_contains_the_state_folder_and_the_executable(self):
        var = "HEARTING_TIDY_WORKER_CMD"

        outside = Path(self.iso.root.parent) / "not-under-the-root.py"
        with self.iso.patched_environ({var: str(self.worker_cmd), "HEARTING_TIDY_POLL": "0.01"}):
            self.assertIsNone(runner.injected_executable(var))           # no test root named
            self.assertEqual(runner.tunable("POLL"), runner.TUNABLES["POLL"])
        with self.iso.patched_environ({var: str(self.worker_cmd), "HEARTING_TIDY_POLL": "0.01",
                                       "HEARTING_TIDY_TEST_ROOT": str(self.iso.root)}):
            self.assertEqual(runner.injected_executable(var), self.worker_cmd.resolve())
            self.assertEqual(runner.tunable("POLL"), 0.01)
        with self.iso.patched_environ({var: str(self.worker_cmd), "HEARTING_TIDY_POLL": "0.01",
                                       "HEARTING_TIDY_TEST_ROOT": str(self.iso.root / "elsewhere")}):
            self.assertIsNone(runner.injected_executable(var))           # state folder is not inside it
            self.assertEqual(runner.tunable("POLL"), runner.TUNABLES["POLL"])
        with self.iso.patched_environ({var: str(outside), "HEARTING_TIDY_TEST_ROOT": str(self.iso.root)}):
            self.assertIsNone(runner.injected_executable(var))           # executable is not inside it

    def test_the_real_launch_goes_through_the_adapters_checked_wrapper(self):
        item = {"harness": "codex", "cwd": str(self.cwd), "id": "tidy-x"}
        # git refuses a checkout owned by another user under the isolated HOME, so trust it explicitly
        trust = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"}
        with self.iso.patched_environ(trust):
            for harness, wrapper in (("claude", "claude"), ("codex", "codex"), ("opencode", "claude")):
                item["harness"] = harness
                command = runner.launch_command(item, Path("/p"), "slug")
                self.assertEqual(command[1], str(ROOT / "adapters" / wrapper / "bin" / "dispatch-headless.py"))
                self.assertIn("--worker-type", command)
                self.assertNotIn("--route-file", command)


if __name__ == "__main__":
    unittest.main()
