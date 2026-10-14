from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node") or "node"


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
                    "second": {"limit": {"context": 900000, "input": 850000, "output": 7000}}}},
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
                "provider": {"openai": {"options": {"keep": True}, "models": {
                    "gpt": {"variants": {"low": {"temperature": 0.1}},
                            "limit": {"context": 1000000, "input": 922000, "output": 8192}},
                    "second": {"variants": {"high": {"temperature": 0.8}},
                               "limit": {"context": 280000, "input": 170000, "output": 4096}},
                    "without-input": {"limit": {"context": 500000, "output": 4096}}}}},
                "disabled_providers": [],
            }
            script = f'''import {{ AgentHarnessGuards }} from {json.dumps((ROOT / "adapters/opencode/plugins/hearting-guards.js").as_uri())};
const hooks = await AgentHarnessGuards({{ directory: process.cwd(), worktree: process.cwd(), client: {{}} }});
const original = {json.dumps(config)};
const before = JSON.parse(JSON.stringify(original));
const start = performance.now();
const after = await hooks.config(before);
const again = await hooks.config(JSON.parse(JSON.stringify(after)));
const disabled = await hooks.config({{ ...JSON.parse(JSON.stringify(original)), disabled_providers: ["openai", "recent-provider"] }});
const restricted = await hooks.config({{ ...JSON.parse(JSON.stringify(original)), enabled_providers: ["openai"] }});
console.log(JSON.stringify({{ after, again, disabled, restricted, elapsed_ms: performance.now() - start }}));
'''
            env = {**os.environ, "PATH": "/usr/bin:/bin", "HOME": str(home), "XDG_CONFIG_HOME": str(xconfig),
                   "XDG_DATA_HOME": str(xdata), "XDG_STATE_HOME": str(xstate),
                   "XDG_CACHE_HOME": str(home / "cache"), "HEARTING_OPENCODE_MODELS_CACHE": str(xcache / "opencode"),
                   "AGENT_HOME": str(ROOT), "AGENT_SESSION_ROLE": "worker"}
            run = subprocess.run([NODE, "--input-type=module", "-e", script], cwd=ROOT,
                                 env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertNotIn("PRIVATE_AUTH_VALUE", run.stdout + run.stderr)
            result = json.loads(run.stdout.strip().splitlines()[-1])
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
