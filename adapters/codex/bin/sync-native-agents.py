#!/usr/bin/env python3
"""Generate Codex-native custom agent projections for kernel agents.

Team agents are retired: former team behavior lives in the portable unit catalog
(`roles/units/**`) and runs as dispatched depth-2 nodes, never as native agents.
Only kernel helpers (`kernel.agents` in `harness-manifest.json`) project here.

Rendering lives in ``native_agent_renderer.py``, shared with
``tools/install/native_agent_payload.py``. This generator stays deterministic:
it renders only from the checked-in shipped ``models.conf`` and never reads a
runtime home, so the committed ``adapters/codex/agents/*.toml`` are the shipped
profiles.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "adapters" / "codex" / "agents"
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness_manifest
import model_config
import native_agent_renderer as renderer
from native_agent_renderer import CATALOG_AGENTS, EXTRA_AGENTS, KERNEL_AGENTS  # noqa: F401


MODELS_CONF = ROOT / "adapters" / "codex" / "config" / "models.conf"


def load_models_conf() -> dict[str, str]:
    """Parse the shipped config with the same parser runtime selection uses."""
    return model_config.parse_config(MODELS_CONF)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify generated projections")
    args = parser.parse_args()

    manifest = harness_manifest.load()
    try:
        rendered = renderer.render_agents(load_models_conf(), manifest["kernel"]["agents"])
    except (renderer.RenderError, model_config.ModelConfigError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    expected: dict[Path, str] = {OUT / name: body for name, (_model, body) in rendered.items()}

    stale: list[str] = []
    for path, body in expected.items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != body:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")

    existing = sorted(OUT.glob("*.toml")) if OUT.exists() else []
    extras = [path for path in existing if path not in expected]
    if args.check:
        stale.extend(str(path.relative_to(ROOT)) for path in extras)
    else:
        for path in extras:
            path.unlink()

    if stale:
        print("Codex native agent projections are stale:", file=sys.stderr)
        for item in stale:
            print(f"  {item}", file=sys.stderr)
        return 1

    if not args.check:
        print(f"generated {len(expected)} Codex native agent projections")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
