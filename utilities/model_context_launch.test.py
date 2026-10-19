#!/usr/bin/env python3
"""Focused no-model-call checks for OpenCode's native context-limit projection."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import opencode_context_window as context
import opencode_runtime


MODEL = "opencode-go/deepseek-v4.1-flash"


class ModelContextLaunchTest(unittest.TestCase):
    def test_override_caps_existing_limits_and_preserves_unrelated_settings(self):
        before = {
            "model": MODEL,
            "plugin": ["retained-plugin"],
            "permission": {"*": "deny", "external_directory": {"/safe": "allow"}},
            "hooks": {"beforeRun": [{"command": "keep-hook"}]},
            "provider": {
                "opencode-go": {
                    "options": {"baseURL": "https://provider.invalid", "keep": True},
                    "models": {"deepseek-v4.1-flash": {
                        "variants": {"max": {"reasoningEffort": "max"}},
                        "limit": {"context": 700000, "input": 430000, "output": 8192},
                    }},
                }
            },
        }
        native_limits = {"context": 700000, "input": 430000, "output": 8192}
        encoded, applied = context.apply_context_override(
            json.dumps(before), MODEL, "800000", native_limits
        )
        after = json.loads(encoded)
        selected = after["provider"]["opencode-go"]["models"]["deepseek-v4.1-flash"]
        self.assertEqual(selected["limit"], {"context": 700000, "input": 430000, "output": 8192})
        self.assertEqual(applied, {"context": 700000, "input": 430000, "output": 8192})
        for key in ("model", "plugin", "permission", "hooks"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after["provider"]["opencode-go"]["options"],
                         before["provider"]["opencode-go"]["options"])
        self.assertEqual(selected["variants"], {"max": {"reasoningEffort": "max"}})

        # The native input ceiling can be smaller than context and remains so;
        # a large output allowance is never touched.
        _, smaller = context.apply_context_override(
            json.dumps(before), MODEL, "600000",
            {"context": 1000000, "input": 350000, "output": 500000},
        )
        self.assertEqual(smaller, {"context": 600000, "input": 350000, "output": 8192})

        # Smaller limits already in the selected model config remain effective
        # even when the installed provider advertises a larger capacity.
        limited = json.loads(json.dumps(before))
        limited["provider"]["opencode-go"]["models"]["deepseek-v4.1-flash"]["limit"] = {
            "context": 280000, "input": 170000, "output": 4096,
        }
        encoded, applied = context.apply_context_override(
            json.dumps(limited), MODEL, "800000",
            {"context": 1000000, "input": 600000, "output": 8192},
        )
        self.assertEqual(applied, {"context": 280000, "input": 170000, "output": 4096})
        final_limits = json.loads(encoded)["provider"]["opencode-go"]["models"]["deepseek-v4.1-flash"]["limit"]
        self.assertEqual(final_limits, {"context": 280000, "input": 170000, "output": 4096})

    def test_invalid_existing_config_and_unknown_native_model_fail_closed(self):
        for value in ("800k", "+800000", "99999", "1000001"):
            with self.subTest(value=value), self.assertRaises(context.OpenCodeContextError):
                context.apply_context_override("{}", MODEL, value,
                                               {"context": 1000000, "input": None, "output": 8192})
        with self.assertRaises(context.OpenCodeContextError):
            context.apply_context_override("[]", MODEL, "800000",
                                           {"context": 1000000, "input": None, "output": 8192})
        zero = context.apply_context_override(
            "{}", MODEL, "800000", {"context": 0, "input": 480000, "output": 8192}
        )[1]
        self.assertEqual(zero, {"context": 0, "input": 0, "output": 8192})
        with self.assertRaises(context.OpenCodeContextError):
            context.apply_context_override("{}", MODEL, "800000",
                                           {"context": None, "input": 480000, "output": 8192})

    def test_models_dev_cache_supplies_baseline_separate_from_user_model_limits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache" / "opencode" / "models.json"
            cache.parent.mkdir(parents=True)
            native = {"context": 900000, "output": 120000}
            cache.write_text(json.dumps({"opencode-go": {"models": {
                "deepseek-v4.1-flash": {"limit": native}
            }}}, separators=(",", ":")), encoding="utf-8")
            env = {"HOME": str(root), "XDG_CACHE_HOME": str(root / "cache"),
                   "OPENCODE_CONFIG_CONTENT": json.dumps({"provider": {"opencode-go": {
                       "models": {"deepseek-v4.1-flash": {"limit": {
                           "context": 1500000, "input": 850000, "output": 250000,
                       }}}
                   }}})}
            self.assertEqual(context._native_limits(MODEL, env=env),
                             {"context": 900000, "input": None, "output": 120000})
            encoded, applied = context.apply_context_override(
                env["OPENCODE_CONFIG_CONTENT"], MODEL, "800000",
                context._native_limits(MODEL, env=env),
            )
            self.assertEqual(applied, {"context": 800000, "input": 800000, "output": 120000})
            limits = json.loads(encoded)["provider"]["opencode-go"]["models"]["deepseek-v4.1-flash"]["limit"]
            self.assertEqual(limits, {"context": 800000, "input": 800000, "output": 120000})

    def test_prepare_caps_preexisting_merged_limits_and_rejects_higher_managed_override(self):
        configured = {
            "model": MODEL,
            "provider": {"opencode-go": {"models": {"deepseek-v4.1-flash": {
                "limit": {"context": 700000, "input": 300000, "output": 80000},
            }}}},
        }
        final = json.loads(json.dumps(configured))
        with mock.patch.object(context, "_native_limits", return_value={
                "context": 1000000, "input": None, "output": 120000}), mock.patch.object(
                context, "_resolved_config", side_effect=[configured, final]):
            launch = context.prepare_launch_context(
                model=MODEL, context_window="800000", config_content="{}",
                cwd="/tmp", environ={"OPENCODE_CONFIG_CONTENT": "{}"}, command="opencode",
            )
        self.assertEqual(launch["configured_limits"], {
            "context": 700000, "input": 300000, "output": 80000,
        })
        self.assertEqual(launch["native_limits"], {
            "context": 700000, "input": 300000, "output": 80000,
        })
        self.assertEqual(launch["applied_limits"], {
            "context": 700000, "input": 300000, "output": 80000,
        })
        self.assertEqual(launch["final_limits"], launch["applied_limits"])

        managed_override = {"model": MODEL, "provider": {"opencode-go": {
            "models": {"deepseek-v4.1-flash": {"limit": {"context": 900000}}}
        }}}
        with mock.patch.object(context, "_native_limits", return_value={
                "context": 1000000, "input": None, "output": None}), mock.patch.object(
                context, "_resolved_config", side_effect=[{"model": MODEL}, managed_override]):
            with self.assertRaisesRegex(context.OpenCodeContextError, "higher-priority managed setting"):
                context.prepare_launch_context(
                    model=MODEL, context_window="800000", config_content="{}",
                    cwd="/tmp", environ={"OPENCODE_CONFIG_CONTENT": "{}"}, command="opencode",
                )

    def test_native_compaction_policy_uses_final_input_and_reserve_once(self):
        configured = {
            "model": MODEL, "compaction": {"auto": True, "reserved": 5000},
            "provider": {"opencode-go": {"models": {"deepseek-v4.1-flash": {
                "limit": {"context": 500000, "input": 400000, "output": 8192},
            }}}},
        }
        final = json.loads(json.dumps(configured))
        final["provider"]["opencode-go"]["models"]["deepseek-v4.1-flash"]["limit"]["input"] = 350000
        native = {"context": 700000, "input": 350000, "output": 8192}
        with mock.patch.object(context, "_installed_version", return_value="1.19.0"), \
                mock.patch.object(context, "_native_limits", return_value=native), \
                mock.patch.object(context, "_resolved_config", side_effect=[configured, final]):
            launch = context.prepare_launch_context(
                model=MODEL, context_window="800000", config_content=json.dumps(configured),
                cwd="/tmp", environ={"OPENCODE_CONFIG_CONTENT": json.dumps(configured)},
                command="opencode")
        self.assertEqual(launch["runtime_version"], "1.19.0")
        self.assertEqual(launch["final_limits"], {"context": 500000, "input": 350000,
                                                  "output": 8192})
        self.assertEqual(launch["compaction_policy"], {
            "state": "applied", "enabled": True, "trigger_tokens": 345000,
            "context_tokens": 500000, "input_tokens": 350000,
            "output_allowance_tokens": 8192, "configured_reserve_tokens": 5000,
            "effective_reserve_tokens": 5000,
            "source": "opencode-v1.19.0-session-overflow", "reason": None,
        })

    def test_configured_custom_model_needs_no_standard_catalog_entry(self):
        configured = {
            "model": "local/custom",
            "permission": {"*": "deny"},
            "provider": {"local": {
                "options": {"baseURL": "https://custom.invalid"},
                "models": {"custom": {"limit": {
                    "context": 64000, "input": 48000, "output": 8192,
                }}},
            }},
        }
        with mock.patch.object(context, "_native_limits", side_effect=
                context.OpenCodeContextError("no standard model record")), mock.patch.object(
                context, "_resolved_config", side_effect=[configured, configured]):
            launch = context.prepare_launch_context(
                model="local/custom", context_window="800000",
                config_content=json.dumps(configured), cwd="/tmp", environ={},
            )
        self.assertEqual(launch["catalog_limits"], {
            "context": None, "input": None, "output": None,
        })
        self.assertEqual(launch["applied_limits"], {
            "context": 64000, "input": 48000, "output": 8192,
        })
        self.assertEqual(json.loads(launch["config_content"]), configured)

    def test_missing_catalog_and_native_context_remain_unknown(self):
        with mock.patch.object(context, "_native_limits", side_effect=
                context.OpenCodeContextError("no standard model record")), mock.patch.object(
                context, "_resolved_config", return_value={"model": "local/custom"}):
            with self.assertRaisesRegex(context.OpenCodeContextError, "no standard model record"):
                context.prepare_launch_context(
                    model="local/custom", context_window="800000",
                    config_content="{}", cwd="/tmp", environ={},
                )

    def test_installed_opencode_resolves_and_consumes_merged_limits_without_a_model_call(self):
        executable = shutil.which("opencode") or "/home/Uihyeop/.opencode/bin/opencode"
        if not Path(executable).is_file():
            self.skipTest("installed OpenCode CLI is unavailable")
        with tempfile.TemporaryDirectory(prefix="model-context-opencode-") as temporary:
            root = Path(temporary)
            for name in ("config", "data", "state", "cache"):
                (root / name).mkdir()
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(root),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CACHE_HOME": str(root / "cache"),
            }
            version = subprocess.run([executable, "--version"], cwd=root, env=env,
                                     text=True, capture_output=True, timeout=15, check=False)
            self.assertEqual(version.returncode, 0, version.stderr)
            parsed = opencode_runtime.parse_version((version.stdout or "") + "\n" +
                                                    (version.stderr or ""))
            if parsed is None:
                self.skipTest("installed OpenCode version could not be identified")
            if parsed[0] >= 2:
                custom = {
                    "model": "local/custom", "plugins": [],
                    "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
                    "providers": {"local": {
                        "package": "aisdk:@ai-sdk/openai-compatible",
                        "settings": {"baseURL": "http://127.0.0.1:9/v1"},
                        "models": {"custom": {"name": "Custom fixture",
                            "modelID": "custom-api-id", "limit": {
                                "context": 64000, "input": 48000, "output": 8192,
                            }}},
                    }},
                }
                try:
                    launch = context.prepare_launch_context(
                        model="local/custom", context_window="60000",
                        config_content=json.dumps(custom), cwd=str(root),
                        environ={**env, "OPENCODE_CONFIG_CONTENT": json.dumps(custom)},
                        command=executable,
                    )
                except context.OpenCodeContextError as exc:
                    self.skipTest(f"isolated V2 native API unavailable: {exc}")
                self.assertEqual(launch["native_limits"], {
                    "context": 64000, "input": 48000, "output": 8192,
                })
                self.assertEqual(launch["applied_limits"], {
                    "context": 60000, "input": 48000, "output": 8192,
                })
                self.assertEqual(launch["final_limits"], launch["applied_limits"])
                self.assertEqual(launch["location_identity"], str(root))
                return

            if parsed != (1, 19, 0):
                self.skipTest("installed V1 consumer is not the tested 1.19.0 version")
            cache = root / "cache" / "opencode" / "models.json"
            cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps({"opencode-go": {"models": {
                "deepseek-v4.1-flash": {"limit": {
                    "context": 900000, "input": 650000, "output": 8192,
                }}
            }}}), encoding="utf-8")
            original = {
                "model": MODEL, "plugin": [], "permission": {"*": "deny"},
                "provider": {"opencode-go": {"models": {
                    "deepseek-v4.1-flash": {"limit": {
                        "context": 850000, "input": 600000, "output": 4096,
                    }, "variants": {"max": {"reasoningEffort": "max"}}}
                }}},
            }
            launch = context.prepare_launch_context(
                model=MODEL, context_window="800000", config_content=json.dumps(original),
                cwd=str(root), environ={**env, "OPENCODE_CONFIG_CONTENT": json.dumps(original)},
                command=executable,
            )
            self.assertEqual(launch["native_limits"], {
                "context": 850000, "input": 600000, "output": 4096,
            })
            self.assertEqual(launch["final_limits"], launch["applied_limits"])
            final_env = {**env, "OPENCODE_CONFIG_CONTENT": launch["config_content"]}
            debug = subprocess.run([executable, "debug", "config"], cwd=root, env=final_env,
                                   text=True, capture_output=True, timeout=30, check=False)
            self.assertEqual(debug.returncode, 0, debug.stderr)
            resolved = json.loads(debug.stdout)
            selected = resolved["provider"]["opencode-go"]["models"]["deepseek-v4.1-flash"]
            self.assertEqual(selected["limit"], launch["applied_limits"])
            self.assertEqual(selected["variants"], original["provider"]["opencode-go"]["models"]
                             ["deepseek-v4.1-flash"]["variants"])


if __name__ == "__main__":
    unittest.main()
