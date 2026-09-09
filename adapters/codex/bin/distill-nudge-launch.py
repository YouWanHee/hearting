#!/usr/bin/env python3
"""Launch incremental extraction without holding the synchronous prompt hook."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "utilities"))
from memory_session_completion import (  # noqa: E402
    CompletionError, excluded, identity_state, launch, read_receipt,
)

HARNESS = "codex-nudge"
WORKER = ROOT / "adapters/codex/bin/distill-worker.sh"


def wait_for_nudge(sid: str, *, timeout: float = 900.0) -> None:
    """Join the earlier main-owned worker without a model or memory operation.

    A failed terminal extraction leaves its delta for the normal curator
    fallback. Unknown ownership or an unfinished deadline is not a completed
    join. The enclosing SessionEnd controller also bounds this wait.
    """
    started = time.monotonic()
    deadline = started + timeout
    while True:
        receipt = read_receipt(HARNESS, sid)
        if receipt is None or receipt["state"] in ("completed", "failed"):
            return
        identities = [receipt[k] for k in ("runner", "command") if receipt[k]]
        if identities:
            states = [identity_state(value) for value in identities]
            if "unknown" in states or "alive" not in states:
                raise CompletionError("nudge-completion-unavailable")
        # A spawned runner may not have published its identity yet. Its launcher's
        # exit does not prove failure; wait within the same finite join deadline.
        if time.monotonic() >= deadline:
            raise CompletionError("nudge-completion-timeout")
        time.sleep(0.05)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("session_id")
    parser.add_argument("cwd")
    args = parser.parse_args()
    try:
        if args.wait:
            # This is the main-owned completion controller, not a model worker.
            # Every other D-42 exclusion still applies before receipt access.
            wait_env = dict(os.environ)
            wait_env.pop("MEM_SESSION_COMPLETION", None)
            if not excluded(wait_env):
                wait_for_nudge(args.session_id)
            return 0
        if excluded() or os.environ.get("CODEX_DISTILL_ENABLE") != "1":
            return 0
        result = launch(
            HARNESS, args.session_id, args.cwd, str(WORKER),
            [args.session_id, args.cwd, "increment"],
            # Each counter firing is a new generation without conversation data.
            # The existing session lease suppresses an active repeat.
            input_generation=secrets.token_hex(32),
        )
        if result["state"] == "failed":
            raise CompletionError(result["reason"])
        return 0
    except (CompletionError, OSError) as exc:
        reason = exc.reason if isinstance(exc, CompletionError) else "launcher-unavailable"
        sys.stderr.write(f"codex incremental memory: {reason}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
