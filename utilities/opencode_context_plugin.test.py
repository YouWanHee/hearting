from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node") or "node"
_TOOLS_DIR = str(ROOT / "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from fleet import projection, render
from fleet.collectors import dispatch as fleet_dispatch
from fleet.model import DispatchJob


class OpenCodeContextPluginTest(unittest.TestCase):
    def test_real_plugin_config_hook_caps_only_loaded_provider_and_hides_auth_values(self):
        with tempfile.TemporaryDirectory(prefix="opencode-context-hook-") as raw:
            home = Path(raw)
            xconfig, xdata, xstate, xcache = [home / name for name in ("config", "data", "state", "cache")]
            runtime = xconfig / "opencode"
            (runtime / "agent-config").mkdir(parents=True)
            shutil.copyfile(ROOT / "adapters/opencode/config/models.conf", runtime / "agent-config/models.conf")
            (runtime / "agent-config/models.conf").write_text(
                (runtime / "agent-config/models.conf").read_text().replace(
                    "CFG_MODEL_CONTEXT_WINDOW=800000", "CFG_MODEL_CONTEXT_WINDOW=600000"))
            (xdata / "opencode").mkdir(parents=True)
            (xdata / "opencode/auth.json").write_text(json.dumps({"openai": {"secret": "PRIVATE_AUTH_VALUE"}}))
            (xstate / "opencode").mkdir(parents=True)
            (xstate / "opencode/model.json").write_text(json.dumps({"recent": [
                {"providerID": "recent-provider", "modelID": "second"}]}))
            (xcache / "opencode").mkdir(parents=True)
            catalog = {
                "openai": {"env": ["OPENAI_API_KEY"], "models": {
                    "gpt": {"limit": {"context": 1000000, "input": 922000, "output": 8192}},
                    "second": {"limit": {"context": 900000, "input": 850000, "output": 7000}},
                    "small": {"limit": {"context": 300000, "input": 250000, "output": 4096}}}},
                "provider-without-input": {"models": {"tiny": {
                    "limit": {"context": 500000, "output": 4096}}}},
                "recent-provider": {"models": {"second": {
                    "limit": {"context": 900000, "input": 850000, "output": 7000}}}},
                "unloaded-catalog-only": {"models": {"ghost": {
                    "limit": {"context": 1000000, "input": 900000, "output": 8192}}}},
            }
            (xcache / "opencode/models.json").write_text(json.dumps(catalog))
            config = {
                "model": "openai/gpt", "plugin": ["keep"],
                "compaction": {"auto": True, "reserved": 5000},
                "provider": {"openai": {"options": {"keep": True}, "models": {
                    "gpt": {"variants": {"low": {"temperature": 0.1}},
                            "limit": {"context": 1000000, "input": 922000, "output": 8192}},
                    "second": {"variants": {"high": {"temperature": 0.8}},
                               "limit": {"context": 280000, "input": 170000, "output": 4096}},
                    "without-input": {"limit": {"context": 500000, "output": 4096}}}}},
                "disabled_providers": [],
            }
            script = f'''import {{ AgentHarnessGuards }} from {json.dumps((ROOT / "adapters/opencode/plugins/hearting-guards.js").as_uri())};
import {{ readFileSync, readdirSync }} from "node:fs";
const hooks = await AgentHarnessGuards({{ directory: process.cwd(), worktree: process.cwd(), client: {{}} }});
const original = {json.dumps(config)};
const before = JSON.parse(JSON.stringify(original));
const start = performance.now();
const after = await hooks.config(before);
await hooks["chat.params"]({{ sessionID: "ses_context123", model: {{ providerID: "openai", id: "gpt",
  limit: {{ context: 600000, input: 600000, output: 8192 }} }}, provider: {{ info: {{ id: "openai" }} }} }},
  {{ maxOutputTokens: 4096 }});
const policy = JSON.parse(readFileSync(process.env.XDG_STATE_HOME + "/agent-fleet/context-policy/opencode/attempt-att-context.json", "utf8"));
delete process.env.AGENT_DISPATCH_ATTEMPT_ID;
await hooks["chat.params"]({{ sessionID: "ses_interactive", model: {{ providerID: "openai", id: "gpt",
  limit: {{ context: 600000, input: 600000, output: 8192 }} }}, provider: {{ info: {{ id: "openai" }} }} }},
  {{ maxOutputTokens: 4096 }});
const policyDir = process.env.XDG_STATE_HOME + "/agent-fleet/context-policy/opencode";
const interactiveName = readdirSync(policyDir).find((name) => name.startsWith(process.pid + "-") && name.endsWith(".json"));
const interactivePolicy = JSON.parse(readFileSync(policyDir + "/" + interactiveName, "utf8"));
const again = await hooks.config(JSON.parse(JSON.stringify(after)));
const disabled = await hooks.config({{ ...JSON.parse(JSON.stringify(original)), disabled_providers: ["openai", "recent-provider"] }});
const restricted = await hooks.config({{ ...JSON.parse(JSON.stringify(original)), enabled_providers: ["openai"] }});
console.log(JSON.stringify({{ after, again, disabled, restricted, policy, interactivePolicy, elapsed_ms: performance.now() - start }}));
'''
            env = {**os.environ, "PATH": "/usr/bin:/bin", "HOME": str(home), "XDG_CONFIG_HOME": str(xconfig),
                   "XDG_DATA_HOME": str(xdata), "XDG_STATE_HOME": str(xstate),
                   "XDG_CACHE_HOME": str(home / "cache"), "HEARTING_OPENCODE_MODELS_CACHE": str(xcache / "opencode"),
                   "FLEET_SESSION_REGISTRY_DIR": str(xstate / "agent-fleet" / "sessions"),
                   "AGENT_HOME": str(ROOT), "AGENT_SESSION_ROLE": "worker",
                   "AGENT_DISPATCH_ATTEMPT_ID": "att-context", "OPENCODE_VERSION": "1.19.0"}
            run = subprocess.run([NODE, "--input-type=module", "-e", script], cwd=ROOT,
                                 env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertNotIn("PRIVATE_AUTH_VALUE", run.stdout + run.stderr)
            result = json.loads(run.stdout.strip().splitlines()[-1])
            policy = result["policy"]
            self.assertEqual((policy["session_id"], policy["attempt_id"], policy["model"]),
                             ("ses_context123", "att-context", "openai/gpt"))
            self.assertEqual(policy["policy"]["state"], "applied")
            self.assertEqual(policy["policy"]["effective_window_tokens"], 600000)
            self.assertEqual(policy["policy"]["applied_trigger_tokens"], 595000)
            self.assertEqual(policy["policy"]["output_allowance_tokens"], 4096)
            self.assertEqual(policy["policy"]["compaction_reserve_tokens"], 5000)
            self.assertEqual(result["interactivePolicy"]["session_id"], "ses_interactive")
            self.assertIsNone(result["interactivePolicy"]["attempt_id"])
            self.assertEqual(result["interactivePolicy"]["policy"]["state"], "applied")
            # The same producer record passes through the registered collector and
            # normalizer under its exact attempt/session, with the PRD's dispatch
            # surface remaining JSON-only.
            attempt_log = home / "attempt.att-context.opencode.jsonl"
            attempt_log.write_text(json.dumps({"type": "step_finish",
                "sessionID": "ses_context123", "part": {"tokens": {
                    "total": 300000, "input": 280000, "output": 1000,
                    "cache": {"read": 19000, "write": 0}}}}) + "\n", encoding="utf-8")
            os.utime(attempt_log, (time.time() + 2, time.time() + 2))
            job = DispatchJob(key="code", slug="worker", harness="opencode",
                              attempt_id="att-context", liveness="working")
            job.model = "openai/gpt"
            job.cwd = str(home)
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(xstate),
                    "FLEET_SESSION_REGISTRY_DIR": str(xstate / "agent-fleet" / "sessions")}), \
                    mock.patch.object(fleet_dispatch, "_owned_attempt_log_path",
                                      return_value=str(attempt_log)):
                fleet_dispatch._enrich_opencode_attempt_session(job)
                projection.attach_projections([], [job], now=time.time())
            self.assertEqual(job.active_context_tokens, 300000)
            self.assertEqual(job.context_display_window_tokens, 595000)
            self.assertEqual(job.context.used_pct, 50)
            payload = job.to_dict()
            self.assertEqual(payload["context_policy"]["attempt_id"], "att-context")
            self.assertIn("50%", "".join(part for row in
                render._dispatch_summary_detail_row(job, term_width=168) for part, _key in row))
            after = result["after"]
            gpt = after["provider"]["openai"]["models"]["gpt"]
            self.assertEqual(gpt["limit"], {"context": 600000, "input": 600000, "output": 8192}, run.stderr + run.stdout)
            self.assertEqual(gpt["variants"], {"low": {"temperature": 0.1}})
            self.assertEqual(after["provider"]["openai"]["options"], {"keep": True})
            self.assertEqual(result["again"], after)
            self.assertEqual(after["provider"]["openai"]["models"]["second"]["limit"],
                             {"context": 280000, "input": 170000, "output": 4096})
            self.assertEqual(after["provider"]["openai"]["models"]["without-input"]["limit"],
                             {"context": 500000, "output": 4096})
            self.assertEqual(after["provider"]["recent-provider"]["models"]["second"]["limit"],
                             {"context": 600000, "input": 600000, "output": 7000})
            self.assertNotIn("unloaded-catalog-only", after["provider"])
            # Native limits already under the budget: no entry, so OpenCode keeps the catalog record.
            self.assertNotIn("small", after["provider"]["openai"]["models"])
            self.assertEqual(result["disabled"]["provider"]["openai"], config["provider"]["openai"])
            self.assertNotIn("recent-provider", result["disabled"]["provider"])
            self.assertNotIn("recent-provider", result["restricted"]["provider"])
            self.assertGreaterEqual(result["elapsed_ms"], 0)

    def test_helper_failure_keeps_original_plugin_config(self):
        with tempfile.TemporaryDirectory(prefix="opencode-context-failure-") as raw:
            home = Path(raw)
            runtime = home / "config/opencode"
            (runtime / "agent-config").mkdir(parents=True)
            (runtime / "agent-config/models.conf").write_text("broken config\n")
            config = {"model": "openai/gpt", "provider": {"openai": {"models": {}}}}
            script = f'''import {{ AgentHarnessGuards }} from {json.dumps((ROOT / "adapters/opencode/plugins/hearting-guards.js").as_uri())};
const hooks = await AgentHarnessGuards({{ directory: process.cwd(), worktree: process.cwd(), client: {{}} }});
const config = {json.dumps(config)}; const result = await hooks.config(config);
console.log(JSON.stringify({{ same: result === config, config: result }}));
'''
            env = {**os.environ, "PATH": "/usr/bin:/bin", "HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"),
                   "AGENT_HOME": str(ROOT), "AGENT_SESSION_ROLE": "worker"}
            run = subprocess.run([NODE, "--input-type=module", "-e", script], cwd=ROOT,
                                 env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue(json.loads(run.stdout.strip().splitlines()[-1])["same"])


if __name__ == "__main__":
    unittest.main()
