import json
import os
import tempfile
import unittest

from tools.fleet.collectors.dispatch import _parse_pipe_meta
from tools.fleet.explorer.output import cell_width
from tools.fleet.tests.explorer_fixture import load_screens, load_source, materialize


class ExplorerFixtureTests(unittest.TestCase):
    def test_source_has_canonical_evidence_families_and_redacted_paths(self):
        source = load_source()
        for key in ("snapshot", "decisions", "registry", "routes", "cycles", "markers", "obligations", "events"):
            self.assertIn(key, source)
            self.assertTrue(source[key], key)
        self.assertEqual(source["path_root"], "$FIXTURE_ROOT")
        rendered = str(source)
        self.assertNotIn("/home/nas/user/Uihyeop/", rendered)
        self.assertNotIn("/home/Uihyeop/", rendered)
        self.assertNotIn("/home/nas/", rendered)

    def test_source_carries_the_canonical_counts(self):
        source = load_source()
        self.assertEqual(len(source["registry"]), 41)
        self.assertEqual(len(source["decisions"]), 41)
        self.assertEqual(len(source["snapshot"]["sessions"]), 19)
        self.assertEqual(len(source["cycles"]), 9)
        self.assertEqual(len([r for r in source["registry"] if r["metadata"]["parent_sid"] == source["main_session"]["sid"]]), 40)
        reconstructed = [c for c in source["cycles"] if c.get("reconstructed")]
        self.assertEqual(len(reconstructed), 3)                 # frame-only requests have no captured cycle record

    def test_materialized_files_round_trip_and_hold_no_unexpanded_token(self):
        with tempfile.TemporaryDirectory() as root:
            fx = materialize(root)
            with open(fx.jobs_path, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
            self.assertEqual(len(lines), 41)
            for line, row in zip(lines, fx.source["registry"]):
                fields = line.split("\t")
                self.assertEqual(len(fields), 6)
                parsed = dict(_parse_pipe_meta(fields[5]))
                parsed.pop("_name", None)
                self.assertEqual(parsed, row["metadata"])
            for folder, _dirs, files in os.walk(root):
                for name in files:
                    with open(os.path.join(folder, name), encoding="utf-8") as handle:
                        self.assertNotIn("$FIXTURE_ROOT", handle.read(), os.path.join(folder, name))
            self.assertTrue(os.path.isfile(os.path.join(root, "artifacts", ".runtime", "routes", "rt-bca10c8fd3987697.json")))
            self.assertTrue(os.path.isfile(os.path.join(root, "state", "completion", "rt-bca10c8fd3987697", "execute.json")))

    def test_screen_fixture_covers_nine_scenes_at_both_widths(self):
        fixture = load_screens()
        self.assertEqual(len(fixture["scenes"]), 9)
        for name, scene in fixture["scenes"].items():
            self.assertEqual(set(scene["widths"]), {"100", "168"})
            for width, screen in scene["widths"].items():
                self.assertEqual(len(screen["rows"]), 45, name)
                self.assertEqual(screen["cols"], int(width))
                self.assertGreater(screen["regions"]["nav"][2], 0)
                widths = {cell_width(row) for row in screen["rows"] if row}
                self.assertEqual(widths, {int(width)}, name)

    def test_screens_hold_no_real_home_path_and_name_every_allowed_difference(self):
        rendered = json.dumps(load_screens(), ensure_ascii=False)
        for forbidden in ("/home/", "/nas/user/"):
            self.assertNotIn(forbidden, rendered)
        allowed = {entry["id"] for scene in load_screens()["scenes"].values() for entry in scene.get("allowed", ())}
        self.assertEqual(allowed, {"successor-wording", "p4-duty-order", "history-replacement-section"})
        first = load_screens()["scenes"]["first"]["widths"]
        self.assertEqual(first["100"]["output"], {"x": 27, "y": 22, "w": 73, "h": 18})
        self.assertEqual(first["168"]["output"], {"x": 104, "y": 4, "w": 64, "h": 36})

    def test_source_carries_every_value_the_status_band_and_system_page_show(self):
        snapshot = load_source()["snapshot"]
        self.assertEqual(len(snapshot["jobs"]), 16)
        self.assertEqual({j["liveness"] for j in snapshot["jobs"]}, {"working", "idle", "done", "dead"})
        self.assertEqual(sorted(snapshot["usage_snapshots"]), ["claude", "codex", "opencode"])
        self.assertEqual(len(snapshot["compute_hosts"]["hosts"]), 5)
        self.assertEqual(snapshot["governor"], {"active": 5, "cap": 30, "classes": {"dispatch": 5}})
        self.assertEqual(set(snapshot["observations"]), {"jobs", "herdr", "compute"})
        tags = {s["session_id"]: s["session_tag"] for s in snapshot["sessions"] if s.get("session_tag")}
        self.assertEqual([tags[sid] for sid in snapshot["session_order"]], ["b6", "c0", "7a", "3e", "d6"])


if __name__ == "__main__":
    unittest.main()
