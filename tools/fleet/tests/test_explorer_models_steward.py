"""SUPERVISION edits: current interval and per-repo limit, preview, Apply, failure, reread."""
import os
import shutil
import stat
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tools.fleet.explorer import focus
from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.runtime import ExplorerRuntime
from tools.fleet.explorer.state import NavState, reduce
from tools.fleet.explorer.view_model import ExplorerViewModel, load_fixture

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).parent / "fixtures" / "explorer"


class StewardEditor(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="fleet-steward-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        environment = {"HOME": str(root), "XDG_CONFIG_HOME": str(root / "config"),
                       "XDG_STATE_HOME": str(root / "state"), "XDG_DATA_HOME": str(root / "data"),
                       "XDG_CACHE_HOME": str(root / "cache"), "CLAUDE_CONFIG_DIR": str(root / "claude"),
                       "CODEX_HOME": str(root / "codex"), "AGENT_HOME": str(ROOT), "FLEET_DEMO": "",
                       "TMPDIR": str(root)}
        patch = mock.patch.dict(os.environ, environment)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("DISPATCH_DEFAULTS_CONFIG", None)
        self.path = root / "config" / "hearting" / "dispatch-defaults.yaml"
        self.path.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "profiles" / "dispatch-defaults.yaml", self.path)
        self.original = self.path.read_bytes()

    def vm(self):
        values = load_fixture(FIXTURES / "view_model.json").to_mapping()
        values["models"] = dict(values["models"], steward=focus.read_model_config()["steward"])
        return ExplorerViewModel.from_mapping(values)

    @staticmethod
    def click(state, frame, action):
        hit = next(hit for hit in frame.hits if hit.action == action)
        return reduce(state, {"type": "mouse", "phase": "press", "button": 0, "x": hit.x0, "y": hit.y}, frame)

    def test_current_values_preview_apply_and_reread(self):
        vm = self.vm()
        self.assertEqual(vm.models["steward"]["values"],
                         {"check_interval_seconds": 3600, "max_supervisors_per_repo": 2})
        state = NavState(view="MODELS", title="MODELS")
        frame = draw(vm, state, 160, 120, beat=1)
        text = "\n".join(frame.text_rows())
        self.assertIn("SUPERVISION", text)
        self.assertIn("[ 1h ]", text)
        self.assertIn("· / 2 supervising", text)
        self.assertIn("affects new checks and new supervisor grants only", text)
        state, _effects = self.click(state, frame, ("ui", "steward:check_interval_seconds", "1800"))
        frame = draw(vm, state, 160, 120, beat=1)
        state, _effects = self.click(state, frame, ("ui", "steward:max_supervisors_per_repo", "3"))
        frame = draw(vm, state, 160, 120, beat=1)
        self.assertIn("check every 1h → 30m  ·  per repo 2 → 3", "\n".join(frame.text_rows()))
        payload = (("steward.check_interval_seconds", "1800"), ("steward.max_supervisors_per_repo", "3"))
        state, effects = self.click(state, frame, ("alloc.apply", payload, True))
        self.assertEqual(effects, (("alloc.apply", payload, True),))

        runtime = ExplorerRuntime()
        planned = runtime.apply_effects(effects, vm)
        self.assertEqual(planned["status"], "planned")
        self.assertEqual(self.path.read_bytes(), self.original)
        applied = runtime.apply_effects([("alloc.apply", payload, False)], vm)
        self.assertEqual(applied["status"], "changed")
        self.assertEqual(Path(str(self.path) + ".bak").read_bytes(), self.original)
        self.assertEqual(applied["models"]["steward"]["values"],
                         {"check_interval_seconds": 1800, "max_supervisors_per_repo": 3})
        changed = [line.split("#", 1)[0].rstrip() for line in self.path.read_text().splitlines()
                   if line not in self.original.decode().splitlines()]
        self.assertEqual(changed, ["  check_interval_seconds: 1800", "  max_supervisors_per_repo: 3"])

        vm = replace(vm, models=applied["models"])
        state = replace(state, status=applied["message"])
        text = "\n".join(draw(vm, state, 160, 120, beat=1).text_rows())
        self.assertNotIn("[ dry-run ]  [ apply ]", text.split("SUPERVISION", 1)[1])
        self.assertIn("steward: set check_interval_seconds: 3600 -> 1800", text)

    def test_out_of_range_and_write_failure_are_shown_and_leave_the_file(self):
        vm = self.vm()
        runtime = ExplorerRuntime()
        for payload in ((("steward.check_interval_seconds", "300"),),
                        (("steward.max_supervisors_per_repo", "9"),),
                        (("steward.max_supervisors_per_repo", "two"),)):
            bad = runtime.apply_effects([("alloc.apply", payload, False)], vm)
            self.assertEqual(bad["status"], "invalid-arguments")
            self.assertTrue(bad["message"].startswith("steward: invalid-arguments:"))
        self.assertEqual(self.path.read_bytes(), self.original)
        state = NavState(view="MODELS", title="MODELS", status=bad["message"])
        frame = draw(vm, state, 160, 120, beat=1)
        y, x = next((index, row.find("steward: invalid-arguments"))
                    for index, row in enumerate(frame.text_rows()) if "steward: invalid-arguments" in row)
        self.assertEqual(frame.cells[y][x][1], "g_blocked")

        self.path.parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(self.path.parent.chmod, stat.S_IRWXU)
        failed = runtime.apply_effects([("alloc.apply", (("steward.max_supervisors_per_repo", "1"),), False)], vm)
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["message"].startswith("steward: failed:"))
        self.assertEqual(self.path.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
