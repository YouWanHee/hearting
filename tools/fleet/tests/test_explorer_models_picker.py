"""MODELS keeps every catalog model choosable: filter, scroll, keys, click, and Apply of the last one."""
import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.models import model_matches
from tools.fleet.explorer.state import NavState, models_finding, reduce
from tools.fleet.explorer.view_model import ExplorerViewModel, load_fixture

FIXTURES = Path(__file__).parent / "fixtures" / "explorer"
CATALOG = ([f"opencode-go/go-model-{index:02d}" for index in range(32)]
           + [f"openai/oa-model-{index:02d}" for index in range(53)])


def _vm():
    values = load_fixture(FIXTURES / "view_model.json").to_mapping()
    models = dict(values["models"])
    models["model_choices"] = dict(models["model_choices"], opencode=list(CATALOG))
    models["catalog_sources"] = {"opencode": {"state": "available", "source": "/cache/models.json",
                                              "observed_at": 1}}
    values["models"] = models
    return ExplorerViewModel.from_mapping(values)


def _state(**folds):
    base = {"ui:sel": "balanced/opencode"}
    base.update(folds)
    return NavState(view="MODELS", title="MODELS", folds=tuple(sorted(base.items())))


def _picks(frame):
    return [hit for hit in frame.hits if hit.action[:2] == ("ui", "model")]


def _step(vm, state, event, cols):
    frame = draw(vm, state, cols, 70, beat=1)
    state, effects = reduce(state, event, frame)
    return state, effects, draw(vm, state, cols, 70, beat=1)


class ModelPicker(unittest.TestCase):
    def test_every_catalog_model_is_reachable_and_the_last_one_applies(self):
        vm = _vm()
        for cols in (120, 180):
            state = _state()
            frame = draw(vm, state, cols, 70, beat=1)
            find = next(hit for hit in frame.hits if hit.action[0] == "models.find")
            state, _effects = reduce(state, {"type": "click", "action": find.action}, frame)
            self.assertTrue(models_finding(state))
            seen = set()
            frame = draw(vm, state, cols, 70, beat=1)
            for _ in range(20):
                seen.update(hit.action[2] for hit in _picks(frame))
                state, _effects, frame = _step(vm, state, {"type": "key", "key": "pagedown"}, cols)
            self.assertEqual(seen, set(CATALOG), cols)
            state, _effects, frame = _step(vm, state, {"type": "key", "key": "end"}, cols)
            state, _effects, frame = _step(vm, state, {"type": "key", "key": "enter"}, cols)
            self.assertEqual(state.fold_map()["ui:model"], CATALOG[-1])
            actions = {hit.action for hit in frame.hits if hit.action[:1] == ("models.apply",)}
            self.assertIn(("models.apply", "opencode", "profile", "balanced", CATALOG[-1], "xhigh", True),
                          actions)
            self.assertIn(("models.apply", "opencode", "profile", "balanced", CATALOG[-1], "xhigh", False),
                          actions)

    def test_typed_filter_wheel_and_click_choose_a_model(self):
        vm = _vm()
        state = _state(**{"ui:mfocus": "1"})
        frame = draw(vm, state, 120, 70, beat=1)
        for char in "oa 52":
            state, _effects, frame = _step(vm, state, {"type": "key", "key": "text:" + char}, 120)
        self.assertEqual([hit.action[2] for hit in _picks(frame)], ["openai/oa-model-52"])
        state, _effects, frame = _step(vm, state, {"type": "key", "key": "backspace"}, 120)
        self.assertEqual(state.fold_map()["ui:mfind"], "oa 5")
        state, _effects, frame = _step(vm, state, {"type": "key", "key": "escape"}, 120)
        self.assertFalse(models_finding(state))
        self.assertEqual(state.fold_map()["ui:mfind"], "oa 5")

        state = _state()
        frame = draw(vm, state, 120, 70, beat=1)
        row = _picks(frame)[0]
        state, _effects = reduce(state, {"type": "wheel", "x": row.x0, "y": row.y, "direction": -1}, frame)
        frame = draw(vm, state, 120, 70, beat=1)
        self.assertEqual(_picks(frame)[0].action[2], CATALOG[3])
        clicked = _picks(frame)[-1]
        state, _effects = reduce(state, {"type": "mouse", "phase": "press", "button": 0,
                                         "x": clicked.x0 + 2, "y": clicked.y}, frame)
        self.assertEqual(state.fold_map()["ui:model"], clicked.action[2])

    def test_keys_stay_shared_until_the_list_is_focused_and_a_new_cell_resets_it(self):
        vm = _vm()
        state = _state(**{"ui:mfind": "go", "ui:mfocus": "1"})
        frame = draw(vm, state, 120, 70, beat=1)
        moved, _effects = reduce(state, {"type": "key", "key": "down", "order": ["a", "b"]}, frame)
        self.assertEqual(moved.scope, "-")
        idle = _state()
        moved, _effects = reduce(idle, {"type": "key", "key": "down", "order": ["a", "b"]},
                                 draw(vm, idle, 120, 70, beat=1))
        self.assertEqual(moved.scope, "a")
        cell = next(hit for hit in frame.hits if hit.action == ("ui", "sel", "balanced/codex"))
        state, _effects = reduce(state, {"type": "click", "action": cell.action}, frame)
        self.assertNotIn("ui:mfind", state.fold_map())
        self.assertNotIn("ui:mfocus", state.fold_map())

    def test_profile_value_without_provider_matches_its_catalog_entry(self):
        vm = _vm()
        values = vm.to_mapping()
        models = dict(values["models"])
        models["profile_map"] = dict(models["profile_map"],
                                     opencode=dict(models["profile_map"]["opencode"],
                                                   balanced=["go-model-05", "xhigh"]))
        values["models"] = models
        vm = ExplorerViewModel.from_mapping(values)
        state = _state(**{"ui:model": "opencode-go/go-model-05"})
        frame = draw(vm, state, 120, 70, beat=1)
        self.assertFalse([hit for hit in frame.hits if hit.action[:1] == ("models.apply",)
                          and hit.action[2] == "profile"])

    def test_filter_matches_every_word_case_insensitively(self):
        self.assertEqual(model_matches(CATALOG, "OA 1"),
                         tuple(item for item in CATALOG if item.startswith("openai/") and "1" in item))
        self.assertEqual(model_matches(CATALOG, ""), tuple(CATALOG))

    def test_last_model_dry_run_and_apply_change_only_a_temporary_home(self):
        from tools.fleet.explorer.runtime import ExplorerRuntime

        vm = _vm()
        state = _state(**{"ui:model": CATALOG[-1]})
        frame = draw(vm, state, 120, 70, beat=1)
        dry, apply = sorted((hit.action for hit in frame.hits if hit.action[:1] == ("models.apply",)
                             and hit.action[2] == "profile"), key=lambda action: not action[-1])
        models = vm.models
        with TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_home = root / "opencode-runtime"
            environment = {
                "AGENT_HOME": str(Path(__file__).resolve().parents[3]),
                "HOME": str(root), "CLAUDE_CONFIG_DIR": str(root / "claude"),
                "CODEX_HOME": str(root / "codex"), "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"), "XDG_STATE_HOME": str(root / "state"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "HEARTING_MODEL_CONFIG_HOME": str(runtime_home), "FLEET_DEMO": "",
            }
            with mock.patch.dict(os.environ, environment):
                config = runtime_home / "agent-config" / "models.conf"
                config.parent.mkdir(parents=True)
                shutil.copyfile(Path(environment["AGENT_HOME"]) / "adapters/opencode/config/models.conf",
                                config)
                before = config.read_bytes()
                runtime = ExplorerRuntime()
                planned = runtime.apply_effects([dry], SimpleNamespace(models=models, ordering={}))
                self.assertEqual(planned["status"], "planned")
                self.assertEqual(config.read_bytes(), before)
                applied = runtime.apply_effects([apply], SimpleNamespace(models=models, ordering={}))
                self.assertEqual(applied["status"], "changed")
                self.assertIn(CATALOG[-1], config.read_text())


if __name__ == "__main__":
    unittest.main()
