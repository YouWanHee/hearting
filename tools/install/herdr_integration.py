"""Managed, offline projection of the pinned Herdr integration assets.

Hearting owns these projections.  The official Herdr reporters are session-only;
Hearting's pane metadata projection remains a separate, non-lifecycle writer.
"""

from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path
from typing import Any

import safe_fs


VERSIONS = {"claude": 11, "codex": 10, "opencode": 13}
CLAUDE_SESSION_COMMAND = 'sh "$HOME/.claude/hooks/run-hook.sh" bash herdr-agent-state.sh session'
CODEX_SESSION_COMMAND = (
    "sh -c 'for root in \"${AGENT_HOME:-}\" \"$HOME/.codex/hearting\"; do "
    'if [ -f "$root/core/CORE.md" ] && '
    '[ -f "$root/adapters/codex/hooks/herdr-agent-state.sh" ]; then '
    'exec bash "$root/adapters/codex/hooks/herdr-agent-state.sh" session; '
    'fi; done; echo "agent harness Codex hook launcher not found" >&2; exit 69'
)
OPENCODE_TUI_V1 = "./tui/herdr-tui-session.js"
OPENCODE_TUI_V2 = "./tui/herdr-opencode"
OPENCODE_IDENTITY = "./tui/hearting-tui-identity.ts"
_JSONC_COMMENT = re.compile(r"//|/\*|\*/")


def _skip_space(text: str, index: int) -> int:
    while index < len(text):
        if text[index].isspace():
            index += 1
        elif text.startswith("//", index):
            end = text.find("\n", index + 2)
            return len(text) if end < 0 else _skip_space(text, end + 1)
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise ValueError("unterminated JSONC comment")
            index = end + 2
        else:
            break
    return index


def _string_end(text: str, index: int) -> int:
    if index >= len(text) or text[index] != '"':
        raise ValueError("expected JSON string")
    index += 1
    escaped = False
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return index + 1
        index += 1
    raise ValueError("unterminated JSON string")


def _value_end(text: str, index: int) -> int:
    index = _skip_space(text, index)
    if index >= len(text):
        raise ValueError("missing JSON value")
    if text[index] == '"':
        return _string_end(text, index)
    if text[index] not in "[{":
        end = index
        while end < len(text) and text[end] not in ",]}" and not text[end].isspace():
            end += 1
        if end == index:
            raise ValueError("invalid JSON value")
        return end
    opening = text[index]
    closing = "]" if opening == "[" else "}"
    index += 1
    while True:
        index = _skip_space(text, index)
        if index >= len(text):
            raise ValueError("unterminated JSON container")
        if text[index] == closing:
            return index + 1
        if opening == "{":
            index = _string_end(text, index)
            index = _skip_space(text, index)
            if index >= len(text) or text[index] != ":":
                raise ValueError("invalid JSON object")
            index = _value_end(text, index + 1)
        else:
            index = _value_end(text, index)
        index = _skip_space(text, index)
        if index < len(text) and text[index] == ",":
            index += 1
            continue
        if index < len(text) and text[index] == closing:
            return index + 1
        raise ValueError("invalid JSON container separator")


def _strip_jsonc(text: str) -> str:
    """Mask comments and trailing commas without changing string contents."""
    chars = list(text)
    index = 0
    in_string = False
    escaped = False
    while index < len(chars):
        char = chars[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            index += 1
            continue
        if text.startswith("//", index):
            end = text.find("\n", index + 2)
            end = len(chars) if end < 0 else end
            for pos in range(index, end):
                if chars[pos] != "\r":
                    chars[pos] = " "
            index = end
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise ValueError("unterminated JSONC comment")
            for pos in range(index, end + 2):
                if chars[pos] not in "\r\n":
                    chars[pos] = " "
            index = end + 2
            continue
        index += 1
    masked = "".join(chars)
    chars = list(masked)
    index = 0
    in_string = False
    escaped = False
    while index < len(chars):
        char = chars[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            index += 1
            continue
        if char == ",":
            following = index + 1
            while following < len(chars) and chars[following].isspace():
                following += 1
            if following < len(chars) and chars[following] in "]}":
                chars[index] = " "
        index += 1
    return "".join(chars)


def parse_jsonc(text: str) -> Any:
    return json.loads(_strip_jsonc(text))


def _root_properties(text: str) -> tuple[int, int, list[tuple[str, int, int]]]:
    start = _skip_space(text, 0)
    if start >= len(text) or text[start] != "{":
        raise ValueError("configuration root must be an object")
    close = _value_end(text, start) - 1
    items: list[tuple[str, int, int]] = []
    index = start + 1
    while True:
        index = _skip_space(text, index)
        if index >= close or text[index] == "}":
            break
        key_end = _string_end(text, index)
        key = json.loads(text[index:key_end])
        index = _skip_space(text, key_end)
        if index >= close or text[index] != ":":
            raise ValueError("invalid JSON object property")
        value_start = _skip_space(text, index + 1)
        value_end = _value_end(text, value_start)
        items.append((key, value_start, value_end))
        index = _skip_space(text, value_end)
        if index < close and text[index] == ",":
            index += 1
        elif index < close and text[index] != "}":
            raise ValueError("invalid JSON object separator")
    return start, close, items


def _array_items(text: str, start: int, end: int) -> list[tuple[int, int]]:
    if text[start] != "[" or text[end - 1] != "]":
        raise ValueError("plugin registration must be an array")
    items: list[tuple[int, int]] = []
    index = start + 1
    close = end - 1
    while True:
        index = _skip_space(text, index)
        if index >= close or text[index] == "]":
            return items
        value_end = _value_end(text, index)
        items.append((index, value_end))
        index = _skip_space(text, value_end)
        if index < close and text[index] == ",":
            index += 1
        elif index < close and text[index] != "]":
            raise ValueError("invalid plugin array separator")


def edit_plugin_list(text: str, *, key: str = "plugin", add: tuple[str, ...] = (),
                     remove: tuple[str, ...] = ()) -> str:
    """Edit one root plugin string array and retain JSONC comments/spacing."""
    data = parse_jsonc(text)
    if not isinstance(data, dict):
        raise ValueError("configuration root must be an object")
    root_start, root_close, props = _root_properties(text)
    prop = next(((start, end) for name, start, end in props if name == key), None)
    if prop is None:
        if remove:
            return text
        if key in data:
            raise ValueError("invalid plugin property")
        encoded = json.dumps(list(add), ensure_ascii=False)
        suffix = "," if props else ""
        return text[:root_start + 1] + f'{json.dumps(key)}: {encoded}{suffix}' + text[root_start + 1:]
    value_start, value_end = prop
    if not isinstance(data.get(key), list):
        raise ValueError(f"{key} property must be an array")
    if remove:
        for plugin in remove:
            while True:
                data = parse_jsonc(text)
                values = data.get(key) if isinstance(data, dict) else None
                if not isinstance(values, list) or plugin not in values:
                    break
                root_start, root_close, props = _root_properties(text)
                prop = next((pair for name, *pair in props if name == key), None)
                if prop is None:
                    raise ValueError("plugin property disappeared during removal")
                array_start, array_end = prop
                spans = _array_items(text, array_start, array_end)
                entries = [parse_jsonc(text[start:end]) for start, end in spans]
                if any(not isinstance(value, str) for value in entries):
                    raise ValueError("plugin entries must be strings")
                index = entries.index(plugin)
                start, end = spans[index]
                if index + 1 < len(spans):
                    comma = _skip_space(text, end)
                    if comma >= spans[index + 1][0] or text[comma] != ",":
                        raise ValueError("plugin separator not found")
                    remove_start, remove_end = start, comma + 1
                elif index > 0:
                    comma = _skip_space(text, spans[index - 1][1])
                    if comma >= start or text[comma] != ",":
                        raise ValueError("plugin separator not found")
                    remove_start, remove_end = comma, end
                else:
                    after = _skip_space(text, end)
                    remove_start, remove_end = start, end
                    if after < array_end - 1 and text[after] == ",":
                        remove_end = after + 1
                text = text[:remove_start] + text[remove_end:]
        return text
    for plugin in add:
        data = parse_jsonc(text)
        values = data.get(key)
        if plugin in values:
            continue
        root_start, root_close, props = _root_properties(text)
        prop = next((pair for name, *pair in props if name == key), None)
        if prop is None:
            raise ValueError("plugin property disappeared during merge")
        value_start, value_end = prop
        spans = _array_items(text, value_start, value_end)
        encoded = json.dumps(plugin, ensure_ascii=False)
        if spans:
            insert_at = spans[-1][1]
            text = text[:insert_at] + ", " + encoded + text[insert_at:]
        else:
            text = text[:value_end - 1] + encoded + text[value_end - 1:]
    return text


def _owned_state_hook(command: Any) -> bool:
    if not isinstance(command, str) or "herdr-agent-state.sh" not in command:
        return False
    return any(token in command for token in (
        "/.claude/hooks/", "/adapters/codex/hooks/", "$AGENT_HOME/",
        "${AGENT_HOME}/", "$root/adapters/codex/hooks/",
    ))


def migrate_hook_data(data: dict, runtime: str, *, register: bool = True) -> bool:
    """Replace Hearting-owned Herdr lifecycle writers with one SessionStart report."""
    if runtime not in {"claude", "codex"}:
        raise ValueError(f"unsupported hook runtime: {runtime}")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks property must be an object")
    command = CLAUDE_SESSION_COMMAND if runtime == "claude" else CODEX_SESSION_COMMAND
    changed = False
    for event, groups in list(hooks.items()):
        if not isinstance(groups, list):
            continue
        remaining = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                remaining.append(group)
                continue
            kept = [hook for hook in group["hooks"]
                    if not isinstance(hook, dict)
                    or hook.get("command") == command
                    or not _owned_state_hook(hook.get("command"))]
            changed |= len(kept) != len(group["hooks"])
            if kept:
                remaining.append(dict(group, hooks=kept))
            elif not group["hooks"]:
                remaining.append(group)
        if remaining:
            hooks[event] = remaining
        elif groups:
            hooks.pop(event, None)
    if not register:
        return changed
    start_groups = hooks.setdefault("SessionStart", [])
    if not isinstance(start_groups, list):
        raise ValueError("SessionStart hooks must be an array")
    existing = any(
        isinstance(group, dict)
        and any(isinstance(hook, dict) and hook.get("command") == command
                for hook in group.get("hooks", []))
        for group in start_groups
    )
    if not existing:
        start_groups.append({"matcher": "*", "hooks": [{"type": "command", "command": command,
                                                            "timeout": 10 if runtime == "claude" else 5}]})
        changed = True
    return changed


def _write_config(path: Path, before: bytes | None, after: bytes, *, dry_run: bool) -> dict:
    if dry_run:
        return {"status": "planned", "changed": before != after}
    with safe_fs.TargetLock(path):
        expected = safe_fs.capture_state(path, include_payload=True)
        if before is not None and (expected.kind != "file" or expected.payload != before):
            return {"status": "changed-during-migration", "changed": False}
        if before is None and expected.kind != "missing":
            return {"status": "appeared-during-migration", "changed": False}
        auth = safe_fs.authority(path, owner="herdr-integration:migration",
                                 allowed_paths=(path,), expected=expected)
        safe_fs.atomic_write_bytes(auth, after, 0o600 if before is None else path.stat().st_mode & 0o777)
    return {"status": "updated", "changed": True}


def migrate_hook_file(path: Path, runtime: str, *, dry_run: bool = False,
                      source_root: Path | None = None) -> dict:
    """Migrate an existing harness hook file without following user symlinks."""
    path = Path(path)
    result = {"action": f"herdr-{runtime}-session-hook", "dest": str(path), "status": "unchanged"}
    if path.is_symlink():
        result["status"] = "source-managed"
        return result
    if not path.exists():
        return result
    try:
        before = path.read_bytes()
        data = json.loads(before.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("configuration root must be an object")
        command = CLAUDE_SESSION_COMMAND if runtime == "claude" else CODEX_SESSION_COMMAND
        prior_hooks = data.get("hooks", {})
        was_registered = isinstance(prior_hooks, dict) and any(
            isinstance(group, dict) and any(
                isinstance(hook, dict) and hook.get("command") == command
                for hook in group.get("hooks", []))
            for group in prior_hooks.get("SessionStart", [])
        )
        if source_root is None:
            source_root = Path(__file__).resolve().parents[2]
        asset = (Path(source_root) / "hooks/herdr-agent-state.sh" if runtime == "claude"
                 else Path(source_root) / "adapters/codex/hooks/herdr-agent-state.sh")
        expected_version = VERSIONS[runtime]
        register = version_marker(asset, runtime, expected_version)
        changed = migrate_hook_data(data, runtime, register=register)
        after = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if not changed or after == before:
            return result
        written = _write_config(path, before, after, dry_run=dry_run)
        receipt = ({"event": "SessionStart", "command": command}
                   if register and not was_registered
                   and written["status"] in {"updated", "planned"}
                   else None)
        return {**result, **written, "managed_config": receipt,
                "detail": "session reporter registered" if register else "stale Herdr writer removed; integration unavailable"}
    except (OSError, UnicodeError, ValueError, TypeError, safe_fs.SafetyError) as exc:
        return {**result, "status": "blocked", "detail": str(exc)}


def _config_candidates(home: Path, name: str) -> tuple[Path, ...]:
    stem = name.removesuffix(".json")
    return (home / f"{stem}.jsonc", home / f"{stem}.json")


def migrate_opencode_configs(home: Path, source_root: Path, *, dry_run: bool = False,
                             previous: dict | None = None) -> dict:
    """Register the V1 and V2 native TUI entrypoints, preserving JSONC/user entries."""
    home, source_root = Path(home), Path(source_root)
    tui_asset = source_root / "adapters/opencode/tui/herdr-tui-session.js"
    v2_asset = source_root / "adapters/opencode/tui/herdr-opencode/tui.js"
    identity_asset = source_root / "adapters/opencode/tui/hearting-tui-identity.ts"
    identity = (OPENCODE_IDENTITY,) if identity_asset.is_file() else ()
    specs = (
        ("tui", "tui.json", "plugin",
         identity + ((OPENCODE_TUI_V1,) if version_marker(tui_asset, "opencode-tui", 13) else ())),
        ("cli", "cli.json", "plugins", identity + ((OPENCODE_TUI_V2,)
                                         if version_marker(tui_asset, "opencode-tui", 13)
                                         and version_marker(v2_asset, "opencode-tui-v2", 13)
                                         else ())),
    )
    receipts = []
    actions = []
    previous_files = (previous or {}).get("files", {})
    for key, template_name, config_key, desired in specs:
        if not desired:
            actions.append({"action": f"herdr-opencode-{key}", "status": "skipped",
                            "detail": "optional integration assets are absent", "changed": False})
            continue
        candidates = _config_candidates(home, template_name)
        target = next((p for p in candidates if p.exists() or p.is_symlink()), candidates[-1])
        template = source_root / "adapters" / "opencode" / "tui" / template_name
        created = False
        seeded = None
        added: list[str] = []
        if target.is_symlink():
            actions.append({"action": f"herdr-opencode-{key}", "dest": str(target),
                            "status": "source-managed", "changed": False})
            continue
        if target.exists() and not target.is_file():
            actions.append({"action": f"herdr-opencode-{key}", "dest": str(target),
                            "status": "blocked", "detail": "configuration is not a regular file"})
            continue
        if not target.exists() and not template.is_file():
            actions.append({"action": f"herdr-opencode-{key}", "dest": str(target),
                            "status": "skipped", "detail": "optional integration template is absent",
                            "changed": False})
            continue
        try:
            if target.is_file():
                before = target.read_bytes()
                text = before.decode("utf-8")
                parsed = parse_jsonc(text)
                if not isinstance(parsed, dict):
                    raise ValueError("configuration root must be an object")
                for value in desired:
                    if value not in parsed.get(config_key, []):
                        added.append(value)
                updated_text = edit_plugin_list(text, key=config_key, add=desired)
                after = updated_text.encode("utf-8")
            else:
                before = None
                template_text = template.read_text(encoding="utf-8")
                parsed = parse_jsonc(template_text)
                if not isinstance(parsed, dict) or any(value not in parsed.get(config_key, []) for value in desired):
                    raise ValueError(f"{template_name} template lacks required plugin registrations")
                unavailable = tuple(value for value in (OPENCODE_TUI_V1, OPENCODE_TUI_V2,
                                                        OPENCODE_IDENTITY) if value not in desired)
                if unavailable:
                    template_text = edit_plugin_list(template_text, key=config_key, remove=unavailable)
                after = template_text.encode("utf-8")
                created = True
                seeded = after.decode("utf-8")
                added = list(desired)
            changed = before != after
            status = "unchanged"
            if changed:
                outcome = _write_config(target, before, after, dry_run=dry_run)
                status = outcome["status"]
                if status == "blocked":
                    raise ValueError(outcome.get("detail", "safe write refused"))
                if status not in {"updated", "planned"}:
                    actions.append({"action": f"herdr-opencode-{key}", "dest": str(target),
                                    "status": "blocked", "detail": status, "changed": False})
                    continue
            if changed and not dry_run:
                record = {"path": target.name, "key": config_key,
                          "added": added, "created": created}
                if created:
                    record["seeded"] = seeded
                receipts.append(record)
            elif not changed:
                old = previous_files.get(target.name) if isinstance(previous_files, dict) else None
                if isinstance(old, dict):
                    receipts.append(old)
            actions.append({"action": f"herdr-opencode-{key}", "dest": str(target),
                            "status": "planned" if dry_run and changed else status,
                            "changed": changed})
        except (OSError, UnicodeError, ValueError, TypeError, safe_fs.SafetyError) as exc:
            actions.append({"action": f"herdr-opencode-{key}", "dest": str(target),
                            "status": "blocked", "detail": str(exc), "changed": False})
    return {"kind": "herdr-opencode-registration", "actions": actions,
            "managed_config": {"files": receipts}}


def remove_opencode_configs(home: Path, record: dict, *, dry_run: bool = False) -> list[str]:
    """Undo only registrations recorded by activation; keep user edits and siblings."""
    removed = []
    files = record.get("files", []) if isinstance(record, dict) else []
    for item in files if isinstance(files, list) else []:
        if not isinstance(item, dict) or Path(str(item.get("path", ""))).name != item.get("path"):
            continue
        target = Path(home) / item["path"]
        if target.is_symlink() or not target.is_file():
            continue
        try:
            before = target.read_bytes()
            text = before.decode("utf-8")
            seeded = item.get("seeded")
            if item.get("created") and isinstance(seeded, str) and text == seeded:
                if not dry_run:
                    with safe_fs.TargetLock(target):
                        expected = safe_fs.capture_state(target)
                        auth = safe_fs.authority(target, owner="herdr-integration:deactivate",
                                                 allowed_paths=(target,), expected=expected)
                        safe_fs.remove_exact(auth, expected=expected)
                removed.append(str(target))
                continue
            updated = edit_plugin_list(text, key=item.get("key", "plugin"),
                                        remove=tuple(item.get("added", [])))
            after = updated.encode("utf-8")
            if after != before:
                if dry_run:
                    removed.append(str(target))
                elif _write_config(target, before, after, dry_run=False)["status"] == "updated":
                    removed.append(str(target))
        except (OSError, UnicodeError, ValueError, TypeError, safe_fs.SafetyError):
            continue
    return removed


def remove_managed_hook_file(path: Path, record: dict, *, dry_run: bool = False) -> bool:
    """Remove only the exact session registration created by this activation."""
    if not isinstance(record, dict) or record.get("event") != "SessionStart":
        return False
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return False
    try:
        before = path.read_bytes()
        data = json.loads(before.decode("utf-8"))
        hooks = data.get("hooks")
        groups = hooks.get("SessionStart") if isinstance(hooks, dict) else None
        if not isinstance(groups, list):
            return False
        remaining = []
        changed = False
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                remaining.append(group)
                continue
            kept = [hook for hook in group["hooks"]
                    if not (isinstance(hook, dict) and hook.get("command") == record.get("command"))]
            changed |= len(kept) != len(group["hooks"])
            if kept:
                remaining.append(dict(group, hooks=kept))
        if not changed:
            return False
        if remaining:
            hooks["SessionStart"] = remaining
        else:
            hooks.pop("SessionStart", None)
        after = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if dry_run:
            return True
        return _write_config(path, before, after, dry_run=False)["status"] == "updated"
    except (OSError, UnicodeError, ValueError, TypeError, safe_fs.SafetyError):
        return False


def version_marker(path: Path, integration_id: str, version: int) -> bool:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    prefix = r"(?:#|//?)\s*"
    return re.search(rf"(?m)^{prefix}HERDR_INTEGRATION_ID={re.escape(integration_id)}\s*$", text) is not None and re.search(
        rf"(?m)^{prefix}HERDR_INTEGRATION_VERSION={version}\s*$", text) is not None


def manifest_assets(runtime: str, source_root: Path, runtime_home: Path) -> dict:
    """Return installed-file hashes for pinned Herdr assets in the ownership manifest."""
    root, home = Path(source_root), Path(runtime_home)
    mapping = {
        "claude": [("hooks/herdr-agent-state.sh", "hooks/herdr-agent-state.sh")],
        "codex": [("adapters/codex/hooks/herdr-agent-state.sh", "agent-hooks/herdr-agent-state.sh")],
        "opencode": [
            ("adapters/opencode/plugins/herdr-agent-state.js", "plugins/herdr-agent-state.js"),
            ("adapters/opencode/tui/herdr-tui-session.js", "tui/herdr-tui-session.js"),
            ("adapters/opencode/tui/herdr-opencode/tui.js", "tui/herdr-opencode/tui.js"),
        ],
    }
    assets = []
    for source_rel, dest_rel in mapping[runtime]:
        source, dest = root / source_rel, home / dest_rel
        if not source.is_file() or not dest.is_file():
            continue
        try:
            digest = hashlib.sha256(dest.read_bytes()).hexdigest()
        except OSError:
            continue
        assets.append({"path": dest_rel, "sha256": digest})
    return {"version": VERSIONS[runtime], "assets": assets}


def source_checks(runtime: str, source_root: Path) -> list[dict]:
    """Read-only checks for pinned bytes and their actual source registrations."""
    root = Path(source_root)
    paths = {
        "claude": [(root / "hooks/herdr-agent-state.sh", "claude", 11)],
        "codex": [(root / "adapters/codex/hooks/herdr-agent-state.sh", "codex", 10)],
        "opencode": [
            (root / "adapters/opencode/plugins/herdr-agent-state.js", "opencode", 13),
            (root / "adapters/opencode/tui/herdr-tui-session.js", "opencode-tui", 13),
            (root / "adapters/opencode/tui/herdr-opencode/tui.js", "opencode-tui-v2", 13),
        ],
    }
    results = []
    for path, integration_id, version in paths[runtime]:
        results.append({"id": f"{runtime}.herdr.asset.{integration_id}",
                        "ok": version_marker(path, integration_id, version),
                        "detail": f"{path} declares {integration_id} v{version}"})
    if runtime == "claude":
        data = parse_jsonc((root / "adapters/claude/settings.json").read_text(encoding="utf-8"))
        command = CLAUDE_SESSION_COMMAND
        entries = data.get("hooks", {}).get("SessionStart", [])
        registered = any(isinstance(group, dict) and any(
            isinstance(hook, dict) and hook.get("command") == command
            for hook in group.get("hooks", [])) for group in entries)
        owned = [(event, hook.get("command"))
                 for event, groups in data.get("hooks", {}).items() for group in groups
                 if isinstance(group, dict) for hook in group.get("hooks", [])
                 if isinstance(hook, dict) and _owned_state_hook(hook.get("command"))]
        registered = registered and owned == [("SessionStart", command)]
        results.append({"id": "claude.herdr.session-only-registration", "ok": registered,
                        "detail": "SessionStart registers the v11 session hook"})
    elif runtime == "codex":
        data = parse_jsonc((root / "adapters/codex/hooks/hooks.json").read_text(encoding="utf-8"))
        entries = data.get("hooks", {}).get("SessionStart", [])
        registered = any(isinstance(group, dict) and any(
            isinstance(hook, dict) and hook.get("command") == CODEX_SESSION_COMMAND
            for hook in group.get("hooks", [])) for group in entries)
        owned = [(event, hook.get("command"))
                 for event, groups in data.get("hooks", {}).items() for group in groups
                 if isinstance(group, dict) for hook in group.get("hooks", [])
                 if isinstance(hook, dict) and _owned_state_hook(hook.get("command"))]
        registered = registered and owned == [("SessionStart", CODEX_SESSION_COMMAND)]
        results.append({"id": "codex.herdr.session-only-registration", "ok": registered,
                        "detail": "SessionStart registers the v10 session hook"})
    else:
        for name, key, plugins in (("tui.json", "plugin", (OPENCODE_IDENTITY, OPENCODE_TUI_V1)),
                                   ("cli.json", "plugins", (OPENCODE_IDENTITY, OPENCODE_TUI_V2))):
            data = parse_jsonc((root / "adapters/opencode/tui" / name).read_text(encoding="utf-8"))
            ok = all(plugin in data.get(key, []) for plugin in plugins)
            results.append({"id": f"opencode.herdr.registration.{name}", "ok": ok,
                            "detail": f"{name} registers {', '.join(plugins)}"})
    return results
