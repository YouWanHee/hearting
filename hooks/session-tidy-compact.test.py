#!/usr/bin/env python3
"""Compact hook contract checks with isolated state and a fake detached runner."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import session_tidy as st  # noqa: E402
import session_tidy_runner as runner  # noqa: E402


class CompactHookTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="session-tidy-compact-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.env = {"XDG_STATE_HOME": str(self.state)}
        envpatch = mock.patch.dict("os.environ", self.env)
        envpatch.start()
        self.addCleanup(envpatch.stop)
        self.seat = st.Seat("project", "fixture-seat", harness="claude", project_key="fixture")
        self.resolve = mock.patch.object(st, "resolve_seat", return_value=self.seat)
        self.resolve.start()
        self.addCleanup(self.resolve.stop)
        self.enqueue = mock.patch.object(runner, "enqueue_item", return_value={"id": "fixture"})
        self.enqueue_mock = self.enqueue.start()
        self.addCleanup(self.enqueue.stop)

    def test_before_is_shared_and_does_not_touch_state(self):
        self.assertEqual(st.run_hook("claude", "compact", "sid-main", phase="before", env=self.env),
                         st.COMPACT_CARD_INSTRUCTIONS)
        self.assertEqual(st.run_hook("opencode", "compact", "sid-main", phase="before", env=self.env),
                         st.COMPACT_CARD_INSTRUCTIONS)
        self.assertEqual(st.run_hook("codex", "compact", "sid-main", phase="before", env=self.env), "")
        self.assertFalse(self.state.exists())

    def test_after_records_then_saves_then_queues_and_author_never_rereads(self):
        result = st.run_hook("claude", "compact", "sid-main", phase="after",
                             compact_summary="summary\x00\n\tkept", cwd=str(self.cwd),
                             env=self.env, now=1000)
        self.assertEqual(result, "")
        self.assertEqual(self.enqueue_mock.call_count, 1)
        self.assertEqual(self.enqueue_mock.call_args.args[1:4], ("claude", "sid-main", str(self.cwd)))
        ledger = list((self.state / "hearting/session-tidy/sessions").glob("*.jsonl"))
        row = json.loads(ledger[0].read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual((row["event"], row["source"], row["epoch"]), ("compact", "compact", 1))
        card = st.read_latest_card(self.seat)
        self.assertEqual(card["origin"], "runtime-compact")
        self.assertEqual(card["body"], "summary\n\tkept")
        self.assertFalse((self.state / "hearting/session-tidy/clear").exists())
        self.assertFalse((self.state / "hearting/session-tidy/handover").exists())
        self.assertEqual(st.run_hook("claude", "prompt", "sid-main", env=self.env, now=1001), "")
        self.assertEqual(st.run_hook("claude", "start", "sid-main", source="compact",
                                     env=self.env, now=1002), "")
        self.assertEqual(st.run_hook("claude", "prompt", "sid-main", env=self.env, now=1003), "")
        self.assertIn("summary", st.run_hook("claude", "start", "sid-next", env=self.env, now=1004))
        self.assertEqual(st.run_hook("claude", "prompt", "sid-next", env=self.env, now=1005), "")

    def test_empty_after_still_queues_once_without_replacing_card(self):
        st.run_hook("claude", "compact", "sid-main", phase="after", compact_summary="saved",
                    cwd=str(self.cwd), env=self.env, now=1000)
        card_before = st.read_latest_card(self.seat)
        st.run_hook("claude", "compact", "sid-main", phase="after", compact_summary=" \x00\n ",
                    cwd=str(self.cwd), env=self.env, now=1010)
        self.assertEqual(self.enqueue_mock.call_count, 2)
        self.assertEqual(st.read_latest_card(self.seat), card_before)
        self.assertEqual(st.session_summary(self.seat)[("claude", "sid-main")]["epoch"], 1)

    def test_oversized_summary_is_cut_by_the_existing_card_limit(self):
        summary = "진행 중: " + "가" * (st.CARD_BODY_MAX_BYTES // 3 + 500)
        st.run_hook("claude", "compact", "sid-main", phase="after", compact_summary=summary,
                    cwd=str(self.cwd), env=self.env, now=1000)
        body = st.read_latest_card(self.seat)["body"]
        self.assertLessEqual(len(body.encode("utf-8")), st.CARD_BODY_MAX_BYTES)
        self.assertTrue(summary.startswith(body))
        self.assertEqual(body, st.sanitize_body(summary))
        self.assertEqual(self.enqueue_mock.call_count, 1)

    def test_start_compact_then_after_keeps_one_epoch_and_excludes_author_card(self):
        st.run_hook("claude", "start", "sid-main", source="compact", cwd=str(self.cwd),
                    env=self.env, now=1000)
        st.run_hook("claude", "compact", "sid-main", phase="after", compact_summary="summary",
                    cwd=str(self.cwd), env=self.env, now=1001)
        row = st.session_summary(self.seat)[("claude", "sid-main")]
        card = st.read_latest_card(self.seat)
        self.assertEqual(row["epoch"], 1)
        self.assertEqual(card["author"]["epoch"], 1)
        self.assertEqual(card["origin"], "runtime-compact")
        self.assertEqual(st.run_hook("claude", "start", "sid-main", cwd=str(self.cwd),
                                     env=self.env, now=1002), "")
        self.assertEqual(self.enqueue_mock.call_count, 1)

    def test_nearby_real_compactions_each_save_a_card_and_queue(self):
        st.run_hook("claude", "compact", "sid-main", phase="after", compact_summary="first",
                    cwd=str(self.cwd), env=self.env, now=1000)
        st.run_hook("claude", "compact", "sid-main", phase="after", compact_summary="second",
                    cwd=str(self.cwd), env=self.env, now=1010)
        card = st.read_latest_card(self.seat)
        self.assertEqual(card["generation"], 2)
        self.assertEqual(card["body"], "second")
        self.assertEqual(st.session_summary(self.seat)[("claude", "sid-main")]["epoch"], 1)
        self.assertEqual(self.enqueue_mock.call_count, 2)

    def test_after_prefers_payload_transcript_then_falls_back_to_ledger(self):
        st.run_hook("claude", "start", "sid-ledger", transcript="ledger.jsonl", cwd=str(self.cwd),
                    env=self.env, now=1000)
        st.run_hook("claude", "compact", "sid-ledger", phase="after", cwd=str(self.cwd),
                    env=self.env, now=1001)
        self.assertEqual(self.enqueue_mock.call_args.args[-1], "ledger.jsonl")
        st.run_hook("claude", "compact", "sid-payload", phase="after", transcript="payload.jsonl",
                    cwd=str(self.cwd), env=self.env, now=1010)
        self.assertEqual(self.enqueue_mock.call_args.args[-1], "payload.jsonl")

    def test_busy_seat_lock_skips_the_card_but_still_queues(self):
        args = SimpleNamespace(harness="claude", session_id="sid-main", event="compact",
                               source=None, transcript="payload.jsonl", cwd=str(self.cwd), reread=False,
                               phase="after")
        with st.seat_lock(self.seat.key), mock.patch.object(st, "COMPACT_LOCK_WAIT_SEC", 0.1):
            with mock.patch.object(st, "resolve_seat", return_value=self.seat), \
                 mock.patch("sys.stdin", io.StringIO('{"summary":"summary"}')), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(st.cmd_hook(args), 0)
                self.assertEqual(output.getvalue(), "")
        self.assertEqual(self.enqueue_mock.call_count, 1)
        self.assertEqual(self.enqueue_mock.call_args.args[-1], "payload.jsonl")
        self.assertIsNone(st.read_latest_card(self.seat))

    def test_bounded_seat_lock_waits_for_a_short_holder(self):
        import threading
        held = threading.Event()

        def holder():
            with st.seat_lock(self.seat.key):
                held.set()
                import time
                time.sleep(0.2)

        thread = threading.Thread(target=holder)
        thread.start()
        held.wait(2)
        st.run_hook("claude", "compact", "sid-main", phase="after", compact_summary="summary",
                    cwd=str(self.cwd), env=self.env, now=1000)
        thread.join()
        self.assertEqual(st.read_latest_card(self.seat)["body"], "summary")
        self.assertEqual(self.enqueue_mock.call_count, 1)

    def test_card_write_failure_does_not_suppress_queue(self):
        with mock.patch.object(st, "write_card", side_effect=OSError("fixture")):
            st.run_hook("claude", "compact", "sid-main", phase="after", compact_summary="summary",
                        cwd=str(self.cwd), env=self.env, now=1000)
        self.assertEqual(self.enqueue_mock.call_count, 1)

    def test_legacy_compact_remains_ledger_only(self):
        st.run_hook("claude", "compact", "sid-main", cwd=str(self.cwd), env=self.env, now=1000)
        self.assertEqual(self.enqueue_mock.call_count, 0)
        self.assertIsNone(st.read_latest_card(self.seat))
        self.assertEqual(st.session_summary(self.seat)[("claude", "sid-main")]["epoch"], 1)

    def test_worker_hooks_emit_nothing_and_never_queue(self):
        worker = {**self.env, "AGENT_SESSION_ROLE": "worker"}
        self.assertEqual(st.run_hook("claude", "compact", "sid-worker", phase="before", env=worker), "")
        self.assertEqual(st.run_hook("claude", "compact", "sid-worker", phase="after",
                                     compact_summary="summary", env=worker), "")
        self.assertFalse(self.state.exists())
        self.assertEqual(self.enqueue_mock.call_count, 0)

    def test_runtime_bridge_before_contract_and_native_worker_exclusion(self):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AGENT_", "MEM_", "CODEX_", "OPENCODE_", "FLEET_"))}
        env.update({"AGENT_HOME": str(ROOT), "XDG_STATE_HOME": str(self.state),
                    "PYTHONDONTWRITEBYTECODE": "1"})
        bridge = ROOT / "utilities/worker-state-hook.py"

        def invoke(harness, payload, **extra):
            call_env = {**env, **extra}
            return subprocess.run([sys.executable, str(bridge), "compact-before", "--harness", harness],
                                  input=json.dumps(payload), cwd=self.cwd, env=call_env,
                                  text=True, capture_output=True, timeout=8)

        for harness in ("claude", "codex", "opencode"):
            result = invoke(harness, {"session_id": "sid-bridge", "cwd": str(self.cwd)})
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = st.COMPACT_CARD_INSTRUCTIONS if harness in ("claude", "opencode") else ""
            self.assertEqual(result.stdout.strip(), expected)
            worker = invoke(harness, {"session_id": "sid-worker", "cwd": str(self.cwd)},
                            AGENT_SESSION_ROLE="worker")
            self.assertEqual((worker.returncode, worker.stdout), (0, ""))
        for harness, payload in (("claude", {"session_id": "claude-child", "agent_id": "agent-1"}),
                                 ("codex", {"session_id": "codex-child", "agent_type": "subagent"})):
            result = invoke(harness, payload)
            self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_runtime_bridge_after_forwards_payload_and_prints_nothing(self):
        fake_home = self.root / "fake-tidy-home"
        (fake_home / "utilities").mkdir(parents=True)
        capture = self.root / "tidy-calls.jsonl"
        (fake_home / "utilities/session_tidy.py").write_text(
            "import json,sys\n"
            f"open({str(capture)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], 'stdin': sys.stdin.read()}}) + '\\n')\n"
            "print('ignored after output')\n", encoding="utf-8")
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AGENT_", "MEM_", "CODEX_", "OPENCODE_", "FLEET_"))}
        env.update({"AGENT_HOME": str(fake_home), "XDG_STATE_HOME": str(self.state),
                    "PYTHONDONTWRITEBYTECODE": "1"})
        bridge = ROOT / "utilities/worker-state-hook.py"
        payloads = {
            "claude": {"session_id": "sid-c", "cwd": str(self.cwd), "transcript_path": "c.jsonl",
                       "compact_summary": "claude summary"},
            "codex": {"session_id": "sid-x", "cwd": str(self.cwd), "transcript_path": "x.jsonl"},
            "opencode": {"sessionID": "sid-o", "cwd": str(self.cwd), "summary": "opencode summary"},
        }
        for harness, payload in payloads.items():
            result = subprocess.run([sys.executable, str(bridge), "compact-after", "--harness", harness],
                                    input=json.dumps(payload), env=env, text=True,
                                    capture_output=True, timeout=8)
            self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        calls = [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(calls), 3)
        expected = {
            "claude": ("sid-c", "c.jsonl", "claude summary"),
            "codex": ("sid-x", "x.jsonl", ""),
            "opencode": ("sid-o", None, "opencode summary"),
        }
        for call in calls:
            argv = call["argv"]
            harness = argv[argv.index("--harness") + 1]
            sid, transcript, summary = expected[harness]
            self.assertEqual(argv[:6], ["hook", "--harness", harness, "--event", "compact", "--phase"])
            self.assertEqual(argv[6], "after")
            self.assertEqual(argv[argv.index("--session-id") + 1], sid)
            self.assertEqual(argv[argv.index("--cwd") + 1], str(self.cwd))
            if transcript:
                self.assertEqual(argv[argv.index("--transcript") + 1], transcript)
            else:
                self.assertNotIn("--transcript", argv)
            self.assertEqual(json.loads(call["stdin"]), {"summary": summary})

    def test_main_wrapper_discards_partial_output_and_fails_open_on_timeout(self):
        fake_home = self.root / "fake-home"
        utility_dir = fake_home / "utilities"
        utility_dir.mkdir(parents=True)
        helper = utility_dir / "worker-state-hook.py"
        helper.write_text(
            "import os,sys,time\n"
            "print('partial ledger output', flush=True)\n"
            "if os.environ.get('FAKE_COMPACT_MODE') == 'timeout': time.sleep(8)\n"
            "raise SystemExit(2 if os.environ.get('AGENT_DISPATCH_STAGE_AUTHORITY') == '0' else 1)\n",
            encoding="utf-8")
        wrapper = ROOT / "hooks/worker-state-compact.py"
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AGENT_", "MEM_", "CODEX_", "OPENCODE_", "FLEET_"))}
        env.update({"AGENT_HOME": str(fake_home), "PYTHONDONTWRITEBYTECODE": "1",
                    "XDG_STATE_HOME": str(self.state), "HERDR_PANE_ID": ""})
        partial = subprocess.run([sys.executable, str(wrapper), "compact-before", "--harness", "claude"],
                                 env={**env, "FAKE_COMPACT_MODE": "partial"}, text=True,
                                 input="{}", capture_output=True, timeout=8)
        self.assertEqual((partial.returncode, partial.stdout), (0, ""))
        timed = subprocess.run([sys.executable, str(wrapper), "compact-before", "--harness", "claude"],
                               env={**env, "FAKE_COMPACT_MODE": "timeout"}, text=True,
                               input="{}", capture_output=True, timeout=8)
        self.assertEqual((timed.returncode, timed.stdout), (0, ""))
        sub = subprocess.run([sys.executable, str(wrapper), "compact-after", "--harness", "claude"],
                             env={**env, "AGENT_DISPATCH_STAGE_AUTHORITY": "0"}, text=True,
                             input="{}", capture_output=True, timeout=8)
        self.assertEqual(sub.returncode, 2)
        self.assertEqual(sub.stdout, "partial ledger output\n")

    def _opencode_fixture(self, scenario, extra_env=None):
        """Run the real plugin against a fake v1 client; a python3 shim records every helper call."""
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        fake_bin = self.root / f"bin-{scenario}"
        fake_bin.mkdir()
        calls = self.root / f"calls-{scenario}.jsonl"
        instructions = self.root / "instructions.txt"
        instructions.write_text(st.COMPACT_CARD_INSTRUCTIONS + "\n", encoding="utf-8")
        shim = fake_bin / "python3"
        shim.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} -c '\n"
            "import json, os, sys\n"
            "argv = sys.argv[1:]\n"
            "name = os.path.basename(argv[0]) if argv else \"\"\n"
            "body = sys.stdin.read() if name == \"worker-state-hook.py\" else \"\"\n"
            f"open({json.dumps(str(calls))}, \"a\").write(json.dumps({{\"argv\": argv, \"stdin\": body}}) + \"\\n\")\n"
            "if name == \"worker-state-hook.py\":\n"
            "    if os.environ.get(\"FAKE_LEDGER_FAIL\") == \"1\":\n"
            "        sys.stderr.write(\"ledger missing\\n\"); raise SystemExit(2)\n"
            "    if argv[1] == \"compact-before\":\n"
            f"        sys.stdout.write(open({json.dumps(str(instructions))}).read())\n"
            "' \"$@\"\n", encoding="utf-8")
        shim.chmod(0o700)
        plugin = ROOT / "adapters/opencode/plugins/hearting-guards.js"
        script = f"""import {{ AgentHarnessGuards }} from {json.dumps(plugin.as_uri())}
const scenario = {json.dumps(scenario)}
const sid = "ses_fixture"
const old = {{ info: {{ id: "old-summary", sessionID: sid, role: "assistant", summary: true, finish: "stop", time: {{ created: Date.now() - 5000 }} }}, parts: [{{ type: "text", text: "old" }}] }}
const fresh = {{ info: {{ id: "new-summary", sessionID: sid, role: "assistant", summary: true, finish: "stop", time: {{ created: 0 }} }}, parts: [{{ type: "text", text: "fresh summary" }}, {{ type: "image", text: "ignored" }}] }}
const calls = []
const ctx = {{ directory: {json.dumps(str(self.cwd))}, worktree: {json.dumps(str(self.cwd))}, client: {{ session: {{
  get: async (request) => {{
    calls.push(["get", request.path.id])
    if (scenario === "unknown") throw new Error("lookup failed")
    return {{ data: {{ id: request.path.id, parentID: request.path.id === "ses_child" ? sid : null }} }}
  }},
  messages: async (request) => {{
    calls.push(["messages", request.path.id, request.query, Boolean(request.signal)])
    fresh.info.time.created = Date.now()
    return {{ data: [old, fresh] }}
  }},
}} }} }}
const plugin = await AgentHarnessGuards(ctx)
const result = {{ calls }}
const output = {{ context: [], prompt: "keep this prompt" }}
try {{
  await plugin["experimental.session.compacting"]({{ sessionID: sid }}, output)
  result.before = "ok"
}} catch (error) {{ result.before = String(error.message || error) }}
result.output = output
try {{
  await plugin.event({{ event: {{ type: "session.compacted", properties: {{ sessionID: sid }} }} }})
  result.after = "ok"
}} catch (error) {{ result.after = String(error.message || error) }}
if (scenario === "main") {{
  result.childOutput = {{ context: [], prompt: "child prompt" }}
  await plugin["experimental.session.compacting"]({{ sessionID: "ses_child" }}, result.childOutput)
  await plugin.event({{ event: {{ type: "session.compacted", properties: {{ sessionID: "ses_child" }} }} }})
}}
console.log(JSON.stringify(result))
"""
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AGENT_", "MEM_", "CODEX_", "OPENCODE_", "FLEET_"))}
        env.update({"AGENT_HOME": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1",
                    "XDG_STATE_HOME": str(self.state), "MEM_STORE": str(self.root / "mem"),
                    "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")})
        env.update(extra_env or {})
        result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=self.cwd,
                                env=env, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()] \
            if calls.exists() else []
        hooks = [(Path(c["argv"][0]).name, c["argv"][1:], c["stdin"]) for c in recorded if c["argv"]]
        return json.loads(result.stdout.strip().splitlines()[-1]), hooks

    def test_opencode_main_gets_instructions_in_context_and_saves_only_the_new_summary(self):
        data, hooks = self._opencode_fixture("main")
        self.assertEqual(data["output"], {"context": [st.COMPACT_CARD_INSTRUCTIONS], "prompt": "keep this prompt"})
        # identity once per sid (cached), the summary read only after compaction, nothing before it
        self.assertEqual([c[:2] for c in data["calls"]],
                         [["get", "ses_fixture"], ["messages", "ses_fixture"], ["get", "ses_child"]])
        self.assertEqual(data["calls"][1][2]["limit"], 64)
        self.assertTrue(data["calls"][1][3])
        compact = [h for h in hooks if h[0] == "worker-state-hook.py"]
        self.assertEqual([h[1] for h in compact], [["compact-before", "--harness", "opencode"],
                                                   ["compact-after", "--harness", "opencode"]])
        after = json.loads(compact[1][2])
        self.assertEqual((after["sessionID"], after["summary"], after["worker"]),
                         ("ses_fixture", "fresh summary", False))
        # a native child session gets no instructions and no tidy at all
        self.assertEqual(data["childOutput"], {"context": [], "prompt": "child prompt"})
        self.assertTrue(any(h[0] == "mem.py" and "_seen-reset" in h[1] for h in hooks))

    def test_opencode_unknown_identity_keeps_the_ledger_only_compact_record(self):
        data, hooks = self._opencode_fixture("unknown")
        self.assertEqual(data["output"], {"context": [], "prompt": "keep this prompt"})
        self.assertFalse(any(h[0] == "worker-state-hook.py" for h in hooks))
        legacy = [h[1] for h in hooks if h[0] == "session_tidy.py"]
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0][:6], ["hook", "--harness", "opencode", "--event", "compact", "--session-id"])
        self.assertNotIn("--phase", legacy[0])
        self.assertTrue(any(h[0] == "mem.py" and "_seen-reset" in h[1] for h in hooks))

    def test_opencode_sub_session_ledger_failure_still_fails_closed(self):
        data, hooks = self._opencode_fixture(
            "subsession", {"AGENT_DISPATCH_STAGE_AUTHORITY": "0", "FAKE_LEDGER_FAIL": "1"})
        self.assertIn("ledger missing", data["before"])
        self.assertIn("ledger missing", data["after"])
        self.assertEqual(data["output"]["context"], [])
        self.assertEqual(data["calls"], [])
        self.assertEqual([h[1] for h in hooks if h[0] == "worker-state-hook.py"],
                         [["compact-before"], ["compact-after"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
