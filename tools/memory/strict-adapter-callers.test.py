#!/usr/bin/env python3
"""Strict distill caller regressions with private CLI/memory/applier stubs."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]


def write_executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o700)


def frontmatter_mapping(text, section):
    """Parse the flat mapping used by the generated agent frontmatter."""
    lines = text.split("---", 2)[1].splitlines()
    active = False
    result = {}
    for line in lines:
        if line == section + ":":
            active = True
            continue
        if active and line and not line.startswith(" "):
            break
        if active and line.startswith("  "):
            raw_key, raw_value = line.strip().split(":", 1)
            key = json.loads(raw_key) if raw_key.startswith('"') else raw_key
            value = raw_value.strip()
            result[key] = False if value == "false" else value
    return result


class Fixture:
    def __init__(self, base, adapter):
        self.root = base / ("source-" + adapter)
        self.bin = base / ("bin-" + adapter)
        self.home = base / ("home-" + adapter)
        self.project = base / ("project-" + adapter)
        self.store = base / ("store-" + adapter)
        self.advance = base / ("advance-" + adapter)
        self.apply_trace = base / ("apply-" + adapter + ".jsonl")
        self.model_trace = base / ("model-" + adapter + ".json")
        for path in (self.bin, self.home, self.project, self.root / "core",
                     self.root / "utilities", self.root / "tools/memory"):
            path.mkdir(parents=True, exist_ok=True)
        (self.root / "core/CORE.md").write_text("# isolated fixture\n")
        write_executable(self.root / "utilities/memory-store.sh",
                         "#!/bin/sh\nprintf '%s\\n' \"$MEM_STORE\"\n")
        write_executable(self.root / "utilities/model-config.sh", """#!/bin/sh
cat <<'EOF'
CFG_TIER_DEEP_MODEL=fixture-deep
CFG_TIER_DEEP_EFFORT=high
CFG_TIER_LIGHT_MODEL=fixture-light
CFG_TIER_LIGHT_EFFORT=medium
CFG_TIER_MINI_MODEL=fixture-mini
CFG_TIER_MINI_EFFORT=low
CFG_LIFECYCLE_NUDGE=light
CFG_LIFECYCLE_CURATE=deep
EOF
""")
        write_executable(self.root / "utilities/model-worker-governor.py", """#!/usr/bin/env python3
import subprocess, sys
args = sys.argv[1:]
command = args[args.index('--') + 1:]
raise SystemExit(subprocess.run(command).returncode)
""")
        write_executable(self.root / "tools/memory/mem.py", """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args and args[0] == 'distill' and '--capture' in args:
    print(json.dumps({'delta': 'synthetic pending transcript', 'frontier': 'frontier-1'}))
elif args and args[0] == 'distill' and '--advance-capture' in args:
    Path(os.environ['TEST_ADVANCE']).write_text(args[args.index('--advance-capture') + 1])
elif args and args[0] == 'curate-snapshot':
    print('IDS:')
elif args and args[0] == 'curate-artifacts':
    print('(none)')
else:
    raise SystemExit(64)
""")
        write_executable(self.root / "tools/memory/apply-distill-actions.py", """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ['TEST_APPLY_TRACE']).open('a') as fh:
    fh.write(json.dumps(args) + '\\n')
if '--strict-output' not in args:
    raise SystemExit(90)
forced = int(os.environ.get('TEST_APPLY_RC', '0'))
if forced:
    raise SystemExit(forced)
text = Path(args[0]).read_text()
if not text.strip():
    raise SystemExit(0)
try:
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
except Exception:
    raise SystemExit(2)
if rows == [{'action': 'noop'}]:
    raise SystemExit(0)
if all(isinstance(row, dict) for row in rows):
    raise SystemExit(0)
raise SystemExit(2)
""")
        self.env = {
            "HOME": str(self.home), "PATH": str(self.bin) + os.pathsep + os.defpath,
            "AGENT_HOME": str(self.root), "MEM_STORE": str(self.store),
            "XDG_CONFIG_HOME": str(base / "config"),
            "XDG_DATA_HOME": str(base / "data"),
            "XDG_STATE_HOME": str(base / "state"),
            "AGENT_MODEL_GOVERNOR_ROOT": str(base / "governor"),
            "TEST_ADVANCE": str(self.advance),
            "TEST_APPLY_TRACE": str(self.apply_trace),
        }

    def reset(self):
        self.advance.unlink(missing_ok=True)
        self.apply_trace.unlink(missing_ok=True)
        self.model_trace.unlink(missing_ok=True)

    def applied_args(self):
        if not self.apply_trace.exists():
            return []
        return [json.loads(line) for line in self.apply_trace.read_text().splitlines()]


class StrictAdapterCallers(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="strict-adapter-callers-", dir="/var/tmp")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.fixture_counter = 0

    def test_claude_worker_closes_builtin_and_mcp_tools(self):
        bin_dir = self.base / "claude-bin"
        calls = self.base / "claude-args.json"
        prompt = self.base / "prompt.txt"
        prompt.write_text("synthetic prompt")
        write_executable(bin_dir / "claude", "#!" + sys.executable + "\n" + """
import json, os, sys
from pathlib import Path
Path(os.environ['TEST_CLAUDE_ARGS']).write_text(json.dumps(sys.argv[1:]))
""")
        env = {
            **os.environ, "PATH": str(bin_dir) + os.pathsep + os.defpath,
            "AGENT_HOME": str(ROOT), "TEST_CLAUDE_ARGS": str(calls),
            "CLAUDE_MEM_DISTILL_MODEL": "fixture-model", "MEM_DISTILL_TIMEOUT": "5",
        }
        result = subprocess.run(
            ["bash", str(ROOT / "adapters/claude/bin/mem-distill-worker.sh"),
             "increment", "fast-distiller", str(prompt)],
            env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        args = json.loads(calls.read_text())
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(json.loads(args[args.index("--mcp-config") + 1]), {"mcpServers": {}})
        self.assertNotIn("--disallowedTools", args)

    def make_worker(self, adapter):
        fixture = Fixture(self.base, adapter)
        target = fixture.root / f"adapters/{adapter}/bin/distill-worker.sh"
        target.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / f"adapters/{adapter}/bin/distill-worker.sh", target)
        target.chmod(0o700)
        if adapter == "codex":
            write_executable(fixture.bin / "codex", "#!" + sys.executable + "\n" + """
import os, sys
from pathlib import Path
args = sys.argv[1:]
if os.environ.get('TEST_NO_OUTPUT') != '1':
    Path(args[args.index('--output-last-message') + 1]).write_text(os.environ.get('TEST_MODEL_OUTPUT', ''))
raise SystemExit(int(os.environ.get('TEST_MODEL_RC', '0')))
""")
            fixture.env.update({
                "CODEX_DISTILL_ENABLE": "1", "CODEX_DISTILL_APPLY": "1",
                "CODEX_DISTILL_CONTRACT_ACCEPTED": "1", "CODEX_DISTILL_TIMEOUT": "5",
            })
        else:
            write_executable(fixture.bin / "opencode", "#!" + sys.executable + "\n" + """
import os, sys
import json
from pathlib import Path
Path(os.environ['TEST_MODEL_TRACE']).write_text(json.dumps(sys.argv[1:]))
sys.stdin.read()
sys.stdout.write(os.environ.get('TEST_MODEL_OUTPUT', ''))
raise SystemExit(int(os.environ.get('TEST_MODEL_RC', '0')))
""")
            fixture.env.update({
                "OPENCODE_BIN": str(fixture.bin / "opencode"),
                "OPENCODE_DISTILL_ENABLE": "1", "OPENCODE_DISTILL_APPLY": "1",
                "OPENCODE_DISTILL_TIMEOUT": "5", "TEST_MODEL_TRACE": str(fixture.model_trace),
            })
        return fixture, target

    def run_worker(self, fixture, worker, **changes):
        fixture.reset()
        env = {**fixture.env, **{key: str(value) for key, value in changes.items()}}
        return subprocess.run(["sh", str(worker), "sid-1", str(fixture.project), "increment"],
                              env=env, cwd=fixture.project, capture_output=True,
                              text=True, timeout=15)

    def test_codex_and_opencode_strict_apply_controls_frontier(self):
        for adapter in ("codex", "opencode"):
            with self.subTest(adapter=adapter):
                fixture, worker = self.make_worker(adapter)
                for output in ("", "  \n", '{"action":"noop"}\n'):
                    result = self.run_worker(fixture, worker, TEST_MODEL_OUTPUT=output)
                    self.assertEqual(result.returncode, 0, (adapter, result.stdout, result.stderr))
                    self.assertTrue(fixture.advance.exists(), adapter)
                    self.assertIn("--strict-output", fixture.applied_args()[0])
                    if adapter == "opencode":
                        agent = (fixture.store / ".opencode-distill-workdir-v2/.opencode/agent/distiller.md").read_text()
                        self.assertIs(frontmatter_mapping(agent, "tools")["*"], False)
                        self.assertEqual(frontmatter_mapping(agent, "permission")["*"], "deny")
                result = self.run_worker(fixture, worker, TEST_MODEL_OUTPUT="No memory action.\n")
                self.assertEqual(result.returncode, 2, (adapter, result.stdout, result.stderr))
                self.assertFalse(fixture.advance.exists(), adapter)
                self.assertIn("--strict-output", fixture.applied_args()[0])
                result = self.run_worker(
                    fixture, worker, TEST_MODEL_OUTPUT='{"action":"add"}\n', TEST_APPLY_RC=1)
                self.assertEqual(result.returncode, 1, (adapter, result.stdout, result.stderr))
                self.assertFalse(fixture.advance.exists(), adapter)

    def test_codex_missing_output_preserves_frontier(self):
        fixture, worker = self.make_worker("codex")
        result = self.run_worker(fixture, worker, TEST_NO_OUTPUT=1)
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        self.assertIn("model-output-missing", result.stderr)
        self.assertFalse(fixture.advance.exists())
        self.assertEqual(fixture.applied_args(), [])

    def test_opencode_model_failure_returns_status_without_partial_apply(self):
        fixture, worker = self.make_worker("opencode")
        result = self.run_worker(
            fixture, worker, TEST_MODEL_OUTPUT="partial invalid model output\n",
            TEST_MODEL_RC=7)
        self.assertEqual(result.returncode, 7, (result.stdout, result.stderr))
        self.assertEqual(result.stdout, "")
        self.assertFalse(fixture.advance.exists())
        self.assertEqual(fixture.applied_args(), [])

    def test_opencode_ignores_stale_legacy_agent_cache(self):
        fixture, worker = self.make_worker("opencode")
        legacy = fixture.store / ".opencode-distill-workdir/.opencode/agent/distiller.md"
        legacy.parent.mkdir(parents=True)
        legacy_text = "---\ntools:\n  bash: false\n---\nlegacy finite deny\n"
        legacy.write_text(legacy_text)
        result = self.run_worker(fixture, worker, TEST_MODEL_OUTPUT='{"action":"noop"}\n')
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(legacy.read_text(), legacy_text)
        workdir = fixture.store / ".opencode-distill-workdir-v2"
        argv = json.loads(fixture.model_trace.read_text())
        self.assertEqual(Path(argv[argv.index("--dir") + 1]), workdir)
        agent = (workdir / ".opencode/agent/distiller.md").read_text()
        self.assertIs(frontmatter_mapping(agent, "tools")["*"], False)
        self.assertEqual(frontmatter_mapping(agent, "permission")["*"], "deny")

    def make_dispatch(self, kind):
        self.fixture_counter += 1
        case_base = self.base / f"{kind}-case-{self.fixture_counter}"
        fixture = Fixture(case_base, kind)
        if kind == "portable":
            relative = "hooks/mem-distill-dispatch.sh"
        else:
            relative = "adapters/claude/hooks/mem-distill-dispatch.sh"
        dispatch = fixture.root / relative
        dispatch.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / relative, dispatch)
        dispatch.chmod(0o700)
        if kind == "claude":
            (fixture.root / "adapters/claude/utilities").symlink_to("../../utilities")
        worker = case_base / "dispatch-worker"
        write_executable(worker, "#!" + sys.executable + "\n" + """
import os, sys
sys.stdout.write(os.environ.get('TEST_MODEL_OUTPUT', ''))
raise SystemExit(int(os.environ.get('TEST_MODEL_RC', '0')))
""")
        fixture.env.update({
            "MEM_DISTILL_ENABLE": "1", "MEM_DISTILL_WORKER": str(worker),
            "MEM_PY": str(fixture.root / "tools/memory/mem.py"),
            "MEM_APPLIER": str(fixture.root / "tools/memory/apply-distill-actions.py"),
            "MODEL_WORKER_GOVERNOR": str(fixture.root / "utilities/model-worker-governor.py"),
            "MEM_DISTILL_MAX_CONCURRENT": "1", "MEM_DISTILL_MAX_STARTS": "1",
        })
        return fixture, dispatch

    def run_claude_dispatch(self, fixture, dispatch, output, **changes):
        fixture.reset()
        sid = changes.pop("sid", "claude-sid")
        preseed_strike = int(changes.pop("preseed_strike", 2))
        fail_count = fixture.store / (".distill-fail-" + sid)
        fail_count.parent.mkdir(parents=True, exist_ok=True)
        if preseed_strike:
            fail_count.write_text(str(preseed_strike) + "\n")
        env = {**fixture.env, "TEST_MODEL_OUTPUT": output,
               **{key: str(value) for key, value in changes.items()}}
        result = subprocess.run(["bash", str(dispatch), "distill", sid, str(fixture.project)],
                                env=env, cwd=fixture.project, capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        lock = fixture.store / (".distill-lock-" + sid)
        deadline = time.monotonic() + 8
        while lock.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(lock.exists(), "detached Claude fixture did not finish")
        return fail_count

    def test_dispatchers_preserve_frontier_and_strikes_on_strict_failure(self):
        for dispatcher in ("portable", "claude"):
            for output, apply_rc, failure_kind in (
                    ("No memory action.\n", 0, "apply-invalid-output"),
                    ('{"action":"add"}\n', 1, "apply-failed")):
                with self.subTest(dispatcher=dispatcher, failure=failure_kind):
                    fixture, dispatch = self.make_dispatch(dispatcher)
                    fail_count = self.run_claude_dispatch(
                        fixture, dispatch, output, TEST_APPLY_RC=apply_rc)
                    self.assertFalse(fixture.advance.exists())
                    self.assertEqual(fail_count.read_text().strip(), "2")
                    self.assertIn("--strict-output", fixture.applied_args()[0])
                    self.assertIn(failure_kind,
                                  (fixture.store / ".distill-failures.log").read_text())

    def test_dispatchers_advance_no_action_but_never_apply_failed_partial(self):
        for dispatcher in ("portable", "claude"):
            for output in ("", " \n", '{"action":"noop"}\n'):
                with self.subTest(dispatcher=dispatcher, output=repr(output)):
                    fixture, dispatch = self.make_dispatch(dispatcher)
                    fail_count = self.run_claude_dispatch(fixture, dispatch, output)
                    self.assertTrue(fixture.advance.exists())
                    self.assertFalse(fail_count.exists())
                    self.assertIn("--strict-output", fixture.applied_args()[0])
            fixture, dispatch = self.make_dispatch(dispatcher)
            fail_count = self.run_claude_dispatch(
                fixture, dispatch, "No memory action.\n", TEST_MODEL_RC=1,
                preseed_strike=0)
            self.assertFalse(fixture.advance.exists())
            self.assertEqual(fixture.applied_args(), [])
            self.assertEqual(fail_count.read_text().strip(), "1")


if __name__ == "__main__":
    unittest.main()
