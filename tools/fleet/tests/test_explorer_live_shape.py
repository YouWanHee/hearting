"""A live snapshot becomes view-model records of exactly the fixture's shape, section by section.

The screens read the fixture's records positionally and by key. A live builder that renames a field,
puts an object where the fixture has a pair, or changes a type breaks a screen only on real data
(that is how a ``ResourceJob`` object once reached the COMPUTE rail). Each section is compared here:
the live record's keys are drawn from the fixture's keys, its value types are the fixture's types,
and an unknown value is ``None``.
"""
import time
import unittest
from pathlib import Path

from tools.fleet.explorer.live import build_view_model
from tools.fleet.explorer.view_model import load_fixture
from tools.fleet.model import Session
from tools.fleet.tests import explorer_fixture as ef

FIXTURE = Path(__file__).parent / "fixtures" / "explorer" / "view_model.json"
HARNESSES = ("claude", "codex", "opencode")
FREE_KEYED = frozenset(("dialogue.logs", "models.shown", "models.profile_map", "models.model_choices",
                        "models.main_only", "models.pools", "models.context", "models.context_source",
                        "models.token_units", "models.effort_keys"))
# Sections only the selection pump's reads can fill: without them they are omitted (empty), never invented.
OMITTED = ("seats", "generations", "cards", "dialogue", "attempts", "models")


def bucket(value):
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "str"
    if isinstance(value, dict) or hasattr(value, "keys"):
        return "map"
    if isinstance(value, (list, tuple)):
        return "list"
    return type(value).__name__


def conform(live, samples, path, problems):
    """Record in ``problems`` how ``live`` differs in shape from the fixture ``samples``."""
    kind = bucket(live)
    if kind == "none":
        return                                  # unknown is always allowed (drawn ·)
    sample_kinds = {bucket(sample) for sample in samples} - {"none"}
    if not sample_kinds:
        return
    if kind not in sample_kinds:
        problems.append(f"{path}: live {kind}, fixture {sorted(sample_kinds)}")
        return
    if kind == "map" and path in FREE_KEYED:
        # Keys are data (a model name, a "tag:index" log): every live value has the shape of some fixture value.
        values = [item for sample in samples if bucket(sample) == "map" for item in sample.values()]
        for key, value in live.items():
            conform(value, values, f"{path}[{key}]", problems)
        return
    if kind == "map":
        keys = set().union(*(set(sample) for sample in samples if bucket(sample) == "map"))
        for key in live:
            if path == "system" and key in ("gpu_process_summary", "unregistered_gpu_summary"):
                if not isinstance(live[key], dict) or set(live[key]) - {"count", "unregistered", "owner", "remaining"}:
                    problems.append(f"system.{key}: invalid summary shape")
                continue
            if key not in keys:
                if path == "system" and key == "gpu_processes":
                    for index, process in enumerate(live[key]):
                        if not isinstance(process, dict) or set(process) - {
                                "host", "gpu", "session_id", "cwd", "command", "vram", "age"}:
                            problems.append(f"system.gpu_processes[{index}]: invalid process record shape")
                    continue
                if path == "system" and key == "unregistered_gpu_processes":
                    if any(not isinstance(row, (list, tuple)) or len(row) != 5 for row in live[key]):
                        problems.append("system.unregistered_gpu_processes: invalid positional shape")
                    continue
                problems.append(f"{path}.{key}: field the fixture does not have")
                continue
            conform(live[key], [s[key] for s in samples if bucket(s) == "map" and key in s],
                    f"{path}.{key}", problems)
    elif kind == "list":
        lists = [sample for sample in samples if bucket(sample) == "list" and sample]
        lengths = {len(item) for item in lists}
        width = next(iter(lengths)) if len(lengths) == 1 else 0
        kinds = [{bucket(item[i]) for item in lists} - {"none"} for i in range(width)]
        positional = width > 1 and any(kinds[i] != kinds[0] for i in range(1, width))
        if positional and len(live) == width:
            for index, value in enumerate(live):
                conform(value, [item[index] for item in lists], f"{path}[{index}]", problems)
        else:
            flat = [element for item in lists for element in item]
            for index, value in enumerate(live):
                conform(value, flat, f"{path}[]", problems)


def rich_model(with_detail=True):
    """The demo snapshot with the pump's results, so every producible section exists."""
    return ef.demo_model(detail=with_detail)


class SectionShapes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = load_fixture(FIXTURE).to_mapping()
        cls.live = rich_model().to_mapping()
        cls.bare = rich_model(with_detail=False).to_mapping()

    def check(self, section):
        problems = []
        live, fixture = self.live[section], self.fixture[section]
        if isinstance(fixture, dict) and isinstance(live, dict) and section in ("nodes", "generations", "cards"):
            live_items, fixture_items = list(live.values()), [v for v in fixture.values()]
            for item in live_items:
                conform(item, fixture_items, section, problems)
        else:
            conform(live, [fixture], section, problems)
        self.assertEqual(problems, [], section)

    def test_every_section_the_builder_fills_has_the_fixture_shape(self):
        for section in ("hosts", "usage", "repos", "sessions", "routes", "nodes", "questions", "answered",
                        "seats", "generations", "cards", "dialogue", "attempts", "models",
                        "peer_nodes", "peer_edges", "compute", "memory", "events", "system"):
            with self.subTest(section=section):
                self.assertTrue(self.live[section], f"live {section} is empty — the sample has no source")
                self.check(section)

    def test_sections_without_a_source_are_omitted_not_invented(self):
        for section in OMITTED:
            with self.subTest(section=section):
                self.assertFalse(self.bare[section])

    def test_session_records_carry_the_same_fields_for_every_harness(self):
        by_harness = {}
        for row in self.live["sessions"]:
            by_harness.setdefault(row["harness"], set()).add(frozenset(row))
        self.assertEqual(set(by_harness), set(HARNESSES))
        fields = {next(iter(sets)) for sets in by_harness.values()}
        self.assertEqual(len(fields), 1)
        self.assertEqual(set(next(iter(fields))), set(self.fixture["sessions"][0]))

    def test_compute_rows_are_positional_like_the_fixture(self):
        for host in self.live["compute"]:
            self.assertEqual(len(host), len(self.fixture["compute"][0]))
        reachable = [host for host in self.live["compute"] if host[5]]
        self.assertTrue(reachable)
        for gpu in reachable[0][5]:
            self.assertEqual(len(gpu), len(self.fixture["compute"][0][5][0]))

    def test_unreadable_host_and_unknown_values_stay_none(self):
        gone = next(host for host in self.live["compute"] if host[0] == "gone")
        self.assertEqual(gone[2:5], [None, None, None])
        self.assertEqual(gone[5], [])
        unknown = Session(harness="claude", pid=1, cwd="/synthetic/repo", session_id="s-unknown",
                          session_tag="ab")
        vm = build_view_model({"sessions": [unknown], "project_of": lambda _cwd: "synthetic-repo"})
        row = vm.sessions[0]
        for name in ("model", "effort", "title", "context", "window", "now", "ask", "pane", "branch",
                     "generation", "last_clear", "running", "gpu", "pane_meta"):
            self.assertIsNone(row[name], name)
        self.assertEqual(row["tag"], "ab")

    def test_a_zero_is_a_value_not_unknown(self):
        zero = Session(harness="codex", pid=2, cwd="/synthetic/repo", session_id="s-zero",
                       session_tag="cd", ctx_pct=0, elapsed_min=0)
        vm = build_view_model({"sessions": [zero], "project_of": lambda _cwd: "synthetic-repo"})
        self.assertEqual(vm.sessions[0]["context"], 0)
        self.assertEqual(vm.sessions[0]["age"], "0m")


if __name__ == "__main__":
    unittest.main()
