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
    ``runtime-hook`` -- the carrier binds the session itself.
``without_carrier``
    What a direct registered launch does when the carrier cannot reach the
    parent: ``poll`` -- the receipt discloses a bounded wait; ``refuse`` -- the
    launch stops before spawn, unless an operator authorizes the bounded wait.

``session_identity`` says how a session of that harness is identified:

``env``
    The variables, current name first, that carry the native session id the
    runtime exports to the commands it runs (`session_identity` reads them).
``process_proof``
    The native source that proves which session a running process is on:
    ``session-registry`` (Claude ``sessions/<pid>.json``), ``open-rollout``
    (the Codex rollout a process holds), ``tui-selection`` (the OpenCode TUI's
    own selection record). A process without it proves only its harness.
``herdr_session_id``
    How far herdr's reported ``agent_session`` value may be taken as the
    session id: ``verified`` -- the publisher proved it from the process;
    ``claim`` -- a reported value that is not taken as proof.

A declaration is data, never an approval step: reading it adds no gate.
"""
from __future__ import annotations

import functools
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESSES = ("claude", "codex", "opencode")
SCHEMA_VERSION = 1
PARENT_PROOFS = frozenset({"native-session", "runtime-hook"})
WITHOUT_CARRIER = frozenset({"poll", "refuse"})
PARENT_COMPLETION_KEYS = ("carrier", "reason", "parent_proof", "without_carrier")
SESSION_IDENTITY_KEYS = ("env", "process_proof", "herdr_session_id")
PROCESS_PROOFS = frozenset({"session-registry", "open-rollout", "tui-selection"})
HERDR_SESSION_IDS = frozenset({"verified", "claim"})
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
    identity = value.get("session_identity")
    if not isinstance(identity, dict) or sorted(identity) != sorted(SESSION_IDENTITY_KEYS):
        refuse("session_identity")
    names = identity["env"]
    if (not isinstance(names, list) or not names
            or not all(isinstance(name, str) and name.isidentifier() for name in names)):
        refuse("session_identity.env")
    if identity["process_proof"] not in PROCESS_PROOFS:
        refuse("session_identity.process_proof")
    if identity["herdr_session_id"] not in HERDR_SESSION_IDS:
        refuse("session_identity.herdr_session_id")
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


def session_identity(harness: str) -> dict:
    """How a session of ``harness`` is identified (see the module doc)."""
    return dict(capabilities(harness)["session_identity"])


def session_env() -> dict[str, tuple[str, ...]]:
    """``{harness: (variable, ...)}`` -- the session id variables every adapter declares."""
    return {harness: tuple(capabilities(harness)["session_identity"]["env"]) for harness in HARNESSES}


def herdr_verified_harnesses() -> frozenset[str]:
    """Harnesses whose herdr ``agent_session`` value is a proven session id."""
    return frozenset(harness for harness in HARNESSES
                     if capabilities(harness)["session_identity"]["herdr_session_id"] == "verified")
