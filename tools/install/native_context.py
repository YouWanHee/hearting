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


_CONTEXT_KEY = "model_context_window"
_ROOT_KEY = re.compile(r"""[ \t]*(?:model_context_window|"model_context_window"|'model_context_window')[ \t]*=[ \t]*""")
_INTEGER = re.compile(r"[+-]?(?:0x[0-9A-Fa-f_]+|0o[0-7_]+|0b[01_]+|[0-9_]+)")
_HEADER = re.compile(r"[ \t]*\[")


def _parse_toml(raw: bytes) -> tuple[str, dict]:
    try:
        text = raw.decode("utf-8")
        return text, tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise NativeContextError(f"invalid Codex config TOML: {exc}") from exc


def _skip_multiline(text: str, index: int, quote: str) -> int:
    """Return the offset after a multiline string whose body starts at ``index``."""
    while index < len(text):
        if quote == '"' and text[index] == "\\":
            index += 2
            continue
        if text.startswith(quote * 3, index):
            end = index + 3
            # Up to two quotes may sit directly before the closing delimiter.
            while end < len(text) and text[end] == quote and end - index < 5:
                end += 1
            return end
        index += 1
    return index


def _skip_string(text: str, index: int, quote: str) -> int:
    """Return the offset after a one-line string whose body starts at ``index``."""
    while index < len(text) and text[index] != "\n":
        if quote == '"' and text[index] == "\\":
            index += 2
            continue
        if text[index] == quote:
            return index + 1
        index += 1
    return index


def _root_scan(text: str) -> tuple[tuple[int, int] | None, int]:
    """Find the root key's value span and the first table header, outside strings and comments."""
    found = None
    index, line_start, depth = 0, True, 0
    while index < len(text):
        if line_start:
            line_start = False
            if depth == 0 and _HEADER.match(text, index):
                return found, index
            key = _ROOT_KEY.match(text, index) if depth == 0 and found is None else None
            if key:
                number = _INTEGER.match(text, key.end())
                if not number:
                    raise NativeContextError("Codex root model_context_window is not a plain integer")
                found = (key.end(), number.end())
        char = text[index]
        if char == "\n":
            line_start = True
            index += 1
        elif char == "#":
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline
        elif text.startswith('"""', index) or text.startswith("'''", index):
            index = _skip_multiline(text, index + 3, char)
        elif char in "\"'":
            index = _skip_string(text, index + 1, char)
        else:
            # A multi-line array or inline table may hold lines that look like headers.
            depth += 1 if char in "[{" else -1 if char in "]}" else 0
            index += 1
    return found, len(text)


def _verify_codex(original: dict, candidate: bytes, desired: int) -> None:
    """Only the root key may differ; anything else means the edit landed in the wrong place."""
    try:
        parsed = tomllib.loads(candidate.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise NativeContextError(f"Codex context projection is invalid: {exc}") from exc
    if parsed.get(_CONTEXT_KEY) != desired:
        raise NativeContextError("Codex context projection did not set the root model_context_window")
    before = {key: value for key, value in original.items() if key != _CONTEXT_KEY}
    after = {key: value for key, value in parsed.items() if key != _CONTEXT_KEY}
    if before != after and repr(before) != repr(after):
        raise NativeContextError("Codex context projection would change other settings; config left unchanged")


def _edit_codex(raw: bytes, desired: int) -> bytes:
    text, parsed = _parse_toml(raw)
    value = parsed.get(_CONTEXT_KEY)
    if value is not None and (type(value) is not int or value < 1):
        raise NativeContextError("Codex root model_context_window must be a positive integer")
    span, header = _root_scan(text)
    if span:
        candidate = (text[:span[0]] + str(desired) + text[span[1]:]).encode("utf-8")
    else:
        newline = "\r\n" if "\r\n" in text else "\n"
        prefix, suffix = text[:header], text[header:]
        if prefix and not prefix.endswith(("\n", "\r")):
            prefix += newline
        candidate = (prefix + f"{_CONTEXT_KEY} = {desired}{newline}" + suffix).encode("utf-8")
    _verify_codex(parsed, candidate, desired)
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
    try:
        values, receipt = model_config.resolve_config(adapter, runtime=runtime_home,
                                                     source_root=source_root)
        desired, provenance = model_config.resolve_context_window(values)
    except model_config.ModelConfigError as exc:
        raise NativeContextError(str(exc)) from exc
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
    try:
        original = path.read_bytes() if path.exists() else b""
    except OSError as exc:
        raise NativeContextError(f"cannot read Codex config: {exc}") from exc
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
