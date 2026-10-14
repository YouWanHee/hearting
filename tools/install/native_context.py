"""Project the shared context budget into runtime-owned native settings."""
from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from contextlib import nullcontext
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "utilities"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_config
import safe_fs


class NativeContextError(ValueError):
    pass


def _toml_root_value(raw: bytes) -> tuple[int, int] | None:
    """Return the byte span of the single root numeric value, excluding comments."""
    try:
        text = raw.decode("utf-8")
        parsed = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise NativeContextError(f"invalid Codex config TOML: {exc}") from exc
    value = parsed.get("model_context_window")
    if value is not None and (type(value) is not int or value < 1):
        raise NativeContextError("Codex root model_context_window must be a positive integer")

    # TOML's root table ends at its first real table header. Config syntax has
    # already been validated, so a root assignment can be found line-wise.
    header = re.compile(r'^\s*\[')
    key = re.compile(r'^\s*(?:model_context_window|"model_context_window")\s*=\s*')
    offset = 0
    multiline = None
    escaped = False
    found = None
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        at_root = multiline is None
        if at_root and header.match(body):
            break
        match = key.match(body) if at_root else None
        if match:
            tail = body[match.end():]
            number = re.match(r"[+-]?\d+", tail)
            if not number:
                raise NativeContextError("Codex root model_context_window is not a plain integer")
            end = offset + match.end() + number.end()
            found = (offset + match.end(), end)
            break
        # Track TOML multiline strings so a header/key-shaped string body is
        # never mistaken for root syntax.
        index = 0
        while index < len(body):
            char = body[index]
            if multiline:
                if multiline == '"""':
                    if body.startswith('"""', index) and not escaped:
                        multiline = None
                        index += 3
                        continue
                elif multiline == "'''" and body.startswith("'''", index):
                    multiline = None
                    index += 3
                    continue
                if multiline == '"""' and char == "\\" and not escaped:
                    escaped = True
                else:
                    escaped = False
                index += 1
                continue
            if body.startswith('"""', index):
                multiline = '"""'; index += 3; continue
            if body.startswith("'''", index):
                multiline = "'''"; index += 3; continue
            if char == "#":
                break
            index += 1
        offset += len(line)
    if found is None:
        return None
    return (len(text[:found[0]].encode("utf-8")),
            len(text[:found[1]].encode("utf-8")))


def _edit_codex(raw: bytes, desired: int) -> bytes:
    try:
        text = raw.decode("utf-8")
        tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise NativeContextError(f"invalid Codex config TOML: {exc}") from exc
    span = _toml_root_value(raw)
    if span:
        return raw[:span[0]] + str(desired).encode() + raw[span[1]:]
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    insert = len(text)
    offset = 0
    multiline = None
    for line in lines:
        body = line.rstrip("\r\n")
        if multiline is None and re.match(r"^\s*\[", body):
            insert = offset
            break
        index = 0
        while index < len(body):
            if multiline == '"""' and body.startswith('"""', index):
                multiline = None; index += 3; continue
            if multiline == "'''" and body.startswith("'''", index):
                multiline = None; index += 3; continue
            if multiline is None and body.startswith('"""', index):
                multiline = '"""'; index += 3; continue
            if multiline is None and body.startswith("'''", index):
                multiline = "'''"; index += 3; continue
            if multiline is None and body[index] == "#":
                break
            index += 1
        offset += len(line)
    prefix = text[:insert]
    suffix = text[insert:]
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += newline
    addition = f"model_context_window = {desired}{newline}"
    candidate = (prefix + addition + suffix).encode("utf-8")
    try:
        tomllib.loads(candidate.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise NativeContextError(f"Codex context projection is invalid: {exc}") from exc
    return candidate


def _publish(path: Path, candidate: bytes, *, owner: str, source: bytes,
             already_locked=False) -> bool:
    current = safe_fs.capture_state(path, include_payload=True)
    if current.kind == "missing":
        before = b""
        mode = 0o600
    elif current.kind == "file":
        if current.mode is None or current.payload is None:
            raise NativeContextError(f"cannot safely read native config: {path}")
        if os.lstat(path).st_nlink != 1:
            raise NativeContextError(f"native config has a hardlink alias: {path}")
        before, mode = current.payload, current.mode
    else:
        raise NativeContextError(f"native config must be a regular file: {path}")
    if before == candidate:
        return False
    try:
        with (nullcontext() if already_locked else safe_fs.TargetLock(path)):
            current = safe_fs.capture_state(path, include_payload=True)
            if current.kind == "missing":
                if source:
                    raise NativeContextError(f"native config changed concurrently: {path}")
                before, mode = b"", 0o600
            elif current.kind == "file" and current.payload is not None:
                if current.payload != source:
                    raise NativeContextError(f"native config changed concurrently: {path}")
                if os.lstat(path).st_nlink != 1:
                    raise NativeContextError(f"native config has a hardlink alias: {path}")
                before, mode = current.payload, current.mode or 0o600
            else:
                raise NativeContextError(f"native config must be a regular file: {path}")
            if before == candidate:
                return False
            authority = safe_fs.authority(path, owner=owner, allowed_paths=(path,),
                                          allow_leaf_symlink=False, expected=current)
            safe_fs.atomic_write_bytes(authority, candidate, mode, expected=current,
                                       create_parents=True)
    except safe_fs.SafetyError as exc:
        raise NativeContextError(str(exc)) from exc
    return True


def project_context_window(adapter: str, *, runtime, source_root, previous=None,
                           scope="global", claude_merge: Callable | None = None,
                           context_only=False, native_locked=False):
    """Resolve once and project the selected scalar to the explicit runtime home."""
    if adapter not in model_config.ADAPTERS:
        raise NativeContextError(f"unknown harness: {adapter}")
    runtime_home = Path(runtime).expanduser().absolute()
    values, receipt = model_config.resolve_config(adapter, runtime=runtime_home,
                                                 source_root=source_root)
    desired, provenance = model_config.resolve_context_window(values)
    number = int(desired)
    if adapter == "opencode":
        return {"kind": "context-window-projected", "runtime": adapter,
                "path": None, "previous": None, "desired": desired,
                "changed": False, "source": receipt.source,
                "report_lines": [f"context window: OpenCode reads {desired} at runtime startup"]}
    if adapter == "claude":
        if claude_merge is None:
            raise NativeContextError("Claude context projection requires the existing settings merger")
        result = claude_merge(context_window=desired, context_only=context_only,
                              native_locked=native_locked)
        return {"kind": "context-window-projected", "runtime": adapter,
                "path": result.get("path"), "previous": result.get("previous_context_window"),
                "desired": desired, "changed": bool(result.get("context_changed")),
                "source": receipt.source, "merge": result,
                "report_lines": list(result.get("report_lines", []))}
    path = runtime_home / "config.toml"
    original = path.read_bytes() if path.exists() else b""
    old = None
    if original:
        try:
            parsed = tomllib.loads(original.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise NativeContextError(f"invalid Codex config TOML: {exc}") from exc
        root = parsed.get("model_context_window")
        if root is not None and type(root) is not int:
            raise NativeContextError("Codex root model_context_window must be an integer")
        old = str(root) if root is not None else None
    candidate = _edit_codex(original, number)
    changed = _publish(path, candidate, source=original,
                       owner="native-context:codex-root-window",
                       already_locked=native_locked)
    lines = [] if old == desired and not changed else [
        f"context window: codex {old or 'unset'} -> {desired} tokens", f"config: {path}"]
    # A profile-level override wins at runtime; expose it without changing it.
    parsed = tomllib.loads(candidate.decode("utf-8"))
    profile = parsed.get("profile")
    profile_table = parsed.get("profiles", {}).get(profile, {}) if isinstance(profile, str) else {}
    effective = profile_table.get("model_context_window") if isinstance(profile_table, dict) else None
    if effective is not None and effective != number:
        lines.append(f"effective Codex profile model_context_window={effective} overrides root={desired}")
    return {"kind": "context-window-projected", "runtime": adapter,
            "path": str(path), "previous": old, "desired": desired,
            "changed": changed, "source": receipt.source, "provenance": provenance,
            "report_lines": lines}
