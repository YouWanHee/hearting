"""VM mapping keeps unknowns explicit and freezes nested input."""
import unittest
import json
from pathlib import Path
from unittest import mock

from tools.fleet.explorer.live import build_view_model
from tools.fleet.explorer.chrome import count_word
from tools.fleet.explorer.view_model import load_fixture
from tools.fleet.model import Session


class CoreFields(unittest.TestCase):
    def test_harness_fields_and_deep_immutability(self):
        sessions = [Session(harness=harness, pid=index + 1, session_tag=f"s{index}",
                            cwd="/synthetic/repo", title=None, ctx_pct=0,
                            liveness="working", elapsed_min=7)
                    for index, harness in enumerate(("claude", "codex", "opencode"))]
        vm = build_view_model({"sessions": sessions, "compute_hosts": [],
                               "project_of": lambda _cwd: "synthetic-repo"})
        self.assertEqual(tuple(row["harness"] for row in vm.sessions), ("claude", "codex", "opencode"))
        self.assertIsNone(vm.sessions[0]["title"])
        self.assertEqual(vm.sessions[0]["context"], 0)
        self.assertEqual(vm.sessions[0]["state"], "working")
        self.assertEqual(vm.sessions[0]["age"], "7m")
        with self.assertRaises(TypeError):
            vm.sessions[0]["title"] = "changed"

    def test_reference_fixture_has_complete_scrubbed_core_records(self):
        path = Path(__file__).parent / "fixtures" / "explorer" / "view_model.json"
        vm = load_fixture(path)
        self.assertGreater(len(vm.hosts), 0)
        self.assertGreater(len(vm.sessions), 0)
        self.assertGreater(len(vm.routes), 0)
        self.assertGreater(len(vm.nodes), 0)
        self.assertGreater(len(vm.questions), 0)
        self.assertGreater(len(vm.seats), 0)
        self.assertGreater(len(vm.generations), 0)
        self.assertGreater(len(vm.cards), 0)
        self.assertGreater(len(vm.dialogue), 0)
        self.assertGreater(len(vm.peer_nodes), 0)
        self.assertGreater(len(vm.compute), 0)
        self.assertTrue(vm.models)
        self.assertTrue(vm.system)
        encoded = path.read_text(encoding="utf-8").lower()
        self.assertNotIn("/home/", encoded)
        self.assertNotIn("mock", encoded)

    def test_live_boundary_keeps_exact_pane_and_source_record_joins(self):
        session = {"harness": "codex", "session_id": "sid-live", "cwd": "/synthetic/repo",
                   "herdr_pane": "workspace:pane-7", "context_pct": 0}
        graph = ({"harness": "codex", "session_id": "sid-live",
                  "pane_id": "workspace:pane-7"},)
        facts = {"routes": (("rt-test", {"id": "rt-test"}),), "warnings": ()}
        vm = build_view_model({"sessions": [session], "pane_graph": graph,
                               "project_of": lambda _cwd: "synthetic-repo"},
                              accepted_detail={"source_records": facts})
        self.assertEqual(vm.sessions[0]["pane"], "workspace:pane-7")
        self.assertEqual(vm.sessions[0]["context"], 0)
        self.assertEqual(vm.pane_graph[0]["session_id"], "sid-live")
        self.assertEqual(vm.source_records["warnings"], ())


class ConfigFields(unittest.TestCase):
    def test_cached_config_and_memory_projection_preserve_missing_and_zero(self):
        from tools.fleet.explorer.view_model import ExplorerViewModel
        config = {"models": {"profiles": ["balanced"], "context": {"codex": 0}}}
        snapshot = {"sessions": [], "resources": [], "memory": None}
        vm = build_view_model(snapshot, accepted_detail={"memory": [("repo", [("12:00", "saved", "note", "fact")])]},
                              cached_config=config)
        self.assertEqual(vm.models["profiles"], ("balanced",))
        self.assertEqual(vm.models["context"]["codex"], 0)
        self.assertEqual(vm.memory[0][1][0][3], "fact")
        missing = build_view_model({"sessions": [], "resources": []}, cached_config={})
        self.assertIsNone(missing.models)
        self.assertIsNone(missing.memory)
        self.assertIsInstance(ExplorerViewModel.from_mapping(missing), ExplorerViewModel)


class RealDataFidelity(unittest.TestCase):
    """What the default board decides about a row, the explorer's records say the same."""

    def build(self, *sessions, **extra):
        return build_view_model({"sessions": list(sessions), "project_of": lambda cwd: "synthetic-repo",
                                 **extra}, now=2_000_000_000)

    def test_model_effort_and_context_resolve_like_the_default_board_for_every_harness(self):
        rows = [Session(harness="claude", pid=1, session_tag="c1", cwd="/x/repo", liveness="idle",
                        model="Opus 5.5 (1M context)", effort="xhigh", active_context_tokens=250_000,
                        context_window_tokens=1_000_000),
                Session(harness="codex", pid=2, session_tag="c2", cwd="/x/repo", liveness="idle",
                        model="gpt-6.1-sol", effort="high", active_context_tokens=100_000,
                        context_window_tokens=400_000),
                Session(harness="opencode", pid=3, session_tag="c3", cwd="/x/repo", liveness="idle",
                        model="opencode-go/glm-5.2", effort="runtime-default", effort_default=True),
                Session(harness="claude", pid=4, session_tag="c4", cwd="/x/repo", liveness="stale",
                        model="Opus 5.5", effort="high", ctx_pct=40)]
        vm = self.build(*rows)
        by_tag = {row["tag"]: row for row in vm.sessions}
        self.assertEqual(tuple(by_tag["c1"]["model"]), ("Opus 5.5", "fam_opus"))
        self.assertEqual(by_tag["c1"]["effort"], "xhigh")
        self.assertEqual((by_tag["c1"]["context"], by_tag["c1"]["window"]), (25, 1.0))
        from tools.fleet import render
        self.assertEqual(by_tag["c2"]["context"], render._native_context_gauge(rows[1])[0])   # window-normalised
        self.assertEqual(by_tag["c3"]["model"][0],
                         render._clean_model(render._dispatch_display_model("opencode-go/glm-5.2")))
        self.assertEqual(by_tag["c3"]["effort"], "default")          # the runtime's own default, not a level
        # a stale row has no live telemetry: the bare harness, no context
        self.assertEqual((by_tag["c4"]["model"], by_tag["c4"]["effort"], by_tag["c4"]["context"]),
                         (None, None, None))

    def test_a_folder_inside_the_home_folder_is_never_grouped_as_the_home_folder(self):
        from unittest import mock
        from tools.fleet.explorer import live
        # a data root that carries the user's folder name is the home folder for this purpose
        with mock.patch.object(live, "_HOME_FORMS", ("/home/alice", "/data/alice")):
            names = {live._repo_of(lambda _cwd: "alice", cwd) for cwd in
                     ("/data/alice/work/projects/2026-08_keyword", "/data/alice")}
            self.assertEqual(names, {"2026-08_keyword", live.HOME_LABEL})
            self.assertEqual(live._repo_of(lambda _cwd: "tool", "/data/alice/NN_Zoo/tool"), "tool")
            self.assertEqual(live._repo_of(lambda _cwd: "alice-site", "/data/alice/alice-site"), "alice-site")

    def test_branch_is_the_known_branch_or_nothing(self):
        vm = self.build(Session(harness="claude", pid=1, session_tag="b1", cwd="/x/repo", liveness="idle",
                                branch="fix/thing"),
                        Session(harness="claude", pid=2, session_tag="b2", cwd="/x/repo", liveness="idle"))
        self.assertEqual([row["branch"] for row in vm.sessions], ["fix/thing", None])

    def test_install_version_and_usage_freshness_reach_the_top_line(self):
        usage = {"claude": {"payload": {"rl_5h": 10, "rl_7d": 20}, "observed_at": 1_999_999_000}}
        vm = self.build(Session(harness="claude", pid=1, session_tag="u1", cwd="/x/repo", liveness="idle"),
                        hearting={"version": "v3.42.0-2-gdc406c55a-dirty", "install_method": "unmanaged"},
                        usage_snapshots=usage)
        self.assertEqual(vm.system["install"]["version"], "v3.42.0-2-gdc406c55a-dirty")
        self.assertRegex(vm.system["usage_api"]["freshness"], r"^\d\d:\d\d$")

    def test_route_uses_recorded_result_name_and_default_version_helper_keeps_build(self):
        from tools.fleet.explorer import live
        snapshot = {"resources": (), "sessions": (), "jobs": (), "route_entities": ()}
        collected = {"route-x": {"view": {"nodes": [], "slug": "route-slug"},
                                  "record": {"slug": "route-slug", "result": {
                                      "name": "recorded task name", "result": "success"}},
                                  "jobs": [], "sessions": []}}
        with mock.patch.object(live, "_collect_routes", return_value=collected):
            routes, _nodes = live._route_records(snapshot, [], {}, lambda _cwd: None, 10)
        self.assertEqual(routes[0]["title"], "recorded task name")
        install = live._install_record({"hearting": {"version": "v7.8.9-4-gabc123-dirty"}})
        self.assertEqual(install["version"], "v7.8.9-4-gabc123-dirty")

    def test_peer_endpoint_matches_a_resumed_session_alias_and_newest_first(self):
        from tools.fleet.explorer.live import _peer_records
        sessions = [
            {"tag": "new", "harness": "claude", "repo": "repo"},
            {"tag": "sender", "harness": "codex", "repo": "repo"},
        ]
        index = {
            "new": {"sid": "sid-new", "ids": ("sid-new", "sid-old"),
                    "session": {"peer_last_recv": {"from_session_id": "sid-sender",
                                                      "from_harness": "codex", "kind": "steer",
                                                      "age_min": 20}}},
            "sender": {"sid": "sid-sender", "ids": ("sid-sender",),
                       "session": {"peer_last_sent": {"to_session_id": "sid-old",
                                                       "to_harness": "claude", "kind": "handoff",
                                                       "age_min": 2}}},
        }
        _nodes, edges = _peer_records(sessions, index, 100)
        self.assertEqual(edges["links"][0][1], ["new", "claude"])
        self.assertEqual(edges["links"][0][2], "handoff")
        self.assertEqual(edges["links"][1][2], "steer")

    def test_decision_records_become_the_answered_list_and_the_feed_line(self):
        memory = {"recent": [{"ts": "2026-10-23T12:00:00+00:00", "action": "decision-record", "type": "decision",
                              "snippet": "pick the plain name", "cwd": "/x/repo", "sid": ""}],
                  "by_repo": {"repo": [{"ts": "2026-10-23T12:00:00+00:00", "action": "decision-record",
                                        "type": "decision", "snippet": "pick the plain name"}]}}
        vm = self.build(Session(harness="claude", pid=1, session_tag="m1", cwd="/x/repo", liveness="idle"),
                        memory=memory)
        self.assertEqual(vm.answered[0][0], "repo")
        self.assertEqual(vm.answered[0][1][0][1], "pick the plain name")
        self.assertEqual(vm.system["answered_total"], 1)
        self.assertEqual(vm.events[0][1], "m1")            # the session that works in that folder
        self.assertEqual(vm.events[0][5], "pick the plain name")


class CountWords(unittest.TestCase):
    def test_every_count_word_is_singular_for_one(self):
        self.assertEqual(count_word(1, "question"), "1 question")
        self.assertEqual(count_word(2, "question"), "2 questions")
        self.assertEqual(count_word(0, "session"), "0 sessions")
        self.assertEqual(count_word(1, "harness", "harnesses"), "1 harness")
        self.assertEqual(count_word(3, "harness", "harnesses"), "3 harnesses")


if __name__ == "__main__":
    unittest.main()
