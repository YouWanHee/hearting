#!/usr/bin/env python3
"""SD-120/121 activation as a checked runtime capability (PRD §13.53.2).

`§13.53.2` forbids activating the Claude terminal fast path through "a manual
switch the owner has to remember".  Activation must instead be *sealed* as
checked support on the route, and it must not open until the binding
(`§13.53.3`), the transaction (`§13.53.4`~`§13.53.5`) and the handoff claim
(`§13.36.3`) have all landed together -- hash correction alone re-creates the
D-2 regression `§13.36.6` already rejected.

This module is that check.  It censuses the **runtime root the route is bound
to** (never the caller's checkout) and reports a typed verdict.  The census is
a pure `ast` parse: it never imports or executes runtime code, so a
compose-time probe cannot be made to run a stale or foreign module's side
effects.

What it proves is **symbol existence, not behavior**.  A module that defines
the right names with the wrong semantics passes here and fails later inside
the transaction.  That is an accepted limit: the realistic adversary is a
partially-upgraded release, not a hostile stub, every surface below is
published together by one release, and the transaction is forward-recovering.
Two call sites this module cannot afford to get wrong are additionally checked
for their keyword names.

Fail-closed in every direction: an unreadable root, a syntactically broken
module, one missing symbol, or a contract-name disagreement all yield
`supported=False` with the reason naming what was missing.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Mapping, Optional

# The contract names the route publishes under `runtime_support`. A runtime
# that spells any of them differently is a different contract, not a newer
# one, so the probe refuses rather than guessing compatibility.
TERMINAL_COMMIT_CONTRACT = "terminal_commit_v1"
TERMINAL_HANDOFF_CONTRACT = "terminal_handoff_claim_v1"
PRODUCER_BINDING_CONTRACT = "producer_binding_v1"

# Every surface below is load-bearing for one of the three parts. Dropping a
# row here silently widens activation, so each row names why it is required.
REQUIRED_SURFACES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # §13.53.4~13.53.5 -- the transaction itself, its identity, its forward
    # state machine and the bounded cleanup capability it seals.
    ("utilities/dispatch_terminal_commit.py", (
        "publish_producer_binding",
        "load_producer_binding",
        "prove_terminal_authority",
        "settle_terminal_commit",
        "authorize_cleanup_operation",
        "terminal_commit_id",
        "producer_lifecycle_applies",
        "TERMINAL_RESULTS",
        "TERMINAL_REASONS",
    )),
    # §13.53.4(2) -- the claim is a fence against competing start / retry /
    # marker publication, shared through one helper under the jobs lock.
    ("utilities/dispatch_contract.py", (
        "TERMINAL_CLAIM_CONTRACT",
        "claim_terminal_route_locked",
        "terminal_claim_observation",
        "ensure_terminal_claim_absent",
    )),
    # §13.36.3 / §13.53.8 -- the post-join reserve conversion and the three
    # submission states with their single reconciler.
    ("utilities/dispatch_budget_record.py", (
        "SUBMISSION_STATES",
        "SUBMISSION_EVIDENCE",
        "claim_terminal_handoff",
        "convert_claim_to_prompt_intent",
        "begin_submission",
        "reconcile_submission",
        "settle_submission",
        "complete_terminal_handoff",
    )),
    # §13.53.5 -- finalize is bound to the one cycle the binding names, split
    # from root-wide recovery.
    ("utilities/artifact_producer.py", (
        "finalize_exact_cycle",
        "verify_finalized_cycle",
    )),
    # §13.53.2 -- the Claude adapter is the only consumer of the sealed flag;
    # the supervisor must carry both the gate and the settlement adapter.
    ("utilities/claude-session-supervisor.py", (
        "terminal_commit_enabled",
        "terminal_commit_adapter",
    )),
    # §13.53.4(3) -- the canonical lock-order table must be registered before
    # the fence contract may be claimed at all.
    ("utilities/dispatch_lock_order.py", (
        "LOCK_ORDER",
        "acquired",
        "assert_not_held",
        "LockOrderError",
    )),
    # §13.53.2 / A82-1 -- the canonical four-key exclusion set. A runtime still
    # carrying the old two-key form would fail the hash comparison anyway, but
    # it would fail naming the wrong cause; refusing here names the real one.
    ("utilities/route_identity.py", (
        "ROUTE_HASH_EXCLUDED_KEYS",
        "route_hash",
        "route_id_from_hash",
    )),
    # §13.53.9 / A82-12 -- the cleanup scope is only real where it is enforced.
    # The helper living in `dispatch_terminal_commit` proves nothing if the
    # hook that calls it is absent from the runtime root, so census the
    # enforcement point too.
    ("hooks/material-route-guard.py", (
        "main",
    )),
)

# Call sites where an argument-name change would pass a name-only census and
# then fail inside the transaction. Cheap to check, so check them.
REQUIRED_SIGNATURES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("utilities/artifact_producer.py", "finalize_exact_cycle", ("cycle_id", "expected_binding")),
    ("utilities/dispatch_terminal_commit.py", "settle_terminal_commit", ("request",)),
)

# The route seals one of these; `off` is the operator kill switch.
SUPPORT_MODES = ("auto", "off")
DEFAULT_SUPPORT_MODE = "auto"


class _Verdict(dict):
    """A plain dict so the verdict is JSON-serialisable into evidence."""

    @property
    def supported(self) -> bool:
        return bool(self.get("supported"))


def _module_symbols(path: Path) -> Optional[set[str]]:
    """Top-level defs, classes and UPPER_CASE assignments, or None.

    `None` means "could not be read as Python at all" and is distinct from an
    empty set, so a truncated or binary file is never mistaken for a module
    that merely defines nothing.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, SyntaxError):
        return None
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _function_arguments(path: Path, name: str) -> Optional[set[str]]:
    """Argument names of one top-level function, or None if it is absent."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, SyntaxError):
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            arguments = node.args
            found = {argument.arg for argument in
                     list(arguments.args) + list(arguments.posonlyargs) + list(arguments.kwonlyargs)}
            if arguments.vararg:
                found.add(arguments.vararg.arg)
            if arguments.kwarg:
                # `**kwargs` forwards anything, so it satisfies any name.
                found.add("**")
            return found
    return None


def probe_runtime(runtime_root: Any) -> _Verdict:
    """Census `runtime_root` for the SD-120/121 contract surfaces.

    Returns a verdict dict with `supported`, `reason`, and -- when it refused
    -- `missing`, a sorted list of `<module>:<symbol>` (or `<module>` for a
    module that could not be parsed at all).
    """
    if not runtime_root:
        return _Verdict(supported=False, reason="runtime-root-unknown", missing=[])
    root = Path(runtime_root)
    if not root.is_dir():
        return _Verdict(supported=False, reason="runtime-root-unreadable", missing=[])
    missing: list[str] = []
    for relative, symbols in REQUIRED_SURFACES:
        present = _module_symbols(root / relative)
        if present is None:
            missing.append(relative)
            continue
        missing.extend(f"{relative}:{name}" for name in symbols if name not in present)
    if missing:
        return _Verdict(supported=False, reason="runtime-contract-incomplete",
                        missing=sorted(missing))
    for relative, function, arguments in REQUIRED_SIGNATURES:
        found = _function_arguments(root / relative, function)
        if found is None:
            missing.append(f"{relative}:{function}")
            continue
        if "**" in found:
            continue
        missing.extend(f"{relative}:{function}({name})"
                       for name in arguments if name not in found)
    if missing:
        return _Verdict(supported=False, reason="runtime-signature-mismatch",
                        missing=sorted(missing))
    return _Verdict(supported=True, reason="runtime-contract-complete", missing=[])


def query_support_mode(config: Optional[Mapping[str, Any]]) -> str:
    """Read `runtime.terminal_commit` from a parsed defaults config.

    Absent section or absent key yields the shipped default (`auto`), which is
    what lets the census decide. **Any other present value that is not exactly
    `auto` disables the gate.**

    That asymmetry is deliberate. `off` is a plain string to this repository's
    narrow YAML subset parser, but YAML 1.1 resolves bare `off`/`no` to boolean
    false -- so a reader swap would turn the operator's `off` into `False`,
    miss the string match, fall back to `auto`, and silently *open* a gate the
    operator had closed. Treating everything unrecognised as `off` makes the
    only failure direction the safe one: the legacy owner-driven close/finalize
    path. This reader stays total and never raises; the defaults validator owns
    telling the operator that the value was not understood.
    """
    if isinstance(config, Mapping):
        runtime = config.get("runtime")
        if isinstance(runtime, Mapping) and "terminal_commit" in runtime:
            mode = runtime.get("terminal_commit")
            if isinstance(mode, str) and mode.strip().lower() == DEFAULT_SUPPORT_MODE:
                return DEFAULT_SUPPORT_MODE
            return "off"
    return DEFAULT_SUPPORT_MODE


def terminal_commit_support(runtime_root: Any, config: Optional[Mapping[str, Any]] = None) -> _Verdict:
    """The sealed verdict: operator kill switch first, then the census."""
    mode = query_support_mode(config)
    if mode == "off":
        return _Verdict(supported=False, reason="operator-disabled", missing=[])
    verdict = probe_runtime(runtime_root)
    verdict["mode"] = mode
    return verdict


def _main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtime-root", required=True)
    arguments = parser.parse_args(argv)
    verdict = probe_runtime(arguments.runtime_root)
    print(json.dumps(verdict, sort_keys=True, ensure_ascii=False))
    return 0 if verdict.supported else 1


if __name__ == "__main__":  # pragma: no cover - CLI shim
    import sys

    raise SystemExit(_main(sys.argv[1:]))
