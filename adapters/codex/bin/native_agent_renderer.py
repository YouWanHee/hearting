#!/usr/bin/env python3
"""Pure Codex native-agent TOML renderer.

Extracted from ``sync-native-agents.py`` (WP1, Astra guide alignment O1) so
``tools/install/native_agent_payload.py`` can render the exact same TOML bodies
from a runtime's *effective* config, not only the shipped one. Every public
function here takes an already-parsed config mapping; none reads a runtime
home (no ``$CODEX_HOME``) and none writes a file.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Mapping

_UTILITIES = Path(__file__).resolve().parents[3] / "utilities"
if str(_UTILITIES) not in sys.path:
    sys.path.insert(0, str(_UTILITIES))

from model_profile import resolve_profile_values  # noqa: E402

# Bump when the rendered TOML shape or the specs below change, so
# tools/install/native_agent_payload.py's digest changes with it even if the
# effective config mapping happens to be unchanged.
RENDERER_VERSION = "1"


KERNEL_AGENTS = {
    "memory-scout": {
        "description": "Read-only memory scout for agent-initiated deep memory reconnaissance.",
        "instructions": """You are the Codex-native memory-scout custom agent.
This is adapter-owned output generated from core/MEMORY.md §7.4, not a Claude Agent copy.

Contract:
1. Read-only only. Do not edit files or write memory.
2. Never run memory mutation commands such as mem add, mem note, mem consume, mem restore, mem delete, mem reinforce, mem merge, mem prune, mem graduate, or mem reattribute.
3. Use <agent-home>/tools/memory/recall.sh first in the current cwd with narrow synonym and Korean/English variants.
4. Read one selected hit with python3 <agent-home>/tools/memory/mem.py show <id>, or a small ranked set with <agent-home>/tools/memory/recall.sh "<query>" --full --limit 3. These reads do not consume pending handoffs.
5. If misses matter, expand to --all, then --sessions. Never bypass the CLI with direct SQLite or dump.jsonl reads.
6. Cross-check one live file/code fact when the memory result implies an actionable convention.

Output at most 15 lines:
- verdict: found / not-found / ambiguous
- hits: up to 3 short quotes or paraphrases with record id / session pointer
- apply: one line telling the main agent what to do now
- check: one live-code or file cross-check line, or not checked with reason
""",
    }
}

# Backward-compatible alias for callers that patched/consumed the old constant name.
EXTRA_AGENTS = KERNEL_AGENTS


# Native subagent type catalog (CFG_NATIVE_AGENT_CATALOG in this adapter's
# models.conf, name:portable-profile) — cross-harness parity with the Claude
# catalog (adapters/claude/bin/sync-native-agents.py). Portable profiles resolve
# through utilities/model_profile.py; the sandbox stays workspace-write (these
# are general delegated workers, not the read-only memory-scout shape).
# Instructions are adapter-owned output, not a Claude Agent copy.
CATALOG_SANDBOX = "workspace-write"

CATALOG_AGENTS = {
    "general-purpose": {
        "description": (
            "General-purpose delegated agent for research, code search, and "
            "multi-step tasks on the balanced-deep budget."
        ),
        "instructions": """You are a delegated general-purpose agent.
Complete the assigned task fully — don't gold-plate, but don't leave it half-done.
Search broadly when the target is unknown; read directly when the path is known.
Never create files unless necessary; prefer editing existing files.
Do not re-delegate the whole assignment to another agent.
Finish with a concise report of what was done and the key findings.
""",
    },
    "light": {
        "description": (
            "Breadth fan-out agent on the portable light budget — parallel "
            "sweeps, audits, comparisons, routine searches."
        ),
        "instructions": """You are one leg of a breadth fan-out on a light execution budget.
Stay inside the assigned slice; do not widen scope to neighboring questions.
Return compact, deduplicated findings — the caller merges many legs.
Do not spawn further agents; escalate by reporting, not delegating.
""",
    },
    "deep": {
        "description": (
            "Deep investigation agent on the portable deep budget — root cause, "
            "architecture, subtle correctness."
        ),
        "instructions": """You are the deep leg of an investigation on the deep execution budget.
Ground every claim in files you actually read; quote paths and lines.
Distinguish verified facts from inference and state residual uncertainty.
Prefer one airtight answer over broad partial coverage.
""",
    },
}


def parse_native_catalog(cfg: Mapping[str, str]) -> list[tuple[str, str]]:
    """Parse ``CFG_NATIVE_AGENT_CATALOG`` from an already-parsed config mapping."""
    raw = cfg.get("CFG_NATIVE_AGENT_CATALOG", "")
    entries: list[tuple[str, str]] = []
    for token in raw.split():
        if token.count(":") != 1:
            raise ValueError(f"malformed CFG_NATIVE_AGENT_CATALOG entry: {token!r}")
        name, profile = token.split(":", 1)
        entries.append((name, profile))
    return entries


def toml_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def toml_multiline(text: str) -> str:
    return text.replace('"""', '\\"\\"\\"')


def codex_config(cfg: Mapping[str, str], profile: str) -> tuple[str, str, str]:
    key = "CFG_PROFILE_" + profile.upper().replace("-", "_")
    tier, effort, sandbox = (cfg.get(key) or cfg["CFG_PROFILE_DEFAULT"]).split(":")
    return cfg[f"CFG_TIER_{tier.upper()}_MODEL"], effort, sandbox


def render_kernel_agent(cfg: Mapping[str, str], name: str, spec: dict[str, str]) -> str:
    model, reasoning, sandbox = codex_config(cfg, name)
    return f'''name = "{toml_string(name)}"
description = "{toml_string(spec["description"])}"
model = "{toml_string(model)}"
model_reasoning_effort = "{toml_string(reasoning)}"
sandbox_mode = "{toml_string(sandbox)}"
developer_instructions = """
{toml_multiline(spec["instructions"])}"""
'''


def render_catalog_agent(cfg: Mapping[str, str], name: str, profile: str) -> str:
    spec = CATALOG_AGENTS.get(name)
    if spec is None:
        raise ValueError(f"catalog names an agent with no Codex spec: {name}")
    resolved = resolve_profile_values("codex", cfg, profile)
    return f'''name = "{toml_string(name)}"
description = "{toml_string(spec["description"])}"
model = "{toml_string(resolved["model"])}"
model_reasoning_effort = "{toml_string(resolved["budget"])}"
sandbox_mode = "{toml_string(CATALOG_SANDBOX)}"
developer_instructions = """
{toml_multiline(spec["instructions"])}"""
'''


def render_agents(cfg: Mapping[str, str], kernel_names: list[str]) -> dict[str, str]:
    """Render every kernel + catalog agent TOML body from one config mapping.

    ``kernel_names`` is the manifest's ``kernel.agents`` list. Returns a
    filename→body mapping sorted by filename. Raises ``ValueError`` on an
    unknown kernel name or a malformed catalog entry. Reads no runtime home
    and writes no file — the mapping in, the strings out.
    """
    expected: dict[str, str] = {}
    for name in kernel_names:
        spec = KERNEL_AGENTS.get(name)
        if spec is None:
            raise ValueError(f"unknown kernel agent (no Codex projection defined): {name}")
        expected[f"{name}.toml"] = render_kernel_agent(cfg, name, spec)
    for name, profile in parse_native_catalog(cfg):
        expected[f"{name}.toml"] = render_catalog_agent(cfg, name, profile)
    return dict(sorted(expected.items()))
