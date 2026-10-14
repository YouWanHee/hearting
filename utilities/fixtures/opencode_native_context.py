"""Native limit observations for tests that replace the OpenCode worker.

Only lifecycle/access fixtures use this substitute. The native context suite
still inspects installed OpenCode and exercises unavailable/managed limits.
"""
from __future__ import annotations

from opencode_context_window import apply_context_override


def launch_context(**values):
    model = values["model"]
    # Some parser/carrier fixtures deliberately use a bare fake model name.
    config_model = model if "/" in model else "fixture/" + model
    native = {"context": 1000000, "input": None, "output": 8192}
    config, applied = apply_context_override(
        values["config_content"], config_model, values["context_window"], native)
    return {
        "model": model, "config_content": config,
        "catalog_limits": dict(native),
        "configured_limits": {"context": None, "input": None, "output": None},
        "native_limits": dict(native), "applied_limits": applied,
        "final_limits": dict(applied),
        # The substitute has no installed OpenCode to verify, so its policy stays unknown.
        "compaction_policy": {
            "state": "unknown", "enabled": None, "trigger_tokens": None,
            "context_tokens": applied.get("context"), "input_tokens": applied.get("input"),
            "output_allowance_tokens": applied.get("output"),
            "configured_reserve_tokens": None, "effective_reserve_tokens": None,
            "source": "fixture", "reason": "native-consumer-version-unverified",
        },
        "runtime_version": None,
    }
