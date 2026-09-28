"""Opt-in same-work identity gates shared by dispatch consumers."""

import os
import sys


def gates_on():
    return os.environ.get("HEARTING_GATES", "off") == "on"


def same_work_or_refuse(reason, detail=""):
    if gates_on():
        # Late import keeps dispatch_contract free to use this helper itself.
        from dispatch_contract import DispatchContractError
        raise DispatchContractError(reason, detail)
    message = f"hearting: gate-off {reason} {detail}"
    print(" ".join(message.splitlines()), file=sys.stderr)
