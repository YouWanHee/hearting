#!/usr/bin/env python3
"""Generate Codex-native custom agent projections for kernel agents.

Team agents are retired: former team behavior lives in the portable unit catalog
(`roles/units/**`) and runs as dispatched depth-2 nodes, never as native agents.
Only kernel helpers (`kernel.agents` in `harness-manifest.json`) project here.

Rendering itself lives in ``native_agent_renderer.py`` (a pure module shared with
``tools/install/native_agent_payload.py``, which renders the same TOML shapes from
a runtime's *effective* config). This script's ordinary and ``--check`` paths stay
shipped-config deterministic: they parse only the checked-in ``models.conf`` below
and never read a runtime home.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "adapters" / "codex" / "agents"
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness_manifest
import native_agent_renderer as renderer


MODELS_CONF = ROOT / "adapters" / "codex" / "config" / "models.conf"


def load_models_conf() -> dict[str, str]:
    """Parse the flat KEY=value config that is the sole source of concrete models."""
    cfg: dict[str, str] = {}
    for raw in MODELS_CONF.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if val[:1] in ('"', "'"):
            end = val.find(val[0], 1)
            val = val[1:end] if end != -1 else val[1:]
        else:
            hidx = val.find("#")
            if hidx != -1:
                val = val[:hidx].strip()
        cfg[key] = val
    return cfg


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify generated projections")
    args = parser.parse_args()

    cfg = load_models_conf()
    manifest = harness_manifest.load()
    try:
        expected_bodies = renderer.render_agents(cfg, manifest["kernel"]["agents"])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    expected: dict[Path, str] = {OUT / name: body for name, body in expected_bodies.items()}

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
