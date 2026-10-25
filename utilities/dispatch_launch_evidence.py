#!/usr/bin/env python3
"""Bounded, best-effort launch diagnostics shared by all harnesses."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import traceback

from dispatch_contract import parse_registry_metadata, validate_dispatch_log_dir

OUTPUT_TAIL_BYTES = 65536
_SAFE_NAME = re.compile(r"[A-Za-z0-9._-]+")


def _stem(slug: str, attempt: str, harness: str) -> str:
    if (harness not in {"claude", "codex", "opencode"}
            or not attempt.startswith("att-")
            or any(not _SAFE_NAME.fullmatch(value) or value in {".", ".."}
                   for value in (slug, attempt))):
        raise ValueError("invalid diagnostic log identity")
    return f"{slug}.{attempt}.{harness}"


def _tail(value: str) -> str:
    # Ignore only a leading partial UTF-8 character at the byte boundary.
    return value.encode("utf-8", errors="replace")[-OUTPUT_TAIL_BYTES:].decode(
        "utf-8", errors="ignore")


def _write(path: Path, content: str) -> None:
    """Atomic private sidecar; never follow an existing file or special inode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=".launch-evidence-", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_wrapper_evidence(jobs: Path, leg: dict, returncode: int,
                          stdout: str, stderr: str, *, log_dir=None) -> None:
    try:
        directory = validate_dispatch_log_dir(jobs, log_dir)
        stem = _stem(leg["slug"], leg["attempt_id"], leg["adapter"])
        record = {
            "schema_version": 1,
            "attempt_id": leg["attempt_id"],
            "harness": leg["adapter"],
            "returncode": returncode,
            "stdout_tail": _tail(stdout),
            "stderr_tail": _tail(stderr),
        }
        _write(directory / f"{stem}.wrapper.json",
               json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        # Diagnostics never turn an existing receipt into a collection failure.
        pass


def save_fence_failure(jobs: Path, attempt: str, exc: BaseException) -> None:
    try:
        # Read only. In particular, malformed metadata that caused the launch
        # failure is not repaired or passed through another registry writer.
        matches = []
        with jobs.open(encoding="utf-8", errors="replace") as registry:
            for line in registry:
                fields = line.rstrip("\n").split("\t")
                if len(fields) == 6:
                    metadata = parse_registry_metadata(fields[5])
                    if metadata.get("attempt_id") == attempt:
                        matches.append((fields, metadata))
        if len(matches) != 1:
            return
        fields, metadata = matches[0]
        stem = _stem(fields[4], attempt, metadata.get("harness", ""))
        log = Path(metadata["log_file"])
        directory = validate_dispatch_log_dir(jobs, log.parent)
        if log != directory / f"{stem}.jsonl":
            return
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        reason = getattr(exc, "reason", "")
        _write(directory / f"{stem}.launch-fence.txt",
               (f"reason={str(reason)[:160]}\n" if reason else "") + _tail(detail))
    except Exception:
        # This also covers absent/ambiguous rows and an unwritable log directory.
        pass
