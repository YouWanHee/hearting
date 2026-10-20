import unittest

from tools.fleet.tests.explorer_fixture import load_screens, load_source


class ExplorerFixtureTests(unittest.TestCase):
    def test_source_has_canonical_evidence_families_and_redacted_paths(self):
        source = load_source()
        for key in ("snapshot", "decisions", "registry", "routes", "cycles", "markers", "events"):
            self.assertIn(key, source)
        for key in ("decisions", "registry", "routes", "cycles", "markers", "events"):
            self.assertTrue(source[key], key)
        self.assertEqual(source["path_root"], "$FIXTURE_ROOT")
        rendered = str(source)
        self.assertNotIn("/home/nas/user/Uihyeop/", rendered)
        self.assertNotIn("/home/Uihyeop/", rendered)

    def test_screen_fixture_covers_nine_scenes_at_both_widths(self):
        fixture = load_screens()
        self.assertEqual(len(fixture["scenes"]), 9)
        for scene in fixture["scenes"].values():
            self.assertEqual(set(scene["widths"]), {"100", "168"})
            for width, screen in scene["widths"].items():
                self.assertEqual(len(screen["rows"]), 45)
                self.assertEqual(screen["cols"], int(width))
                self.assertGreater(screen["regions"]["nav"]["w"], 0)
                self.assertIn("PR-1 summary", screen["output_expectation"])


if __name__ == "__main__":
    unittest.main()
