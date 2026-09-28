"""Pure Codex native-agent TOML renderer.

Shared by ``sync-native-agents.py``, which renders the committed
``adapters/codex/agents/*.toml`` from the shipped ``config/models.conf``, and
by ``tools/install/native_agent_payload.py``, which renders the same shapes from
a runtime home's effective model config. Every function takes an
already-parsed config mapping; none reads a runtime home or writes a file.
Instructions are adapter-owned output, not a Claude Agent copy.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Mapping

_UTILITIES = Path(__file__).resolve().parents[3] / "utilities"
if str(_UTILITIES) not in sys.path:
    sys.path.insert(0, str(_UTILITIES))

from model_profile import resolve_profile_values  # noqa: E402

# Bump when the rendered TOML shape or the agent specs below change, so an
# installed payload's identity changes with them.
RENDERER_VERSION = "1"


class RenderError(ValueError):
    """The config mapping cannot render the native agent set."""


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
CATALOG_SANDBOX = "workspace-write"

CATALOG_AGENTS = {
    "balanced": {
        "description": "Execute an approved decision across multiple steps on the balanced budget.",
        "instructions": """Carry out the approved execution scope and cite its decision handoff.
Stop and return new important choices or uncertainty to the judgment owner.
Do not make architecture or policy decisions, widen scope, or spawn further agents.
Report completed steps, verification and remaining gaps.
""",
    },
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
    entries = []
    for token in cfg.get("CFG_NATIVE_AGENT_CATALOG", "").split():
        if token.count(":") != 1:
            raise RenderError(f"malformed CFG_NATIVE_AGENT_CATALOG entry: {token!r}")
        name, profile = token.split(":", 1)
        if profile == "top":
            # A native subagent can never run the main-session-only model
            # (the admission hook refuses it at every spawn), so a catalog
            # entry pinning it would only generate a definition that always
            # fails (top review m4).
            raise RenderError(
                f"CFG_NATIVE_AGENT_CATALOG entry {token!r}: top is not a native agent profile"
            )
        entries.append((name, profile))
    return entries


def toml_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def toml_multiline(text: str) -> str:
    return text.replace('"""', '\\"\\"\\"')


def codex_config(cfg: Mapping[str, str], profile: str) -> tuple[str, str, str]:
    key = "CFG_PROFILE_" + profile.upper().replace("-", "_")
    try:
        tier, effort, sandbox = (cfg.get(key) or cfg["CFG_PROFILE_DEFAULT"]).split(":")
        return cfg[f"CFG_TIER_{tier.upper()}_MODEL"], effort, sandbox
    except (KeyError, ValueError) as exc:
        raise RenderError(f"native agent profile {key} cannot be resolved: {exc}") from exc


def _render(name: str, description: str, model: str, effort: str, sandbox: str,
            instructions: str) -> str:
    return f'''name = "{toml_string(name)}"
description = "{toml_string(description)}"
model = "{toml_string(model)}"
model_reasoning_effort = "{toml_string(effort)}"
sandbox_mode = "{toml_string(sandbox)}"
developer_instructions = """
{toml_multiline(instructions)}"""
'''


def render_agents(cfg: Mapping[str, str], kernel_names) -> dict[str, tuple[str, str]]:
    """Return ``{"<name>.toml": (model, body)}`` for kernel + catalog agents.

    ``kernel_names`` is the manifest's ``kernel.agents`` list. Raises
    ``RenderError`` for an unknown agent or an unresolvable profile.
    """
    rendered: dict[str, tuple[str, str]] = {}
    for name in kernel_names:
        spec = KERNEL_AGENTS.get(name)
        if spec is None:
            raise RenderError(f"unknown kernel agent (no Codex projection defined): {name}")
        model, effort, sandbox = codex_config(cfg, name)
        rendered[f"{name}.toml"] = (
            model,
            _render(name, spec["description"], model, effort, sandbox, spec["instructions"]),
        )
    for name, profile in parse_native_catalog(cfg):
        spec = CATALOG_AGENTS.get(name)
        if spec is None:
            raise RenderError(f"catalog names an agent with no Codex spec: {name}")
        try:
            resolved = resolve_profile_values("codex", cfg, profile)
        except ValueError as exc:
            raise RenderError(f"catalog agent {name}: {exc}") from exc
        rendered[f"{name}.toml"] = (
            resolved["model"],
            _render(name, spec["description"], resolved["model"], resolved["budget"],
                    CATALOG_SANDBOX, spec["instructions"]),
        )
    return dict(sorted(rendered.items()))
