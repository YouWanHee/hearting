import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.fleet.herdr_projection import compose
from tools.fleet.session_handle import display_name, minted_tag

ROOT = next(parent for parent in Path(__file__).resolve().parents
            if (parent / "adapters/codex").is_dir())
STATUSLINE = ROOT / "adapters/claude/statusline.sh"
HELPER = ROOT / "adapters/claude/tools/fleet/session_handle.py"


class RuntimeProjectionTest(unittest.TestCase):
    # Whoever runs this suite may themselves BE a registered worker (a dispatched
    # reviewer, the title refresher). Those markers gate the projection, so leaving them
    # inherited makes the result depend on who ran the test.
    _WORKER_ENV = ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                   "OPENCODE_DISPATCH_SLUG", "FLEET_TITLE_REFRESH", "MEM_DISTILL")

    def env(self, root, **overrides):
        env = os.environ.copy()
        for name in self._WORKER_ENV:
            env.pop(name, None)
        env.update({"AGENT_HOME": str(root / "agent"), "HOME": str(root / "home"),
                    "CODEX_HOME": str(root / "codex"), "FLEET_TITLE_STATE_DIR": str(root / "titles"),
                    "PYTHONDONTWRITEBYTECODE": "1"})
        env.update(overrides)
        return env

    def statusline(self, root, sid, title, helper=True):
        path = Path(self.env(root)["AGENT_HOME"]) / "tools/fleet/session_handle.py"
        if helper:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(HELPER, path)
        else:
            path.unlink(missing_ok=True)
        return subprocess.run([str(STATUSLINE)], input=json.dumps({"cwd": str(root), "session_id": sid, "session_name": title}), text=True, capture_output=True, env=self.env(root))

    def stub(self, root):
        bindir, log = root / "bin", root / "herdr.jsonl"
        bindir.mkdir(exist_ok=True)
        path = bindir / "herdr"
        path.write_text("#!/usr/bin/env python3\nimport json,os,sys,time\nwith open(os.environ['HERDR_LOG'],'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\nif os.environ.get('HERDR_MODE')=='timeout': time.sleep(.8)\nraise SystemExit(int(os.environ.get('HERDR_EXIT','0')))\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return bindir, log

    def project(self, root, sid="abcdefgh-123", mode="ok", worker=False, title="title",
                formatter=None, harness="codex"):
        bindir, log = self.stub(root)
        sidecar = root / "titles" / harness / (sid + ".json")
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(title if title.startswith("{") else json.dumps({"title": title}))
        log.write_text("")
        env = self.env(root)
        env.update({"PATH": str(bindir) + os.pathsep + env["PATH"], "HERDR_PANE_ID": "pane-7", "HERDR_LOG": str(log), "HERDR_MODE": mode, "HERDR_EXIT": "7" if mode == "nonzero" else "0"})
        if formatter is not None:
            env["HERDR_SESSION_METADATA_FORMATTER"] = str(formatter)
        if harness == "codex":
            # Through the Codex adapter hook, which is the entry point its two lifecycle
            # hooks call — proving the wrapper still reaches the shared projector.
            code = ("import sys;sys.path.insert(0,%r);from adapters.codex.hooks.herdr_session_projection import project;assert project({},%r,worker=%r)"
                    % (str(ROOT), sid, worker))
        else:
            code = ("import sys;sys.path.insert(0,%r);from tools.fleet.herdr_projection import project;assert project(%r,%r,worker=%r)"
                    % (str(ROOT), harness, sid, worker))
        result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True)
        rows = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
        return result, rows

    def claude_hook(self, root, sid, payload=None, **env_overrides):
        """The hearting-owned Claude hook — herdr's own integration reports state and the
        session id, and never a title, so a Claude pane header had nothing on it."""
        bindir, log = self.stub(root)
        env = self.env(root, **env_overrides)
        env.update({"PATH": str(bindir) + os.pathsep + env["PATH"], "HERDR_PANE_ID": "pane-7",
                    "HERDR_LOG": str(log), "HERDR_MODE": "ok", "HERDR_EXIT": "0"})
        log.write_text("")
        body = json.dumps(payload if payload is not None else {"session_id": sid})
        result = subprocess.run([sys.executable, str(ROOT / "hooks/herdr-session-projection.py")],
                                input=body, text=True, capture_output=True, env=env)
        rows = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
        return result, rows

    def test_claude_positive_missing_control_and_long_titles(self):
        """F-99 — statusline shows the canonical name with zero sid8 (`CL/<sid8>`)."""
        with tempfile.TemporaryDirectory() as td:
            root, sid = Path(td), "abcdefgh-claude"
            self.assertIn("My Task", self.statusline(root, sid, "My Task").stdout)
            missing = self.statusline(root, sid, "My Task", helper=False)
            self.assertEqual(missing.returncode, 0)
            self.assertNotIn("My Task", missing.stdout)
            self.assertNotIn("CL/abcdefgh", missing.stdout)
            self.assertIn("A B", self.statusline(root, sid, "A\nB\x00").stdout)
            long = self.statusline(root, sid, "가" * 100).stdout
            self.assertNotIn("CL/abcdefgh", long)
            display = next(x for x in long.split(" │ ") if "가" in x)
            self.assertLess(display.count("가"), 100)
            self.assertIn("…", display)

    def test_codex_herdr_exact_argv_and_failures(self):
        sid = "abcdefgh-codex"
        agent = "[%s] codex" % minted_tag(sid)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, rows = self.project(root, sid, title="A title")
            self.assertEqual(rows, [["pane","report-agent-session","pane-7","--source","herdr:codex","--agent","codex","--agent-session-id",sid], ["pane","report-metadata","pane-7","--source","herdr:codex","--display-agent",agent,"--title","A title"]])
            _, rows = self.project(root, sid, title="{")
            self.assertNotIn("--title", rows[1])
            _, rows = self.project(root, sid, title="가" * 60)
            projected = rows[1][rows[1].index("--title") + 1]
            self.assertEqual(projected, "가" * 23 + "…")
            for mode in ("nonzero", "timeout"):
                _, rows = self.project(root, sid, mode=mode)
                self.assertEqual(len(rows), 2)
            before = root / "codex/config.toml"
            self.assertFalse(before.exists())
            _, rows = self.project(root, sid, worker=True)
            self.assertEqual(rows, [])
            self.assertFalse(before.exists())
            self.assertTrue(all(row[0] == "pane" for row in rows))

    def test_codex_private_metadata_formatter_and_fail_soft_fallback(self):
        sid = "abcdefgh-codex"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            formatter = root / "formatter"
            formatter.write_text(
                "#!/usr/bin/env python3\n"
                "import argparse,json\n"
                "p=argparse.ArgumentParser();p.add_argument('--harness');"
                "p.add_argument('--session-id');p.add_argument('--summary');a=p.parse_args()\n"
                "print(json.dumps({'display_agent':a.harness,'title':a.summary}))\n"
            )
            formatter.chmod(formatter.stat().st_mode | stat.S_IXUSR)
            _, rows = self.project(root, sid, title="Session summary", formatter=formatter)
            # A personal formatter renames the middle harness word only: the `[tag]` badge
            # and the steward mark are hearting's, composed OUTSIDE the formatter result,
            # so a formatter cannot quietly delete the two things that identify a session.
            self.assertEqual(rows[1][6:],
                             ["[%s] codex" % minted_tag(sid), "--title", "Session summary"])

            formatter.write_text("#!/usr/bin/env python3\nprint('{')\n")
            formatter.chmod(formatter.stat().st_mode | stat.S_IXUSR)
            _, rows = self.project(root, sid, title="Fallback", formatter=formatter)
            self.assertEqual(rows[1][6:],
                             ["[%s] codex" % minted_tag(sid), "--title", "Fallback"])

            formatter.write_text("#!/usr/bin/env python3\nimport time;time.sleep(.5)\n")
            formatter.chmod(formatter.stat().st_mode | stat.S_IXUSR)
            _, rows = self.project(root, sid, title="Timeout", formatter=formatter)
            self.assertEqual(rows[1][6:],
                             ["[%s] codex" % minted_tag(sid), "--title", "Timeout"])

    def test_codex_absent_command_is_fail_soft(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bindir, log = self.stub(root)
            (bindir / "herdr").unlink()
            env = self.env(root)
            env.update({"PATH": str(bindir), "HERDR_PANE_ID": "pane-7"})
            code = ("import sys;sys.path.insert(0,%r);from adapters.codex.hooks.herdr_session_projection import project;assert project({},'abcdefgh-123')" % str(ROOT))
            self.assertEqual(subprocess.run([sys.executable, "-c", code], env=env).returncode, 0)
            self.assertFalse(log.exists())

    def test_shared_input_vectors_match_fleet_claude_and_codex(self):
        """F-99e — statusline and the pane header carry the same one title for one
        session, with zero sid8 handles anywhere."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for harness, sid, title in (("claude","abcdefgh-claude","Task"),("codex","abcdefgh-codex","Task")):
                expected = display_name(harness, sid, runtime_name=None, registry_name=None,
                                        title=title, slug=None, cwd=None)
                self.assertEqual(expected, title)
                if harness == "claude":
                    self.assertIn(expected, self.statusline(root, sid, title).stdout)
                else:
                    _, rows = self.project(root, sid, title=title)
                    self.assertEqual(rows[1][-1], title)

    def test_pane_header_order_is_number_then_harness_then_steward(self):
        """User-fixed 2026-09-09 format `[번호] 하네스 (⚑) 요약`, one shape everywhere."""
        for harness in ("claude", "codex", "opencode"):
            self.assertEqual(compose(harness, "s", tag="3a", steward=False, title="사이클"),
                             ("[3a] %s" % harness, "사이클"))
            self.assertEqual(compose(harness, "s", tag="b0", steward=True, title="감독")[0],
                             "[b0] %s ⚑" % harness)
        # No tag resolves: the badge slot is dropped rather than shown empty or faked.
        self.assertEqual(compose("claude", "s", tag=None, steward=False, title="t")[0],
                         "claude")
        # Budgets are herdr's; a long title is clipped, never wrapped into the agent cell.
        agent, title = compose("codex", "s", tag="3a", steward=True, title="가" * 80)
        self.assertEqual(agent, "[3a] codex ⚑")
        self.assertLess(len(title), 80)

    def test_claude_hook_reports_metadata_and_leaves_the_session_id_to_herdr(self):
        """`report-agent-session` stays herdr's own integration's job — two sources
        claiming one pane's agent session would race their `seq` values."""
        with tempfile.TemporaryDirectory() as td:
            root, sid = Path(td), "abcdefgh-claude"
            sidecar = root / "titles/claude" / (sid + ".json")
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(json.dumps({"title": "Claude pane title"}))
            result, rows = self.claude_hook(root, sid)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertEqual([row[1] for row in rows], ["report-metadata"])
            self.assertEqual(rows[0][4:], ["herdr:claude", "--display-agent", "claude",
                                           "--title", "Claude pane title"])

    def test_claude_hook_skips_a_registered_worker(self):
        with tempfile.TemporaryDirectory() as td:
            root, sid = Path(td), "abcdefgh-claude"
            for marker in ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_DEPTH"):
                value = "worker" if marker == "AGENT_SESSION_ROLE" else "2"
                _, rows = self.claude_hook(root, sid, **{marker: value})
                self.assertEqual(rows, [], marker)

    def test_claude_hook_is_fail_soft_on_every_bad_input(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for payload in ({}, {"session_id": ""}, {"session_id": 7}):
                result, rows = self.claude_hook(root, "unused", payload=payload)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(rows, [])
            proc = subprocess.run([sys.executable, str(ROOT / "hooks/herdr-session-projection.py")],
                                  input="{bad", text=True, capture_output=True,
                                  env=self.env(root))
            self.assertEqual(proc.returncode, 0)

    def test_every_harness_skips_a_registered_worker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for harness in ("claude", "codex", "opencode"):
                _, rows = self.project(root, "abcdefgh-%s" % harness, harness=harness,
                                       worker=True)
                self.assertEqual(rows, [], harness)

    def test_opencode_projects_through_the_same_shared_surface(self):
        sid = "abcdefgh-opencode"
        with tempfile.TemporaryDirectory() as td:
            _, rows = self.project(Path(td), sid, harness="opencode", title="OC task")
            self.assertEqual(rows[-1][4:], ["herdr:opencode", "--display-agent",
                                            "[%s] opencode" % minted_tag(sid),
                                            "--title", "OC task"])

    def test_statusline_carries_the_title_only(self):
        """2026-09-09 — the `[46]` badge moved to the pane header, and a name that is just
        the folder is not a title (the `📁 <dir>` segment already says it)."""
        with tempfile.TemporaryDirectory() as td:
            root, sid = Path(td), "abcdefgh-claude"
            import re
            strip = lambda s: re.sub(r"\x1b\[[0-9;]*m", "", s)
            out = strip(self.statusline(root, sid, "Real title").stdout)
            segment = next(s for s in out.split("│") if "Real title" in s)
            self.assertEqual(segment.strip(), "Real title")   # title only, no `[46]` badge
            # A name that is only the folder is not a title: `📁 <dir>` already says it.
            bare = strip(self.statusline(root, sid, root.name).stdout)
            self.assertEqual(bare.count(root.name), 1)

    def test_sessionstart_worker_gating_and_json_contract(self):
        env = {**os.environ, "AGENT_SESSION_ROLE": "worker", "HERDR_PANE_ID": "pane-7"}
        result = subprocess.run([sys.executable, str(ROOT / "adapters/codex/hooks/sessionstart-lifecycle.py")], input=json.dumps({"session_id":"abcdefgh-123"}), text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0)
        if result.stdout.strip(): json.loads(result.stdout)

    def test_statusline_malformed_input_exits_zero(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(subprocess.run([str(STATUSLINE)], input="{bad", text=True, capture_output=True, env={**os.environ, "AGENT_HOME": td}).returncode, 0)


if __name__ == "__main__":
    unittest.main()
