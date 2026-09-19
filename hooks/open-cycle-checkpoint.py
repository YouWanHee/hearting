#!/usr/bin/env python3
"""Stop hook: refresh this session's open-cycle interim manifest.

Launches `artifact_producer.py checkpoint --trigger turn-end` detached through
`artifact_checkpoint_trigger`; silent, never blocks, never fails the turn.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "utilities"))


def main() -> int:
    try:
        import artifact_checkpoint_trigger

        return artifact_checkpoint_trigger.main(["turn-end", "--harness", "claude"])
    except Exception:  # noqa: BLE001 -- a turn-end refresh never fails the turn
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
