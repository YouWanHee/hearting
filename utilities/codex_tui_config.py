"""Shared Codex TUI status-line preference merge and feasibility reader."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import tomllib


DEFAULT_STATUS_LINE = (
    "thread-title", "git-branch", "context-used", "model-with-reasoning",
    "five-hour-limit", "weekly-limit", "thread-id",
)
DEFAULT_STATUS_LINE_USE_COLORS = True
BACKUP_NAME = "config.toml.pre-harness-tui"
_MISSING = object()
_TABLE_RE = re.compile(r"^\s*(\[\[?[^\]]+\]\]?)\s*(?:#.*)?$")
_TUI_TABLE_RE = re.compile(r"^\s*\[tui\]\s*(?:#.*)?$")
_STATUS_KEY_RE = re.compile(r"^\s*(?:status_line|\"status_line\"|'status_line')\s*=")


class CodexTuiConfigError(ValueError):
    """The config cannot be safely merged without changing unrelated state."""


def _parse(text: str, label: str = "Codex config") -> dict:
    try:
        parsed = tomllib.loads(text) if text.strip() else {}
    except (tomllib.TOMLDecodeError, TypeError) as exc:
        raise CodexTuiConfigError(f"invalid TOML in {label}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CodexTuiConfigError(f"invalid TOML in {label}")
    return parsed


def _normalize(items):
    if not isinstance(items, (list, tuple)) or any(not isinstance(item, str) for item in items):
        raise CodexTuiConfigError("tui.status_line must be an array of strings")
    return [item for item in items if item != "thread-id"] + ["thread-id"]


def _array_end(lines, start, limit):
    """Find the closing bracket of a validated string array, ignoring strings/comments."""
    depth = 0
    quote = None
    triple = False
    escaped = False
    started = False
    for line_index in range(start, limit):
        line = lines[line_index]
        index = 0
        while index < len(line):
            char = line[index]
            if quote is not None:
                marker = quote * (3 if triple else 1)
                if quote == '"' and escaped:
                    escaped = False
                elif quote == '"' and char == "\\":
                    escaped = True
                elif line.startswith(marker, index):
                    quote = None
                    triple = False
                    index += len(marker) - 1
                index += 1
                continue
            if char == "#":
                break
            if char in ('"', "'"):
                triple = line.startswith(char * 3, index)
                quote = char
                index += 2 if triple else 0
            elif char == "[":
                depth += 1
                started = True
            elif char == "]" and started:
                depth -= 1
                if depth == 0:
                    suffix = line[index + 1:]
                    comment = re.search(r"\s+#.*$", suffix)
                    return line_index, (comment.group(0) if comment else "")
            index += 1
    raise CodexTuiConfigError("could not locate the end of tui.status_line")


def merge_status_line_text(text: str, defaults=DEFAULT_STATUS_LINE,
                           default_colors=DEFAULT_STATUS_LINE_USE_COLORS) -> str:
    """Return valid TOML with one thread-id item last; leave all other values intact."""
    current = _parse(text)
    tui = current.get("tui", {})
    if not isinstance(tui, dict):
        raise CodexTuiConfigError("[tui] must be a table")
    if (not isinstance(defaults, (list, tuple)) or any(not isinstance(item, str) for item in defaults)
            or "thread-id" not in defaults or type(default_colors) is not bool):
        raise CodexTuiConfigError("invalid harness TUI defaults")
    default_items = _normalize(defaults)
    raw_items = tui.get("status_line", _MISSING)
    items = default_items if raw_items is _MISSING else _normalize(raw_items)
    raw_colors = tui.get("status_line_use_colors", _MISSING)
    if raw_colors is not _MISSING and type(raw_colors) is not bool:
        raise CodexTuiConfigError("tui.status_line_use_colors must be a boolean")
    colors = default_colors if raw_colors is _MISSING else raw_colors

    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    tui_start = next((i for i, line in enumerate(lines) if _TUI_TABLE_RE.match(line)), None)
    if tui_start is None:
        if "tui" in current:
            raise CodexTuiConfigError("unsupported inline or dotted [tui] config")
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(("[tui]", "status_line = " + json.dumps(items, ensure_ascii=False),
                      "status_line_use_colors = " + str(colors).lower()))
    else:
        table_end = next((i for i in range(tui_start + 1, len(lines))
                          if _TABLE_RE.match(lines[i])), len(lines))
        status_index = next((i for i in range(tui_start + 1, table_end)
                             if _STATUS_KEY_RE.match(lines[i])), None)
        if raw_items is not _MISSING and status_index is None:
            raise CodexTuiConfigError("could not locate the parsed tui.status_line assignment")
        if status_index is None:
            lines.insert(table_end, "status_line = " + json.dumps(items, ensure_ascii=False))
            table_end += 1
        elif raw_items != items:
            end_index, suffix = _array_end(lines, status_index, table_end)
            indent = lines[status_index][:len(lines[status_index]) - len(lines[status_index].lstrip())]
            lines[status_index:end_index + 1] = [
                indent + "status_line = " + json.dumps(items, ensure_ascii=False) + suffix
            ]
            table_end -= end_index - status_index
        has_colors = any(re.match(r"^\s*(?:status_line_use_colors|\"status_line_use_colors\"|'status_line_use_colors')\s*=",
                                  lines[i]) for i in range(tui_start + 1, table_end))
        if not has_colors:
            lines.insert(table_end, "status_line_use_colors = " + str(colors).lower())

    result = newline.join(lines) + (newline if lines else "")
    _parse(result, "merged Codex config")
    return result


def status_line_capability(text: str) -> str:
    """Return capable, incapable, or unknown for an effective status_line setting."""
    try:
        current = _parse(text)
    except CodexTuiConfigError:
        return "unknown"
    tui = current.get("tui", {})
    if not isinstance(tui, dict):
        return "unknown"
    items = tui.get("status_line")
    if items is None:
        return "incapable"
    if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
        return "unknown"
    return "capable" if items and items[-1] == "thread-id" else "incapable"


def _status_line_override(argv) -> bool:
    """Whether argv makes the disk status-line setting unknown."""
    argv = list(argv or ())
    index = 0
    while index < len(argv):
        word = str(argv[index])
        if word in ("--profile", "-p"):
            return True
        if word in ("--config", "-c"):
            if index + 1 >= len(argv):
                return True
            setting = str(argv[index + 1]).split("=", 1)[0].strip()
            if setting == "tui" or setting == "tui.status_line" or setting.startswith("tui.status_line."):
                return True
            index += 2
            continue
        if word.startswith("--config=") or word.startswith("-c="):
            setting = word.split("=", 2)[1].strip() if word.count("=") > 1 else ""
            if setting == "tui" or setting == "tui.status_line" or setting.startswith("tui.status_line."):
                return True
        index += 1
    return False


def status_line_capability_for_home(home, *, argv=()) -> str:
    """Read one known Codex home; unreadable config and relevant CLI overrides stay unknown."""
    if _status_line_override(argv):
        return "unknown"
    path = Path(home).expanduser() / "config.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "incapable"
    except (OSError, UnicodeError):
        return "unknown"
    return status_line_capability(text)


def merge_status_line_from_fragment(text: str, fragment_path) -> str:
    fragment_path = Path(fragment_path)
    fragment = _parse(fragment_path.read_text(encoding="utf-8"), str(fragment_path))
    tui = fragment.get("tui")
    if not isinstance(tui, dict):
        raise CodexTuiConfigError(f"invalid TUI defaults in {fragment_path}")
    defaults = tui.get("status_line")
    colors = tui.get("status_line_use_colors", _MISSING)
    if colors is _MISSING:
        colors = DEFAULT_STATUS_LINE_USE_COLORS
    return merge_status_line_text(text, defaults, colors)


def apply_status_line(config_path, fragment_path, *, dry_run=False) -> dict:
    """Apply the shared transformation with the shell installer backup contract."""
    config_path, fragment_path = Path(config_path), Path(fragment_path)
    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    updated = merge_status_line_from_fragment(original, fragment_path)
    changed = updated != original
    if changed and not dry_run:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        write_path = config_path.resolve(strict=False) if config_path.is_symlink() else config_path
        if config_path.exists():
            backup = config_path.with_name(BACKUP_NAME)
            if not backup.exists():
                backup.write_bytes(config_path.read_bytes())
            mode = config_path.stat().st_mode & 0o777
        else:
            mode = 0o600
        descriptor, temporary = tempfile.mkstemp(dir=str(write_path.parent),
                                                   prefix=f".{write_path.name}.", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, write_path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return {"path": str(config_path), "fragment": str(fragment_path),
            "changed": changed, "status": "planned" if dry_run and changed else "ok"}
