#!/usr/bin/env python3
"""Claude PostToolUse(AskUserQuestion) bridge: keep the person's reply to a registered frame
question (utilities/frame_native_answer.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "utilities"))


def main() -> int:
    try:
        import frame_native_answer
    except Exception:  # noqa: BLE001 -- a missing recorder never affects the question
        return 0
    return frame_native_answer.main(["--claude"])


if __name__ == "__main__":
    raise SystemExit(main())
