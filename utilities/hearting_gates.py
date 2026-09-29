"""Opt-in same-work identity gates shared by dispatch consumers."""

import os
import sys

_REPORTED = set()


def gates_on():
    return os.environ.get("HEARTING_GATES", "off") == "on"


def same_work_or_refuse(reason, detail=""):
    if gates_on():
        # Late import keeps dispatch_contract free to use this helper itself.
        from dispatch_contract import DispatchContractError
        raise DispatchContractError(reason, detail)
    message = " ".join(f"hearting: gate-off {reason} {detail}".splitlines())
    # Long-lived readers (Fleet ticks every 2 s) meet the same stale row again and
    # again; one line per distinct diagnostic is the whole signal.
    if message in _REPORTED:
        return
    _REPORTED.add(message)
    print(message, file=sys.stderr)
