"""Live number allocation, fixed Claude reservations and consumer agreement."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
from fleet import herdr_projection, session_tags
from fleet.model import Session
from fleet.session_handle import minted_tag, resolve_tag
from fleet.collectors import procscan


def collision():
    seen = {}
    for n in range(300):
        sid = "collision-%d" % n
        tag = minted_tag(sid)
        if tag in seen:
            return seen[tag], sid
        seen[tag] = sid
    raise AssertionError("no collision")


class SessionTagCollisionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root),
                                              "CLAUDE_CONFIG_DIR": str(self.root / "claude")})
        self.env.start()
        self.addCleanup(self.env.stop)
        patch = mock.patch("fleet.collectors.herdr.list_agents", return_value=[])
        self.agents = patch.start()
        self.addCleanup(patch.stop)
        self.a, self.b = collision()

    def session(self, sid, harness="codex", started=10, tag=None):
        return Session(harness=harness, session_id=sid, started_at=started,
                       session_tag=tag, pid=os.getpid(), proc_start=procscan.read_proc_start(os.getpid()))

    def tags(self, sessions):
        session_tags.refresh(sessions)
        return [resolve_tag(s.harness, s.session_id) for s in sessions]

    def test_bootstrap_orders_existing_pair_by_start_not_collection_order(self):
        older, newer = self.session(self.a), self.session(self.b, started=20)
        new_tag, old_tag = self.tags([newer, older])
        self.assertEqual(old_tag, minted_tag(self.a))
        self.assertNotEqual(new_tag, old_tag)
        self.assertEqual(self.tags([older, newer]), [old_tag, new_tag])

    def test_first_assignment_keeps_number_when_earlier_session_is_seen_later(self):
        first = self.session(self.b, started=20)
        first_tag = self.tags([first])[0]
        newcomer = self.session(self.a, started=10)
        self.assertEqual(self.tags([newcomer, first])[1], first_tag)
        self.assertNotEqual(resolve_tag("codex", self.a), first_tag)

    def test_cross_harness_collision_and_restart_consumers_agree(self):
        sessions = [self.session(self.a), self.session(self.b, "opencode", started=20)]
        tags = self.tags(sessions)
        self.assertEqual(len(set(tags)), 2)
        script = "from fleet.session_handle import resolve_tag; import sys; print(resolve_tag(sys.argv[1], sys.argv[2]))"
        for sess, tag in zip(sessions, tags):
            env = dict(os.environ, PYTHONPATH=str(ROOT / "tools"))
            actual = subprocess.check_output([sys.executable, "-c", script, sess.harness, sess.session_id], env=env, text=True).strip()
            self.assertEqual(actual, tag)
            agent, _ = herdr_projection.compose(sess.harness, sess.session_id, steward=False, title="")
            self.assertEqual(agent, "[%s] %s" % (tag, sess.harness))
        spec = importlib.util.spec_from_file_location("peer_message_tags", ROOT / "utilities/peer-message.py")
        peer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(peer)
        self.assertEqual(peer.peer_alias("opencode", self.b), "[%s]" % tags[1])

    def test_claude_number_is_fixed_and_codex_moves_even_if_already_assigned(self):
        cx = self.session(self.a)
        original = self.tags([cx])[0]
        cl = self.session("fixed-claude", "claude", started=30, tag=original)
        self.tags([cx, cl])
        self.assertNotEqual(resolve_tag("codex", self.a), original)
        rows = session_tags._read(session_tags._path())
        self.assertEqual(rows[("claude", "fixed-claude")]["tag"], original)

    def test_displaced_session_does_not_steal_another_existing_number(self):
        first, second = self.session(self.a), self.session(self.b, started=20)
        a_tag, b_tag = self.tags([first, second])
        self.tags([self.session("cl", "claude", tag=a_tag), first, second])
        self.assertEqual(resolve_tag("codex", self.b), b_tag)
        self.assertNotIn(resolve_tag("codex", self.a), {a_tag, b_tag})

    def test_record_loss_and_corruption_return_original_hash(self):
        self.tags([self.session(self.a), self.session(self.b, started=20)])
        path = session_tags._path()
        path.unlink()
        self.assertEqual(resolve_tag("codex", self.b), minted_tag(self.b))
        path.write_text("{broken")
        self.assertEqual(resolve_tag("codex", self.b), minted_tag(self.b))
        self.tags([self.session(self.a), self.session(self.b)])
        self.assertEqual(path.read_text(), "{broken")

    def test_unwritable_state_does_not_break_consumer(self):
        with mock.patch("fleet.session_tags.tempfile.mkstemp", side_effect=PermissionError):
            self.tags([self.session(self.a), self.session(self.b)])
        self.assertEqual(resolve_tag("codex", self.b), minted_tag(self.b))

    def test_live_process_survives_partial_collection_and_pid_reuse_releases(self):
        self.tags([self.session(self.a)])
        self.tags([])
        self.assertEqual(len(session_tags._read(session_tags._path())), 1)
        with mock.patch("fleet.collectors.procscan.read_proc_start", return_value="reused"):
            session_tags.refresh([])
        self.assertEqual(session_tags._read(session_tags._path()), {})

    def test_herdr_reserves_fixed_claude_outside_filtered_fleet_collection(self):
        (self.root / "claude/sessions").mkdir(parents=True)
        (self.root / "claude/sessions/1.json").write_text(json.dumps({"sessionId": "cl", "nameSource": "derived", "name": "hearting-" + minted_tag(self.a)}))
        self.agents.return_value = [{"agent": "claude", "pane_id": "w:p", "agent_session": {"kind": "id", "value": "cl"}}]
        self.assertNotEqual(self.tags([self.session(self.a)])[0], minted_tag(self.a))

    def test_unknown_identity_does_not_gain_a_number(self):
        self.tags([Session(harness="codex", pid=os.getpid())])
        self.assertIsNone(resolve_tag("codex", ""))
        self.assertFalse(session_tags._path().exists())

    def test_uuid_creation_order_for_pre_install_herdr_pair(self):
        older = "01a1223d-621b-7908-8399-1f234f623c79"
        newer = "01a1223f-5bfb-7dca-b092-a1db86a2d707"
        self.assertLess(session_tags._started("codex", older), session_tags._started("codex", newer))

    def test_capacity_exhaustion_keeps_existing_256_assignments(self):
        sessions = [self.session("session-%d" % n, started=n) for n in range(256)]
        before = self.tags(sessions)
        self.assertEqual(len(set(before)), 256)
        self.tags(sessions + [self.session("extra", started=300)])
        self.assertEqual([resolve_tag(s.harness, s.session_id) for s in sessions], before)
        self.assertEqual(resolve_tag("codex", "extra"), minted_tag("extra"))

    def test_process_concurrent_refreshes_share_the_same_assignments(self):
        script = '''
import os
from fleet.session_tags import refresh
from fleet.model import Session
from fleet.collectors import herdr, procscan
herdr.list_agents = lambda: []
refresh([Session(harness='codex', session_id=sid, pid=os.getpid(),
    proc_start=procscan.read_proc_start(os.getpid()), started_at=n)
    for n, sid in enumerate(%r)])
''' % ([self.a, self.b],)
        env = dict(os.environ, PYTHONPATH=str(ROOT / "tools"))
        processes = [subprocess.Popen([sys.executable, "-c", script], env=env) for _ in range(4)]
        for process in processes:
            self.assertEqual(process.wait(timeout=10), 0)
        self.assertNotEqual(resolve_tag("codex", self.a), resolve_tag("codex", self.b))


if __name__ == "__main__":
    unittest.main()
