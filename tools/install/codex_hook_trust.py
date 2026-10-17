"""Best-effort Codex-native trust for Hearting's projected hook definitions."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import time
import tomllib


class TrustError(RuntimeError):
    pass


def _status_client():
    path = Path(__file__).resolve().parents[2] / "adapters/codex/bin/hook-trust-status.py"
    spec = importlib.util.spec_from_file_location("hearting_hook_trust_status", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _trusted_hook_record(runtime_home: Path, hooks_file: Path, previous_record: dict | None,
                         *, command: list[str] | None = None, dry_run: bool = False) -> dict:
    previous = previous_record if isinstance(previous_record, dict) else {}
    prior_hooks = previous.get("hooks") if isinstance(previous.get("hooks"), dict) else {}
    result = {"schema": 1, "hooks": dict(prior_hooks)}
    report = []
    pending = {}
    target = None
    try:
        expected = (runtime_home / "hooks.json")
        if not expected.is_symlink() or not expected.resolve(strict=True).samefile(hooks_file.resolve(strict=True)):
            raise TrustError("managed-hooks-link-unverified")
        config = runtime_home / "config.toml"
        if config.exists():
            target = config.resolve(strict=True) if config.is_symlink() else config
            target_mode = target.stat().st_mode & 0o777
            config_text = target.read_text(encoding="utf-8")
            config_data = tomllib.loads(config_text)
        else:
            target, target_mode, config_text, config_data = config, None, "", {}
        client = _status_client()
        cwd = Path(os.environ.get("TMPDIR") or "/tmp").resolve()
        env = os.environ.copy()
        env["CODEX_HOME"] = str(runtime_home)
        server = client.AppServer(command or shlex.split(os.environ.get(
            "CODEX_HOOK_TRUST_APP_SERVER_COMMAND", "codex app-server --listen stdio://")), cwd, env=env)
        deadline = time.monotonic() + 10.0
        try:
            def request(method, params):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TrustError("app-server-deadline")
                return server.request(method, params, timeout=min(4.0, remaining))
            request("initialize", {"clientInfo": {"name": "hearting-installer", "title": "Hearting installer", "version": "1"}, "capabilities": None})
            server.notify("initialized")
            listed = request("hooks/list", {"cwds": [str(cwd)]}).get("data")
            if not isinstance(listed, list):
                raise TrustError("hooks-list-shape-invalid")
            hooks = [hook for row in listed if isinstance(row, dict) and row.get("cwd") == str(cwd)
                     for hook in (row.get("hooks") or []) if isinstance(hook, dict)
                     and client.same_source(hook.get("sourcePath"), hooks_file)]
            if not hooks:
                raise TrustError("managed-hooks-not-listed")
            state = config_data.get("hooks", {}).get("state", {})
            edits = []
            for hook in hooks:
                key, digest = hook.get("key"), hook.get("currentHash")
                status = hook.get("trustStatus")
                if not isinstance(key, str) or not key or not isinstance(digest, str) or not digest:
                    continue
                if status in {"trusted", "managed"}:
                    continue
                if status not in {"untrusted", "modified"}:
                    continue
                old = prior_hooks.get(key)
                current = state.get(key, {}) if isinstance(state, dict) else {}
                if isinstance(old, dict):
                    baseline = {"present": old.get("baseline_present") is True,
                                "value": old.get("baseline_value")}
                else:
                    baseline = {"present": isinstance(current, dict) and "trusted_hash" in current,
                                "value": current.get("trusted_hash") if isinstance(current, dict) else None}
                if baseline["present"] and not isinstance(baseline["value"], str):
                    continue
                pending[key] = {"source": hook.get("sourcePath"), "written_hash": digest,
                                "baseline_present": baseline["present"], "baseline_value": baseline["value"]}
                edits.append({"keyPath": "hooks.state", "value": {key: {"trusted_hash": digest}},
                              "mergeStrategy": "upsert"})
            if edits and not dry_run:
                request("config/batchWrite", {"edits": edits, "reloadUserConfig": True, "filePath": str(target)})
                if target_mode is not None:
                    os.chmod(target, target_mode)
                checked = tomllib.loads(target.read_text(encoding="utf-8"))
                checked_state = checked.get("hooks", {}).get("state", {})
                for key, receipt in pending.items():
                    current = checked_state.get(key, {}) if isinstance(checked_state, dict) else {}
                    if isinstance(current, dict) and current.get("trusted_hash") == receipt["written_hash"]:
                        result["hooks"][key] = receipt
            elif dry_run:
                report.append("Codex hook trust skipped: dry-run")
            for hook in hooks:
                key = hook.get("key")
                if key in prior_hooks and key not in result["hooks"]:
                    result["hooks"][key] = prior_hooks[key]
        finally:
            server.close()
    except Exception as exc:
        try:
            if target is not None and target.is_file() and pending:
                checked = tomllib.loads(target.read_text(encoding="utf-8"))
                checked_state = checked.get("hooks", {}).get("state", {})
                for key, receipt in pending.items():
                    current = checked_state.get(key, {}) if isinstance(checked_state, dict) else {}
                    if isinstance(current, dict) and current.get("trusted_hash") == receipt["written_hash"]:
                        result["hooks"][key] = receipt
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            pass
        report.append(f"Codex hook trust skipped: {str(exc).splitlines()[0][:180]}")
    return {"record": result, "status": "recorded" if result["hooks"] else "skipped",
            "report_lines": report}


def record_managed_trust(runtime_home, hooks_file, previous_record, *, command=None, dry_run=False):
    return _trusted_hook_record(Path(runtime_home), Path(hooks_file), previous_record,
                                command=command, dry_run=dry_run)


def restore_trust_text(text: str, record: dict | None) -> tuple[str, list[str]]:
    """Restore only Codex's `[hooks.state."key"]` table trusted_hash leaves."""
    hooks = record.get("hooks") if isinstance(record, dict) else None
    if not isinstance(hooks, dict) or not hooks:
        return text, []
    lines = text.splitlines(keepends=True)
    report = []
    changed = False
    for key, receipt in hooks.items():
        if not isinstance(receipt, dict):
            continue
        wanted = "[hooks.state." + json.dumps(key, ensure_ascii=False) + "]"
        starts = [i for i, line in enumerate(lines) if line.strip() == wanted]
        if len(starts) != 1:
            report.append(f"Codex hook trust restore skipped: table not recognized for {key}")
            continue
        start = starts[0]
        end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        matches = [i for i in range(start + 1, end) if lines[i].lstrip().startswith("trusted_hash")]
        if len(matches) != 1:
            report.append(f"Codex hook trust restore skipped: trusted_hash not recognized for {key}")
            continue
        import re
        match = re.fullmatch(r'(\s*trusted_hash\s*=\s*)"((?:[^"\\]|\\.)*)"(\s*(?:#.*)?\n?)', lines[matches[0]])
        if not match:
            report.append(f"Codex hook trust restore skipped: trusted_hash not recognized for {key}")
            continue
        try:
            current_hash = tomllib.loads('trusted_hash = "' + match.group(2) + '"')["trusted_hash"]
        except tomllib.TOMLDecodeError:
            report.append(f"Codex hook trust restore skipped: trusted_hash not recognized for {key}")
            continue
        if current_hash != receipt.get("written_hash"):
            continue
        if receipt.get("baseline_present") is True and isinstance(receipt.get("baseline_value"), str):
            lines[matches[0]] = match.group(1) + json.dumps(receipt["baseline_value"]) + match.group(3)
        else:
            siblings = [lines[i] for i in range(start + 1, end) if i != matches[0]
                        and lines[i].strip() and not lines[i].lstrip().startswith("#")]
            del lines[matches[0]]
            if not siblings:
                del lines[start]
                # Remove exactly the blank separator owned by this deleted
                # table. Additional blank lines and all nonblank user content
                # remain untouched.
                if start < len(lines) and not lines[start].strip():
                    del lines[start]
        changed = True
    if changed and not any(line.strip().startswith("[hooks.state.") for line in lines):
        parent = [i for i, line in enumerate(lines) if line.strip() == "[hooks.state]"]
        if len(parent) == 1:
            index = parent[0]
            next_section = next((i for i in range(index + 1, len(lines))
                                 if lines[i].lstrip().startswith("[")), len(lines))
            if all(not line.strip() for line in lines[index + 1:next_section]):
                del lines[index:next_section]
    output = "".join(lines)
    if changed:
        try:
            tomllib.loads(output)
        except tomllib.TOMLDecodeError:
            return text, ["Codex hook trust restore skipped: TOML validation failed"]
    return output, (["Codex hook trust restore skipped: unsupported table form"] if report else [])
