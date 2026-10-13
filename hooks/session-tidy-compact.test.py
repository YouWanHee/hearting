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

    def test_lock_competition_is_silent_at_the_hook_boundary(self):
        args = SimpleNamespace(harness="claude", session_id="sid-main", event="compact",
                               source=None, transcript=None, cwd=str(self.cwd), reread=False,
                               phase="after")
        with st.seat_lock(self.seat.key):
            with mock.patch.object(st, "resolve_seat", return_value=self.seat), \
                 mock.patch("sys.stdin", io.StringIO('{"summary":"summary"}')), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(st.cmd_hook(args), 0)
                self.assertEqual(output.getvalue(), "")
        self.assertEqual(self.enqueue_mock.call_count, 0)

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
                                 capture_output=True, timeout=8)
        self.assertEqual((partial.returncode, partial.stdout), (0, ""))
        timed = subprocess.run([sys.executable, str(wrapper), "compact-before", "--harness", "claude"],
                               env={**env, "FAKE_COMPACT_MODE": "timeout"}, text=True,
                               capture_output=True, timeout=8)
        self.assertEqual((timed.returncode, timed.stdout), (0, ""))
        sub = subprocess.run([sys.executable, str(wrapper), "compact-after", "--harness", "claude"],
                             env={**env, "AGENT_DISPATCH_STAGE_AUTHORITY": "0"}, text=True,
                             input="{}", capture_output=True, timeout=8)
        self.assertEqual(sub.returncode, 2)
        self.assertEqual(sub.stdout, "partial ledger output\n")

    def test_opencode_uses_v1_summary_lookup_context_not_prompt_and_passes_only_new_summary(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        captured = self.root / "after.json"
        child_captured = self.root / "child-after.json"
        bridge_log = self.root / "bridge.log"
        shim = fake_bin / "python3"
        shim.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {shlex.quote(str(bridge_log))}\n"
            f"case \"$1\" in */worker-state-hook.py) case \"$2\" in\n"
            "compact-before) "
            "printf '%s\\n' \"Summarize this session using exactly these six card fields: work in progress; waiting decisions; next actions; related references; today's work; user instructions. Keep the summary concise and preserve actionable details.\"; exit 0 ;;\n"
            f"compact-after) if [ -e {shlex.quote(str(captured))} ]; then cat > {shlex.quote(str(child_captured))}; else cat > {shlex.quote(str(captured))}; fi; exit 0 ;;\nesac; exit 0 ;;\nesac\n"
            f"exec {shlex.quote(sys.executable)} \"$@\"\n", encoding="utf-8")
        shim.chmod(0o700)
        plugin = ROOT / "adapters/opencode/plugins/hearting-guards.js"
        script = f'''import {{ AgentHarnessGuards }} from {json.dumps(plugin.as_uri())}
const sid = "ses_fixture"
const now = Date.now()
let reads = 0
const old = {{ info: {{ id: "old-summary", sessionID: sid, role: "assistant", summary: true, finish: "stop", time: {{ created: now - 5000 }} }}, parts: [{{ type: "text", text: "old" }}] }}
const fresh = {{ info: {{ id: "new-summary", sessionID: sid, role: "assistant", summary: true, finish: "stop", time: {{ created: Date.now() }} }}, parts: [{{ type: "text", text: "fresh summary" }}, {{ type: "image", text: "ignored" }}] }}
const calls = []
const ctx = {{ directory: {json.dumps(str(self.cwd))}, worktree: {json.dumps(str(self.cwd))}, client: {{ session: {{
  get: async (request) => {{ calls.push(["get", request]); return {{ data: {{ id: request.path.id, parentID: request.path.id === "ses_child" ? sid : null }} }} }},
  messages: async (request) => {{
    calls.push(["messages", request])
    if (reads++ === 0) return {{ data: [old] }}
    fresh.info.time.created = Date.now()
    return {{ data: [old, fresh] }}
  }},
}} }} }}
const plugin = await AgentHarnessGuards(ctx)
const output = {{ context: [], prompt: "keep this prompt" }}
await plugin["experimental.session.compacting"]({{ sessionID: sid }}, output)
await plugin.event({{ event: {{ type: "session.compacted", properties: {{ sessionID: sid }} }} }})
const childOutput = {{ context: [], prompt: "child prompt" }}
await plugin["experimental.session.compacting"]({{ sessionID: "ses_child" }}, childOutput)
await plugin.event({{ event: {{ type: "session.compacted", properties: {{ sessionID: "ses_child" }} }} }})
console.log(JSON.stringify({{ output, calls, childOutput }}))
'''
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AGENT_", "MEM_", "CODEX_", "OPENCODE_", "FLEET_"))}
        env.update({"AGENT_HOME": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1",
                    "XDG_STATE_HOME": str(self.state), "MEM_STORE": str(self.root / "mem"),
                    "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                    "COMPACT_CAPTURE": str(captured)})
        result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=self.cwd,
                                env=env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertIn(st.COMPACT_CARD_INSTRUCTIONS, data["output"]["context"],
                      {**data, "bridge": bridge_log.read_text(encoding="utf-8") if bridge_log.exists() else "missing",
                       "stderr": result.stderr})
        self.assertEqual(data["output"]["prompt"], "keep this prompt")
        self.assertEqual([item[0] for item in data["calls"]], ["get", "messages", "messages", "get"])
        self.assertTrue(all(call[1].get("path", {}).get("id") == "ses_fixture"
                            for call in data["calls"][:3]))
        self.assertEqual(data["calls"][3][1]["path"]["id"], "ses_child")
        after = json.loads(captured.read_text(encoding="utf-8"))
        self.assertEqual(after["summary"], "fresh summary")
        self.assertEqual(after["worker"], False)
        child_after = json.loads(child_captured.read_text(encoding="utf-8"))
        self.assertEqual(child_after["worker"], True)
        self.assertEqual(child_after.get("summary", ""), "")
        self.assertEqual(data["childOutput"], {"context": [], "prompt": "child prompt"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
