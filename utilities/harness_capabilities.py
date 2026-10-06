#!/usr/bin/env python3
"""What each harness runtime can do, as its adapter declares it.

Every adapter ships ``adapters/<harness>/config/harness-capabilities.json``.
The shared layer reads these declarations where it would otherwise branch on a
harness name: an adapter translates execution and declares what its runtime
has, and the decision stays in the shared module that reads the declaration.

``parent_completion`` says how a parent session of that harness learns that a
direct registered dispatch-depth-1 child finished:

``carrier``
    The ``parent_completion_delivery`` its runtime carries to the parent
    without a model turn, or null when the parent has to wait for it itself.
``reason``
    The ``parent_completion_reason`` recorded when that carrier is selected.
``parent_proof``
    What must hold at launch for the carrier to reach the parent:
    ``native-session`` -- the calling session is the registered parent;
    ``runtime-hook`` -- the carrier binds the session itself;
    ``carrier-env`` -- the runtime running the carrier names, in the calling
    command's environment (``CARRIER_ENV``), the carrier and the exact
    session it carries as ``<carrier>:<session>``. A runtime that predates
    its carrier names nothing, so its parent keeps the fallback.
``without_carrier``
    What a direct registered launch does when the carrier cannot reach the
    parent: ``poll`` -- the receipt discloses a bounded wait; ``refuse`` -- the
    launch stops before spawn, unless an operator authorizes the bounded wait.

A declaration is data, never an approval step: reading it adds no gate.
"""
from __future__ import annotations

import functools
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESSES = ("claude", "codex", "opencode")
SCHEMA_VERSION = 1
PARENT_PROOFS = frozenset({"native-session", "runtime-hook", "carrier-env"})
CARRIER_ENV = "AGENT_PARENT_COMPLETION_CARRIER"
WITHOUT_CARRIER = frozenset({"poll", "refuse"})
PARENT_COMPLETION_KEYS = ("carrier", "reason", "parent_proof", "without_carrier")
# A parent named by no adapter has no runtime that could carry its completion.
NO_PARENT_CARRIER = {"carrier": None, "reason": None, "parent_proof": None,
                     "without_carrier": "poll"}


class HarnessCapabilityError(ValueError):
    pass


def declaration_path(harness: str, root: Path = ROOT) -> Path:
    return root / "adapters" / harness / "config" / "harness-capabilities.json"


def _validate(harness: str, value: object) -> dict:
    def refuse(detail: str):
        raise HarnessCapabilityError(f"harness-capabilities-invalid:{harness}:{detail}")

    if not isinstance(value, dict):
        refuse("not-an-object")
    if value.get("schema_version") != SCHEMA_VERSION:
        refuse("schema_version")
    if value.get("harness") != harness:
        refuse("harness")
    completion = value.get("parent_completion")
    if not isinstance(completion, dict) or sorted(completion) != sorted(PARENT_COMPLETION_KEYS):
        refuse("parent_completion")
    if completion["without_carrier"] not in WITHOUT_CARRIER:
        refuse("parent_completion.without_carrier")
    if completion["carrier"] is None:
        if completion["reason"] is not None or completion["parent_proof"] is not None:
            refuse("parent_completion.carrier")
    elif not (isinstance(completion["carrier"], str) and completion["carrier"]
              and isinstance(completion["reason"], str) and completion["reason"]
              and completion["parent_proof"] in PARENT_PROOFS):
        refuse("parent_completion.carrier")
    return value


@functools.lru_cache(maxsize=None)
def capabilities(harness: str, root: Path = ROOT) -> dict:
    """The validated declaration of one harness; an unreadable one is an error."""
    if harness not in HARNESSES:
        raise HarnessCapabilityError(f"harness-unknown:{harness}")
    path = declaration_path(harness, root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HarnessCapabilityError(f"harness-capabilities-unreadable:{harness}") from exc
    return _validate(harness, value)


def parent_completion(harness: str) -> dict:
    """How a parent of ``harness`` receives completion (see the module doc)."""
    if harness not in HARNESSES:
        return dict(NO_PARENT_CARRIER)
    return dict(capabilities(harness)["parent_completion"])


def declared_carriers() -> frozenset[str]:
    """Every parent completion carrier some adapter declares."""
    return frozenset(
        carrier for harness in HARNESSES
        if (carrier := capabilities(harness)["parent_completion"]["carrier"])
    )
