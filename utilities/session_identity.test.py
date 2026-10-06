#!/usr/bin/env python3
"""One reading of the harness identity variables, and how sure it is."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import session_identity as S  # noqa: E402


class IdentityTest(unittest.TestCase):
    def test_each_confidence(self):
        cases = (
            ({}, ("", "", "", "none")),
            ({"CLAUDE_CODE_SESSION_ID": "c1"}, ("claude", "c1", "CLAUDE_CODE_SESSION_ID", "sole")),
            ({"CLAUDE_SESSION_ID": "c0"}, ("claude", "c0", "CLAUDE_SESSION_ID", "sole")),
            ({"CODEX_SESSION_ID": "x0", "CODEX_THREAD_ID": "x1"}, ("codex", "x1", "CODEX_THREAD_ID", "sole")),
            ({"OPENCODE_SESSION_ID": "ses_1"}, ("opencode", "ses_1", "OPENCODE_SESSION_ID", "sole")),
            ({"CLAUDE_CODE_SESSION_ID": "c1", "CODEX_THREAD_ID": "x1"},
             ("", "", "CLAUDE_CODE_SESSION_ID,CODEX_THREAD_ID", "ambiguous")),
            ({"CLAUDE_CODE_SESSION_ID": "c1", "CODEX_THREAD_ID": "x1", "AGENT_DISPATCH_CALLER_HARNESS": "codex"},
             ("codex", "x1", "CODEX_THREAD_ID", "named")),
            ({"AGENT_DISPATCH_CURRENT_HARNESS": "opencode"},
             ("opencode", "", "AGENT_DISPATCH_CURRENT_HARNESS", "named")),
            ({"AGENT_DISPATCH_CALLER_HARNESS": "gemini", "CLAUDE_CODE_SESSION_ID": "c1"},
             ("", "", "AGENT_DISPATCH_CALLER_HARNESS", "invalid")),
        )
        for env, expected in cases:
            with self.subTest(env=env):
                found = S.identity(env)
                self.assertEqual((found.harness, found.session_id, found.source, found.confidence), expected)
                self.assertEqual(found.known, expected[3] in ("named", "sole"))

    def test_a_label_never_refuses(self):
        self.assertEqual(S.session_label({}), "operator")
        self.assertEqual(S.session_label({"CLAUDE_CODE_SESSION_ID": "c1"}), "c1")
        self.assertEqual(S.session_label({"CODEX_THREAD_ID": "x1", "CLAUDE_SESSION_ID": "c0"}), "c0")
        self.assertEqual(S.session_label({"CODEX_THREAD_ID": "x1", "CLAUDE_SESSION_ID": "c0",
                                          "AGENT_DISPATCH_CALLER_HARNESS": "codex"}), "x1")


class CallersAgreeTest(unittest.TestCase):
    """The callers that used to keep their own copy now give the module's answer."""

    ENVS = ({}, {"CLAUDE_CODE_SESSION_ID": "c1"}, {"CODEX_THREAD_ID": "x1"}, {"OPENCODE_SESSION_ID": "ses_1"},
            {"CLAUDE_CODE_SESSION_ID": "c1", "CODEX_THREAD_ID": "x1"},
            {"CLAUDE_CODE_SESSION_ID": "c1", "CODEX_THREAD_ID": "x1", "AGENT_DISPATCH_CURRENT_HARNESS": "claude"})

    def test_route_authority_route_chain_and_tidy(self):
        import route_authority
        import session_tidy
        sys.path.insert(0, str(HERE.parent / "tools"))
        from fleet import route_chain
        for env in self.ENVS:
            found = S.identity(env)
            with self.subTest(env=env):
                if found.known:
                    self.assertEqual(route_authority.caller_identity(env), (found.harness, found.session_id))
                    expected_chain = (found.harness, found.session_id) if found.session_id else None
                    self.assertEqual(route_chain.writer_identity(env) if found.session_id else None, expected_chain)
                else:
                    if found.confidence == "ambiguous":
                        with self.assertRaises(Exception):
                            route_authority.caller_identity(env)
                    self.assertIsNone(route_chain.writer_identity(env))
                self.assertEqual(route_authority.correction_source_session(env), S.session_label(env))
                tidy = session_tidy.session_from_env(env=env)
                if found.known and found.session_id:
                    self.assertEqual(tidy, (found.harness, found.session_id))


if __name__ == "__main__":
    unittest.main()
