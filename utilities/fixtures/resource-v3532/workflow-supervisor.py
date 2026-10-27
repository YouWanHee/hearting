"""Narrow support shim for the v3.53.2 resource consumer fixture.

Only the legacy runner loader is versioned here. Other ledger/watch services are
provided by the current test runtime and injected by the test.
"""
import importlib.util
from pathlib import Path

_CURRENT_SUPPORT = None
_RUNNER = None


def bind_runtime_support(module):
    global _CURRENT_SUPPORT
    _CURRENT_SUPPORT = module


def poll_once(route, ledger):
    # The current operation observer records each run without advancing the
    # logical stage. The legacy waiter expects its old per-run intermediate
    # state, so mirror that state through the current evaluator in this temp
    # ledger before the old wait loop inspects it.
    _CURRENT_SUPPORT.poll_once(route, ledger)
    for armed in read_armed(ledger).values():
        if armed.get("predecessor_kind") != "resource":
            continue
        evidence = _CURRENT_SUPPORT.resource_evidence(armed)
        if evidence.get("terminal"):
            _CURRENT_SUPPORT._evaluate(route, ledger, armed, [], observed_evidence=evidence)


def runner():
    global _RUNNER
    if _RUNNER is None:
        path = Path(__file__).with_name("resource-runner.py")
        spec = importlib.util.spec_from_file_location("resource_runner_v3532_fixture", path)
        if spec is None or spec.loader is None:
            raise RuntimeError("v3.53.2 runner fixture is unavailable")
        _RUNNER = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_RUNNER)
    return _RUNNER


def __getattr__(name):
    if _CURRENT_SUPPORT is None:
        raise AttributeError(name)
    return getattr(_CURRENT_SUPPORT, name)
