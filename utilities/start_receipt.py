#!/usr/bin/env python3
"""Best-effort storage and post-tool observation of actual start results."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from dispatch_contract import resolve_agent_home, resolve_dispatch_state_root
from session_identity import identity


def _directory(jobs=None):
    if jobs is not None:
        return Path(jobs).resolve().parent / "start-receipts"
    return resolve_dispatch_state_root(resolve_agent_home(), environ=os.environ) / "start-receipts"


def _key(harness, sid):
    return hashlib.sha256(f"{harness}\0{sid}".encode()).hexdigest()


def _write(path, value):
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
            name = file.name
            os.chmod(name, 0o600)
            json.dump(value, file, ensure_ascii=False)
        os.replace(name, path)
        name = None
    finally:
        if name is not None:
            os.unlink(name)


def save(result, jobs):
    """Keep the result byte values; storage failure never changes its directive."""
    try:
        who = identity()
        folder = _directory(jobs)
        folder.mkdir(parents=True, exist_ok=True)
        key = _key(who.harness, who.session_id) if who.known and who.session_id else _key("route", result["route_id"])
        path = folder / (key + ".json")
        receipt = {**result, "receipt_file": str(path)}
        record = {"schema": "start_receipt_v1", "id": uuid.uuid4().hex, "saved_at": time.time(),
                  "harness": who.harness if who.known else "", "session_id": who.session_id if who.known else "",
                  "receipt": receipt}
        with (folder / (key + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _write(path, record)
        return receipt
    except (OSError, ValueError, TypeError, KeyError):
        return result


def context(harness, sid, jobs=None):
    """Observe once; a receipt is a hint, never completion or launch authority."""
    if harness not in {"claude", "codex", "opencode"} or not sid:
        return ""
    try:
        folder = _directory(jobs)
        key = _key(harness, sid)
        path = folder / (key + ".json")
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4_000_000:
            return ""
        with (folder / (key + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            row = json.loads(path.read_text())
            if row.get("schema") != "start_receipt_v1" or (row.get("harness"), row.get("session_id")) != (harness, sid):
                return ""
            if not 0 <= time.time() - row["saved_at"] <= 3600:
                return ""
            receipt = row["receipt"]
            if not isinstance(receipt, dict) or not receipt.get("parent_next"):
                return ""
            seen = folder / (key + ".seen")
            if seen.exists() and json.loads(seen.read_text()) == row["id"]:
                return ""
            text = "[start-receipt] " + " ".join(f"{k}={receipt[k]}" for k in ("route_id", "state") if k in receipt)
            text += f"\nreceipt_file={path}"
            for name in ("parent_next", "parent_next_reason", "parent_next_command", "required_action", "resume_command"):
                if name in receipt:
                    text += f"\n{name}={receipt[name] or '-'}"
            if len(text.encode()) > 8000:
                text = f"[start-receipt] parent_next={receipt['parent_next']}\nRead the full receipt: {path}"
            _write(seen, row["id"])
            return text
    except (OSError, ValueError, TypeError, KeyError):
        return ""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", required=True, choices=("claude", "codex", "opencode"))
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args()
    text = context(args.harness, args.session_id)
    if text:
        print(text)


if __name__ == "__main__":
    main()
