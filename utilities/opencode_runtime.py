"""Narrow compatibility and read-only account boundary for OpenCode 1.x/2.x.

OpenCode 2 uses a shared service, a different ``run`` CLI, and SQLite-backed
credentials. Keep version-specific mechanics here so callers do not infer V2
support from V1 flags or read the database independently.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
import json
import hashlib
import os
from pathlib import Path
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import time
from types import MappingProxyType
from urllib.parse import quote


VERSION_RE = re.compile(r"(?:^|\D)(\d+)\.(\d+)\.(\d+)(?:\D|$)")
SESSION_ID_RE = re.compile(r"^ses_[A-Za-z0-9]{1,252}$")
MAX_CREDENTIAL_ROWS = 64
MAX_AUTH_BYTES = 64 * 1024
MAX_SESSION_MESSAGES = 64
MAX_MESSAGE_TEXT = 2048
MAX_SHELL_TASK_PAGE = 64
MAX_SHELL_TASK_RECENT = 32
MAX_SHELL_TASK_REFRESH = 64


class OpenCodeRuntimeError(RuntimeError):
    """A bounded runtime or credential read could not be completed safely."""


@dataclass(frozen=True)
class CredentialRead:
    """One credential read, preserving absence separately from read failure."""

    state: str
    credential: object = None
    reason: str | None = None


@dataclass(frozen=True)
class AccountScopeObservation:
    """The selected account scope, or why it could not be observed."""

    state: str
    scope: str | None
    reason: str | None = None


@dataclass(frozen=True)
class GoUsageSelection:
    """Immutable credential and canonical scopes for one Go usage observation."""

    state: str
    integration_id: str | None = None
    credential: object = None
    provider_scope: str | None = None
    opencode_scope: str | None = None
    entitlement_scope: str | None = None
    credential_type: str | None = None
    credential_source: str | None = None
    reason: str | None = None


def parse_version(value: str | None) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = VERSION_RE.search(value)
    return tuple(int(item) for item in match.groups()) if match else None


@lru_cache(maxsize=16)
def runtime_version(binary: str = "opencode") -> tuple[int, int, int] | None:
    """Read the executable version once per process; never start a session."""
    configured = os.environ.get("OPENCODE_RUNTIME_VERSION")
    if configured:
        return parse_version(configured)
    # Test/injected command overrides are often scripts with unrelated
    # ``--version`` semantics. Never execute an arbitrary command merely to
    # identify it as OpenCode; callers can pass a known version through the
    # runtime-owned environment when their executable is not named opencode.
    if Path(binary).name != "opencode":
        return None
    try:
        result = subprocess.run([binary, "--version"], check=False, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=3, env=os.environ.copy())
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_version((result.stdout or "") + "\n" + (result.stderr or ""))


def is_v2(binary: str = "opencode", version: tuple[int, int, int] | None = None,
          environ=None) -> bool:
    if version is not None:
        return version[0] >= 2
    env = os.environ if environ is None else environ
    configured = env.get("OPENCODE_RUNTIME_VERSION")
    if configured:
        version = parse_version(configured)
        return bool(version and version[0] >= 2)
    version = runtime_version(binary)
    # Database generation and executable generation can differ after a
    # downgrade or migration. An unavailable version probe stays unknown and
    # must never turn a retained V2 database into a V2 CLI assumption.
    return bool(version and version[0] >= 2)


# OpenCode V2 permissions are an ordered row stream. Keep its interpretation
# here so config projection, the sealed recorder, and recovery compare the same
# policy. The legacy adapter schema is still a mapping; V2-only action
# interleaving is materialized for the action names this launcher understands.
_OPENCODE_V2_TO_LEGACY_ACTION = {"shell": "bash", "subagent": "task"}
_OPENCODE_LEGACY_TO_V2_ACTION = {"bash": "shell", "task": "subagent",
                                  "write": "edit", "patch": "edit"}
_OPENCODE_BUILTIN_LEGACY_ACTIONS = (
    "read", "edit", "glob", "grep", "bash", "task", "skill", "question",
    "webfetch", "websearch", "external_directory", "doom_loop", "lsp", "execute",
)
_OPENCODE_PERMISSION_EFFECTS = frozenset({"allow", "ask", "deny"})


def _opencode_action_name(value):
    return _OPENCODE_V2_TO_LEGACY_ACTION.get(value, value)


def _opencode_legacy_action_name(value):
    if value in {"write", "patch"}:
        return "edit"
    return _opencode_action_name(value)


def _opencode_pattern_matches(pattern, value):
    """OpenCode's simple wildcard grammar: only `*` and `?` are special."""
    pattern = pattern.replace("\\", "/")
    # Historical action patterns such as ``bash *`` also match the bare tool.
    if pattern.endswith(" *"):
        if value == pattern[:-2]:
            return True
    expression = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return re.fullmatch(expression, value, re.DOTALL) is not None


@lru_cache(maxsize=1024)
def _opencode_patterns_overlap(left, right):
    """Whether two simple `*`/`?` patterns can match at least one same string."""
    left, right = left.replace("\\", "/"), right.replace("\\", "/")
    # Keep the legacy command-pattern shorthand's bare-tool match in the
    # precedence relation as well as in the action evaluator.
    if left.endswith(" *") and _opencode_patterns_overlap(left[:-2], right):
        return True
    if right.endswith(" *") and _opencode_patterns_overlap(left, right[:-2]):
        return True

    def closure(pattern, states):
        result = set(states)
        pending = list(states)
        while pending:
            index = pending.pop()
            if index < len(pattern) and pattern[index] == "*" and index + 1 not in result:
                result.add(index + 1)
                pending.append(index + 1)
        return frozenset(result)

    def move(pattern, states, char):
        result = set()
        for index in states:
            if index >= len(pattern):
                continue
            token = pattern[index]
            if token == "*":
                result.add(index)
            elif token == "?" or char is not None and token == char:
                result.add(index + 1)
        return closure(pattern, result)

    alphabet = set(left + right) - {"*", "?"}
    alphabet.add(None)  # a character other than either pattern's literals
    start = (closure(left, {0}), closure(right, {0}))
    pending, seen = [start], {start}
    while pending:
        left_states, right_states = pending.pop()
        if len(left) in left_states and len(right) in right_states:
            return True
        for char in alphabet:
            next_state = (move(left, left_states, char), move(right, right_states, char))
            if next_state not in seen:
                seen.add(next_state)
                pending.append(next_state)
    return False


def normalize_opencode_permission(permission, *, field="permission"):
    """Return a validated legacy action/resource mapping without changing order.

    A top-level or per-action scalar is the legacy spelling of a `*` rule.
    Missing/null remains empty so callers can distinguish it from recorded
    nonempty authority.
    """
    if permission is None:
        return {}
    if isinstance(permission, str):
        if permission not in _OPENCODE_PERMISSION_EFFECTS:
            raise ValueError(f"OpenCode {field} has invalid effect {permission!r}")
        return {"*": permission}
    if not isinstance(permission, Mapping):
        raise ValueError(f"OpenCode {field} must be a string or object")
    normalized = {}
    for action, setting in permission.items():
        if not isinstance(action, str) or not action:
            raise ValueError(f"OpenCode {field} action must be a nonempty string")
        if isinstance(setting, str):
            if setting not in _OPENCODE_PERMISSION_EFFECTS:
                raise ValueError(f"OpenCode {field}.{action} has invalid effect {setting!r}")
            normalized[action] = setting
            continue
        if not isinstance(setting, Mapping):
            raise ValueError(f"OpenCode {field}.{action} must be a string or object")
        resources = {}
        for resource, effect in setting.items():
            if not isinstance(resource, str) or not isinstance(effect, str) \
                    or effect not in _OPENCODE_PERMISSION_EFFECTS:
                raise ValueError(f"OpenCode {field}.{action} contains an invalid resource rule")
            resources[resource] = effect
        normalized[action] = resources
    return normalized


def _opencode_append_ordered_rule(rules, resource, effect):
    if resource in rules:
        del rules[resource]
    rules[resource] = effect


def opencode_permission_rules_for_action(permission, action, default=None):
    """Materialize one legacy action's effective ordered resource rules."""
    normalized = normalize_opencode_permission(permission)
    value = None if default is None else {"*": default}
    action = _opencode_legacy_action_name(action)
    for pattern, setting in normalized.items():
        canonical_pattern = _opencode_legacy_action_name(pattern)
        if not _opencode_pattern_matches(canonical_pattern, action):
            continue
        if isinstance(setting, str):
            value = {"*": setting}
        else:
            if value is None:
                value = {}
            for resource, effect in setting.items():
                _opencode_append_ordered_rule(value, resource, effect)
    return None if value is None else list(value.items())


def opencode_permission_effect(permission, action, resource, default=None):
    """Return the final effect for one action/resource pair."""
    effect = default
    for pattern, candidate in opencode_permission_rules_for_action(
            permission, action) or ():
        if _opencode_pattern_matches(pattern, resource):
            effect = candidate
    return effect


@lru_cache(maxsize=1024)
def _opencode_pattern_covers(cover, pattern):
    """Prove containment for launcher patterns; keep unproved cases distinct."""
    cover = re.sub(r"\*+", "*", cover.replace("\\", "/"))
    pattern = re.sub(r"\*+", "*", pattern.replace("\\", "/"))
    if cover == "*" or cover == pattern:
        return True
    if "*" not in pattern and "?" not in pattern:
        return _opencode_pattern_matches(cover, pattern)
    # A literal directory/command prefix followed by '*' covers any narrower
    # pattern starting with that prefix. Other glob containments stay unknown.
    prefix = cover[:-1]
    return (cover.endswith("*") and "*" not in prefix and "?" not in prefix
            and pattern.startswith(prefix))


def opencode_permission_ordered_view(permission):
    """Compare live grants, preserving precedence only for conflicting effects."""
    normalized = normalize_opencode_permission(permission)
    rows = []
    for action, setting in normalized.items():
        action = _opencode_legacy_action_name(action)
        resources = {"*": setting} if isinstance(setting, str) else setting
        for resource, effect in resources.items():
            key = (action, resource)
            rows = [entry for entry in rows if entry[:2] != key]
            rows.append((action, resource, effect))
    if not rows:
        return []

    def covers(before, after):
        return (_opencode_pattern_covers(before[0], after[0])
                and _opencode_pattern_covers(before[1], after[1]))

    def overlaps(before, after):
        return (_opencode_patterns_overlap(before[0], after[0])
                and _opencode_patterns_overlap(before[1], after[1]))

    live = []
    for index, row in enumerate(rows):
        if any(covers(later, row) for later in rows[index + 1:]):
            continue  # A later rule decides every point this row could decide.
        for earlier_index in range(len(live) - 1, -1, -1):
            earlier = live[earlier_index]
            if (earlier[2] == row[2] and covers(earlier, row)
                    and not any(between[2] != row[2] and overlaps(between, row)
                                for between in live[earlier_index + 1:])):
                break  # The row repeats an unchanged earlier decision.
        else:
            live.append(row)
    rows = live
    precedence = []
    for index, before in enumerate(rows):
        for after in rows[index + 1:]:
            if before[2] != after[2] and overlaps(before, after):
                precedence.append((before, after))
    return {
        "rules": [list(row) for row in sorted(rows)],
        "precedence": [[list(before), list(after)]
                       for before, after in sorted(precedence)],
    }


def _opencode_validate_v2_rows(rows, *, field="permissions"):
    if rows is None:
        raise ValueError(f"OpenCode {field} must be an ordered array")
    if not isinstance(rows, list):
        raise ValueError(f"OpenCode {field} must be an ordered array")
    result = []
    for index, row in enumerate(rows):
        if (not isinstance(row, dict) or set(row) != {"action", "resource", "effect"}
                or not all(isinstance(row.get(key), str) and row[key]
                           for key in ("action", "resource", "effect"))
                or row["effect"] not in _OPENCODE_PERMISSION_EFFECTS):
            raise ValueError(f"OpenCode {field}[{index}] is not a supported permission rule")
        result.append({key: row[key] for key in ("action", "resource", "effect")})
    return result


def _opencode_rules_from_v2(rows, action):
    result = {}
    for row in rows:
        if _opencode_pattern_matches(row["action"], action):
            _opencode_append_ordered_rule(result, row["resource"], row["effect"])
    return list(result.items())


def _opencode_merge_rule_lists(*groups):
    result = {}
    for group in groups:
        for resource, effect in group or ():
            _opencode_append_ordered_rule(result, resource, effect)
    return list(result.items())


def _opencode_set_action_rules(permission, action, rules):
    if action in permission:
        del permission[action]
    permission[action] = dict(rules)


def _opencode_v2_policy_to_legacy(rows, base, *, field):
    rows = _opencode_validate_v2_rows(rows, field=field)
    for row in rows:
        for legacy_action, native_action in (("bash", "shell"), ("task", "subagent")):
            if (_opencode_pattern_matches(row["action"], legacy_action)
                    and not _opencode_pattern_matches(row["action"], native_action)):
                raise ValueError(
                    f"OpenCode {field} action pattern {row['action']!r} conflicts with "
                    f"the V1 {legacy_action!r} alias")
    permission = normalize_opencode_permission(base, field="permission")
    base_permission = deepcopy(permission)
    for row in rows:
        action = _opencode_action_name(row["action"])
        # These V1 aliases are independent V2 action names, so combining them
        # into one legacy slot would discard a native permission boundary.
        if action in {"bash", "task", "write", "patch"} and row["action"] == action:
            raise ValueError(f"OpenCode {field} uses unsupported V2 action alias {action!r}")
        current = permission.get(action)
        if isinstance(current, str):
            current = {"*": current}
        elif not isinstance(current, dict):
            current = {}
        else:
            current = dict(current)
        _opencode_append_ordered_rule(current, row["resource"], row["effect"])
        permission[action] = current

    targets = list(_OPENCODE_BUILTIN_LEGACY_ACTIONS)
    for row in rows:
        if not any(char in row["action"] for char in "*?"):
            action = _opencode_action_name(row["action"])
            if action not in targets:
                targets.append(action)
    for action in targets:
        native_action = _OPENCODE_LEGACY_TO_V2_ACTION.get(action, action)
        base_rules = opencode_permission_rules_for_action(base_permission, action)
        actual = _opencode_merge_rule_lists(base_rules, _opencode_rules_from_v2(rows, native_action))
        projected = opencode_permission_rules_for_action(permission, action)
        if actual != (projected or []):
            _opencode_set_action_rules(permission, action, actual)
    return permission


def opencode_v2_to_legacy_config(config):
    """Project V2 permission arrays into the legacy shape used by shared overlays.

    Raw rows remain the adapter's serialization source. The exact action entries
    added here repair action-pattern interleaving for launcher-supported tools.
    """
    if not isinstance(config, Mapping):
        raise ValueError("OpenCode config must be an object")
    value = deepcopy(dict(config))
    rows = value.pop("permissions", [])
    value["permission"] = _opencode_v2_policy_to_legacy(
        rows, value.get("permission", {}), field="permissions")
    v2_agents = value.pop("agents", {})
    v1_agents = value.get("agent", {})
    if not isinstance(v2_agents, dict) or not isinstance(v1_agents, dict):
        raise ValueError("OpenCode agent config must be an object")
    agents = {**v2_agents, **v1_agents}
    legacy_agents = {}
    for name, definition in agents.items():
        if not isinstance(definition, dict):
            legacy_agents[name] = definition
            continue
        item = deepcopy(definition)
        agent_rows = item.pop("permissions", [])
        item["permission"] = _opencode_v2_policy_to_legacy(
            agent_rows, item.get("permission", {}), field=f"agents.{name}.permissions")
        legacy_agents[name] = item
    if legacy_agents or "agent" in value or "agents" in config:
        value["agent"] = legacy_agents
    return value


def _opencode_encode_legacy_rows(permission):
    normalized = normalize_opencode_permission(permission)
    rows = []
    for action, setting in normalized.items():
        native_action = _OPENCODE_LEGACY_TO_V2_ACTION.get(action, action)
        resources = {"*": setting} if isinstance(setting, str) else setting
        rows.extend({"action": native_action, "resource": resource, "effect": effect}
                    for resource, effect in resources.items())
    return rows


def _opencode_encode_v2_policy(permission, original_rows, base_permission=None, *, field):
    original_rows = _opencode_validate_v2_rows(original_rows, field=field)
    original_base = normalize_opencode_permission(base_permission, field="permission")
    desired = normalize_opencode_permission(permission, field="permission")
    targets = list(_OPENCODE_BUILTIN_LEGACY_ACTIONS)
    for action in desired:
        canonical = _opencode_legacy_action_name(action)
        if not any(char in canonical for char in "*?") and canonical not in targets:
            targets.append(canonical)
    result = deepcopy(original_rows)
    for action in targets:
        native_action = _OPENCODE_LEGACY_TO_V2_ACTION.get(action, action)
        wanted = opencode_permission_rules_for_action(desired, action) or []
        existing = _opencode_merge_rule_lists(
            opencode_permission_rules_for_action(original_base, action),
            _opencode_rules_from_v2(original_rows, native_action))
        if wanted == existing:
            continue
        if not wanted and existing:
            raise ValueError(f"OpenCode {field} cannot remove an existing {action!r} rule exactly")
        result.extend({"action": native_action, "resource": resource, "effect": effect}
                      for resource, effect in wanted)
    return result


def opencode_legacy_to_v2_config(config, *, original_v2=None):
    """Encode a modified legacy projection as native V2 config.

    When `original_v2` is supplied, preserve its ordered rows and append exact
    action overrides only where the projected legacy policy changed.
    """
    if not isinstance(config, Mapping):
        raise ValueError("OpenCode config must be an object")
    value = deepcopy(dict(config))
    permission = value.pop("permission", {})
    if original_v2 is None:
        value["permissions"] = _opencode_encode_legacy_rows(permission)
    else:
        source = original_v2 if isinstance(original_v2, dict) else {}
        if "permissions" in source:
            value["permissions"] = _opencode_encode_v2_policy(
                permission, source["permissions"], source.get("permission", {}), field="permissions")
        else:
            value["permissions"] = _opencode_encode_legacy_rows(permission)

    agents = value.pop("agent", {})
    if not isinstance(agents, dict):
        raise ValueError("OpenCode agent config must be an object")
    source_agents = original_v2.get("agents", {}) if isinstance(original_v2, dict) else {}
    if not isinstance(source_agents, dict):
        raise ValueError("OpenCode agents config must be an object")
    converted = {}
    for name, definition in agents.items():
        if not isinstance(definition, dict):
            converted[name] = definition
            continue
        item = deepcopy(definition)
        local_permission = item.pop("permission", {})
        if original_v2 is None:
            item["permissions"] = _opencode_encode_legacy_rows(local_permission)
        else:
            source_definition = source_agents.get(name, {})
            if not isinstance(source_definition, dict):
                source_definition = {}
            if "permissions" in source_definition:
                item["permissions"] = _opencode_encode_v2_policy(
                    local_permission, source_definition["permissions"],
                    source_definition.get("permission", {}),
                    field=f"agents.{name}.permissions")
            else:
                item["permissions"] = _opencode_encode_legacy_rows(local_permission)
        converted[name] = item
    if converted or (isinstance(original_v2, dict) and "agents" in original_v2):
        value["agents"] = converted
    return value


def opencode_permission_from_config(config):
    """Read the final global legacy permission from a V1 or V2 config."""
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except json.JSONDecodeError as exc:
            raise ValueError(f"OpenCode config is not valid JSON: {exc}") from exc
    if not isinstance(config, Mapping):
        raise ValueError("OpenCode config must be an object")
    if "permissions" in config or "agents" in config:
        projected = opencode_v2_to_legacy_config(config)
        if "permissions" in config:
            return projected["permission"]
    return normalize_opencode_permission(config.get("permission")) if "permission" in config else None


def model_reference(model: str | None, variant: str | None) -> str | None:
    """Encode V2's provider/model#variant without duplicating a suffix."""
    if not model:
        return None
    if variant in (None, "", "runtime-default"):
        return model
    if "#" in model:
        current, _separator, _existing = model.partition("#")
        return current + "#" + variant
    return model + "#" + variant


def run_argv(*, binary: str = "opencode", cwd: str | None = None,
             agent: str | None = None, model: str | None = None,
             variant: str | None = None, session: str | None = None,
             title: str | None = None, pure: bool = False,
             standalone: bool = False,
             version: tuple[int, int, int] | None = None) -> list[str]:
    """Build a run command while preserving the V1 spelling and V2 cwd contract.

    The caller supplies ``cwd`` to ``Popen``/the shell. V2 removed ``--dir`` and
    ``--variant``; a V2 model variant is part of ``provider/model#variant``.
    ``standalone`` is opt-in and has no effect on V1 or existing callers.
    """
    modern = is_v2(binary, version=version)
    result = [binary, "run"]
    if modern and standalone:
        result.append("--standalone")
    if not modern and pure:
        result.append("--pure")
    if not modern and cwd:
        result += ["--dir", cwd]
    result += ["--format", "json"]
    if agent:
        result += ["--agent", agent]
    if session:
        result += ["--session", session]
    if model:
        if modern:
            result += ["--model", model_reference(model, variant) or model]
        else:
            result += ["--model", model]
            if variant and variant != "runtime-default":
                result += ["--variant", variant]
    if title and modern:
        result += ["--title", title]
    return result


def opencode_data_dir(environ=None) -> Path:
    env = os.environ if environ is None else environ
    home = Path(env.get("HOME") or Path.home())
    data_home = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
    return Path(env.get("OPENCODE_DATA_DIR") or env.get("OPENCODE_DATA_HOME")
                or data_home / "opencode").expanduser()


def opencode_db_path(environ=None) -> Path:
    env = os.environ if environ is None else environ
    override = str(env.get("OPENCODE_DB") or "").strip()
    data_dir = opencode_data_dir(env)
    if override:
        path = Path(override).expanduser()
        return path if path.is_absolute() else data_dir / path
    return data_dir / "opencode.db"


def _decode_credential(raw) -> dict | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None
    return None


def credential_store_present(*, db_path: str | Path | None = None, environ=None) -> bool:
    """Check the credential-store schema without using session tables or secrets."""
    path = Path(db_path) if db_path is not None else opencode_db_path(environ)
    if not path.is_file():
        return False
    uri = "file:" + quote(str(path.resolve()), safe="/:\\") + "?mode=ro"
    con = None
    try:
        con = sqlite3.connect(uri, uri=True, timeout=0.5)
        con.execute("PRAGMA query_only=ON")
        con.execute("PRAGMA busy_timeout=500")
        return con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='credential' LIMIT 1"
        ).fetchone() is not None
    except (OSError, sqlite3.Error):
        return False
    finally:
        if con is not None:
            con.close()


def _scope_for_row(integration_id: str, row: dict | None) -> str:
    if not row:
        return "unscoped"
    identity = row.get("credential_id") or row.get("label")
    if not identity:
        value = row.get("value") or {}
        identity = value.get("key") or value.get("access") or "active"
    digest = hashlib.sha256((integration_id + "\0" + str(identity)).encode()).hexdigest()[:20]
    return "account:" + digest


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _legacy_auth_path(environ):
    home = Path(environ.get("HOME") or Path.home())
    data = Path(environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    return Path(environ.get("OPENCODE_AUTH") or data / "opencode" / "auth.json")


def _legacy_account(integration_id: str, environ) -> dict | None:
    return read_legacy_credential(integration_id, auth_path=_legacy_auth_path(environ))


def account_scope_observation(integration_id: str, *, environ=None) -> AccountScopeObservation:
    """Resolve one account scope without converting a failed read into absence."""
    env = os.environ if environ is None else environ
    read = (read_credential_observation(integration_id, environ=env) if is_v2(environ=env)
            else read_legacy_credential_observation(
                integration_id, auth_path=_legacy_auth_path(env)))
    if read.state == "unavailable":
        return AccountScopeObservation("unavailable", None, read.reason)
    if read.state == "absent":
        return AccountScopeObservation("absent", "unscoped")
    return AccountScopeObservation("present", _scope_for_row(integration_id, read.credential))


def account_scope(integration_id: str, *, environ=None) -> str:
    """Stable opaque scope for the runtime's selected account; never a secret."""
    observation = account_scope_observation(integration_id, environ=environ)
    return observation.scope or "unscoped"


def _go_selection_from_row(integration_id: str, credential, opencode_scope: str):
    value = credential.get("value") if isinstance(credential, Mapping) else None
    credential_type = value.get("type") if isinstance(value, Mapping) else None
    provider_scope = _scope_for_row(integration_id, credential)
    oauth_selected = integration_id == "opencode" and credential_type == "oauth"
    return GoUsageSelection(
        "present", integration_id=integration_id, credential=credential,
        provider_scope=provider_scope, opencode_scope=opencode_scope,
        entitlement_scope=provider_scope if oauth_selected else None,
        credential_type=credential_type,
        credential_source=credential.get("source") if isinstance(credential, Mapping) else None,
    )


def _unavailable_go_selection(reason="credential-store-unavailable"):
    return GoUsageSelection("unavailable", reason=reason)


def selected_go_usage_observation(*, environ=None) -> GoUsageSelection:
    """Select one Go credential and derive every account scope from that read.

    A read failure is not evidence that OAuth is absent. The frozen selection is
    passed through the usage request and cache write so an account switch during
    the request cannot retag its result.
    """
    env = os.environ if environ is None else environ
    if not is_v2(environ=env):
        legacy = read_legacy_credential_observation(
            "opencode-go", auth_path=_legacy_auth_path(env))
        if legacy.state == "unavailable":
            return _unavailable_go_selection(legacy.reason)
        if legacy.state == "absent":
            return GoUsageSelection("absent", provider_scope="unscoped")
        return _go_selection_from_row("opencode-go", legacy.credential, "unscoped")

    oauth_read = read_credential_observation("opencode", environ=env)
    if oauth_read.state == "unavailable":
        return _unavailable_go_selection(oauth_read.reason)
    oauth = oauth_read.credential if oauth_read.state == "present" else None
    opencode_scope = (_scope_for_row("opencode", oauth) if oauth is not None
                      else "unscoped")
    if oauth is not None:
        value = oauth.get("value") if isinstance(oauth, Mapping) else None
        valid_non_oauth_key = (
            isinstance(value, Mapping)
            and value.get("type") in {"key", "api"}
            and isinstance(value.get("key"), str)
            and bool(value.get("key"))
        )
        if not valid_non_oauth_key:
            return _go_selection_from_row("opencode", oauth, opencode_scope)

    api_read = read_credential_observation("opencode-go", environ=env)
    if api_read.state == "unavailable":
        return _unavailable_go_selection(api_read.reason)
    if api_read.state == "present":
        return _go_selection_from_row("opencode-go", api_read.credential, opencode_scope)

    legacy = read_legacy_credential_observation(
        "opencode-go", auth_path=_legacy_auth_path(env))
    if legacy.state == "unavailable":
        return _unavailable_go_selection(legacy.reason)
    if legacy.state == "present":
        return _go_selection_from_row("opencode-go", legacy.credential, opencode_scope)
    return GoUsageSelection("absent", provider_scope="unscoped",
                            opencode_scope=opencode_scope)


def selected_go_usage_credential(*, environ=None) -> dict | None:
    """Compatible dictionary boundary for consumers that only need a token row."""
    selection = selected_go_usage_observation(environ=environ)
    if selection.state != "present":
        return None
    return {"integration_id": selection.integration_id,
            "credential": _thaw(selection.credential)}


def provider_account_observation(provider: str, *, environ=None) -> AccountScopeObservation:
    """Resolve the account scope used by a provider without hiding read failure."""
    env = os.environ if environ is None else environ
    if provider == "opencode-go" and is_v2(environ=env):
        selection = selected_go_usage_observation(environ=env)
        if selection.state == "unavailable":
            return AccountScopeObservation("unavailable", None, selection.reason)
        return AccountScopeObservation(selection.state,
                                      selection.provider_scope or "unscoped",
                                      selection.reason)
    return account_scope_observation(provider, environ=env)


def provider_account_scope(provider: str, *, environ=None) -> str:
    """Resolve Go models against the account OpenCode actually uses for Go."""
    observation = provider_account_observation(provider, environ=environ)
    return observation.scope or "unscoped"


def model_availability(model: str, *, environ=None) -> dict:
    """Read provider/account/model evidence from Fleet's existing cache."""
    if not isinstance(model, str) or "/" not in model:
        return {"state": "unknown", "reason": "model-scope-unavailable"}
    provider, model_id = model.split("/", 1)
    model_id = model_id.split("#", 1)[0]
    if provider == "opencode-go" and is_v2(environ=environ):
        selection = selected_go_usage_observation(environ=environ)
        if selection.state == "unavailable":
            return {"state": "unknown", "reason": selection.reason or "account-scope-unavailable"}
        # The provider observation and entitlement fallback must refer to one
        # selected row. Do not look up the current OpenCode account a second time.
        scope = selection.provider_scope or "unscoped"
        entitlement_scope = selection.entitlement_scope
    else:
        account = provider_account_observation(provider, environ=environ)
        if account.state == "unavailable" or account.scope is None:
            return {"state": "unknown", "reason": account.reason or "account-scope-unavailable"}
        scope = account.scope
        entitlement_scope = None
    tools = Path(__file__).resolve().parents[1] / "tools"
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    try:
        from fleet.collectors import usage_cache
        result = usage_cache.read_provider("opencode", provider, model_id, scope)
        if result.get("state") == "unavailable":
            return result
        general = usage_cache.read_provider("opencode", provider, "*", scope)
        if general.get("state") == "unavailable":
            return general
        if result.get("state") == "unknown":
            result = general
        if (provider == "opencode-go" and result.get("state") == "unknown"
                and entitlement_scope is not None):
            result = usage_cache.read_provider("opencode", "opencode-go-entitlement", "*",
                                               entitlement_scope)
        return result
    except Exception:
        return {"state": "unknown", "reason": "availability-cache-unavailable"}


def record_model_outcome(model: str | None, *, success=False, error=None, source="opencode-run",
                         reason_override=None):
    """Persist only confirmed account/model outcomes; transient failures do not clear them."""
    if not isinstance(model, str) or "/" not in model:
        return False
    provider, model_id = model.split("/", 1)
    model_id = model_id.split("#", 1)[0]
    account = provider_account_observation(provider)
    if account.state == "unavailable" or account.scope is None:
        return False
    scope = account.scope
    state, reason = None, None
    if success:
        state = "usable"
    elif isinstance(error, dict):
        status = error.get("status")
        code = str(error.get("code") or error.get("type") or "")
        message = str(error.get("message") or "")
        classification = classify_usage_rejection(
            status if isinstance(status, int) else 0,
            {"error": {"type": code, "message": message}},
        )
        if classification:
            state, reason = "unavailable", classification["reason"]
        elif isinstance(status, int) and status == 401:
            state, reason = "unavailable", "authentication-rejected"
        elif isinstance(status, int) and status == 429:
            state, reason = "unavailable", "rate-limited"
    if reason_override and not success:
        state, reason = "unavailable", str(reason_override)
    if state is None:
        return False
    tools = Path(__file__).resolve().parents[1] / "tools"
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    try:
        from fleet.collectors import usage_cache
        exact = usage_cache.record_provider("opencode", provider, model_id, scope,
                                            state, reason=reason, source=source)
        # A same-account success proves account authentication recovered. Keep
        # model-specific denials intact while allowing this provider scope to
        # recover from an older account-level rejection.
        general = usage_cache.record_provider("opencode", provider, "*", scope,
                                              state, reason=reason, source=source)
        return exact or general
    except Exception:
        return False


def automatic_model_fallbacks(config_path: str | Path | None = None) -> list[tuple[str, str]]:
    """Ordered, source-controlled fallbacks; no user setting or new policy flag."""
    path = Path(config_path) if config_path else (
        Path(__file__).resolve().parents[1] / "adapters" / "opencode" / "config" / "models.conf")
    try:
        from model_config import parse_config
        config = parse_config(path)
    except Exception:
        return []
    result = []
    for item in config.get("CFG_V2_AUTO_FALLBACKS", "").split():
        model, sep, variant = item.partition(":")
        if sep and model and variant and "/" in model:
            result.append((model, variant))
    return result


def select_automatic_model(model: str, variant: str, *, config_path=None,
                           environ=None) -> tuple[str, str, dict]:
    """Keep a usable/unknown selection; replace only a confirmed unavailable one."""
    status = model_availability(model, environ=environ)
    if status.get("state") != "unavailable":
        return model, variant, status
    for candidate, effort in automatic_model_fallbacks(config_path):
        if candidate == model:
            continue
        state = model_availability(candidate, environ=environ)
        if state.get("state") != "unavailable":
            return candidate, effort, state
    return model, variant, status


def automatic_candidates(model: str, variant: str, *, config_path=None,
                         environ=None) -> list[dict]:
    """Candidate evidence for a hard harness-level availability decision."""
    rows = []
    candidates = [(model, variant), *automatic_model_fallbacks(config_path)]
    seen = set()
    for candidate, effort in candidates:
        key = (candidate, effort)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"model": candidate, "variant": effort,
                     **model_availability(candidate, environ=environ)})
    return rows


def _selected_row(rows):
    """Mirror V2 active-account selection, including pre-active NULL rows.

    An explicit active row wins. When a migrated database has only NULL active
    values, the newest NULL row is the legacy selection. Explicitly inactive
    rows are never promoted by this reader.
    """
    if any(row["active"] not in (None, 0, 1) for row in rows):
        raise OpenCodeRuntimeError("opencode-credential-active-invalid")
    active = [row for row in rows if row["active"] == 1]
    if len(active) == 1:
        return active[0]
    if len(active) > 1:
        raise OpenCodeRuntimeError("opencode-credential-active-ambiguous")
    if any(row["active"] is not None for row in rows):
        return None
    legacy = [row for row in rows if row["active"] is None]
    if not legacy:
        return None
    legacy.sort(key=lambda row: (row.get("time_created") or 0,
                                 row.get("time_updated") or 0, row.get("id") or ""))
    return legacy[-1]


def read_credential_observation(integration_id: str, *, db_path: str | Path | None = None,
                                environ=None) -> CredentialRead:
    """Read one native selected credential without collapsing errors into absence."""
    if not isinstance(integration_id, str) or not integration_id or len(integration_id) > 128:
        return CredentialRead("unavailable", reason="credential-id-invalid")
    path = Path(db_path) if db_path is not None else opencode_db_path(environ)
    try:
        path_stat = path.stat()
    except FileNotFoundError:
        try:
            path.lstat()
        except FileNotFoundError:
            return CredentialRead("absent")
        except OSError:
            return CredentialRead("unavailable", reason="credential-store-read-failed")
        return CredentialRead("unavailable", reason="credential-store-target-missing")
    except OSError:
        return CredentialRead("unavailable", reason="credential-store-read-failed")
    if not stat.S_ISREG(path_stat.st_mode):
        return CredentialRead("unavailable", reason="credential-store-not-file")
    con = None
    try:
        uri = "file:" + quote(str(path.resolve()), safe="/:\\") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=0.5)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        con.execute("PRAGMA busy_timeout=500")
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='credential'")}
        if "credential" not in tables:
            return CredentialRead("unavailable", reason="credential-store-schema-unavailable")
        columns = {row[1] for row in con.execute("PRAGMA table_info(credential)")}
        scope_col = "integration_id" if "integration_id" in columns else (
            "connector_id" if "connector_id" in columns else None)
        if scope_col is None or "value" not in columns:
            return CredentialRead("unavailable", reason="credential-store-schema-unavailable")
        selected = [name for name in ("id", "label", "value", "active", "time_created", "time_updated")
                    if name in columns]
        order = ", ".join(name + " DESC" for name in ("active", "time_created", "time_updated", "id")
                           if name in columns)
        rows = con.execute(
            "SELECT " + ", ".join('"' + name + '"' for name in selected)
            + " FROM credential WHERE \"" + scope_col + "\"=?"
            + (" ORDER BY " + order if order else "") + " LIMIT ?",
            (integration_id, MAX_CREDENTIAL_ROWS + 1),
        ).fetchall()
        if len(rows) > MAX_CREDENTIAL_ROWS:
            return CredentialRead("unavailable", reason="credential-store-selection-overflow")
        normalized = []
        for row in rows:
            item = dict(row)
            item.setdefault("active", None)
            item["value"] = _decode_credential(item.get("value"))
            normalized.append(item)
        chosen = _selected_row(normalized)
        if not chosen:
            return CredentialRead("absent")
        # No secret is returned in diagnostic metadata; the caller needs it
        # only for an immediate request or local eligibility classification.
        # Keep a selected malformed row present with value=None so callers do
        # not mistake it for absent OAuth and fall through to a stale key.
        credential = {"integration_id": integration_id, "credential_id": chosen.get("id"),
                      "label": chosen.get("label"), "value": chosen["value"],
                      "source": "opencode-sqlite",
                      "credential_valid": bool(chosen["value"])}
        return CredentialRead("present", _freeze(credential))
    except OpenCodeRuntimeError as error:
        reason = ("credential-selection-ambiguous" if "ambiguous" in str(error)
                  else "credential-selection-invalid")
        return CredentialRead("unavailable", reason=reason)
    except (OSError, sqlite3.Error):
        return CredentialRead("unavailable", reason="credential-store-read-failed")
    finally:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass


def read_credential(integration_id: str, *, db_path: str | Path | None = None,
                    environ=None) -> dict | None:
    """Compatible selected-credential reader; use the observation API for status."""
    read = read_credential_observation(integration_id, db_path=db_path, environ=environ)
    return _thaw(read.credential) if read.state == "present" else None


def read_legacy_credential_observation(integration_id: str, *, auth_path: str | Path,
                                       environ=None) -> CredentialRead:
    """Read one legacy auth entry while retaining missing-file/read-failure status."""
    path = Path(auth_path).expanduser()
    try:
        path_stat = path.stat()
    except FileNotFoundError:
        try:
            path.lstat()
        except FileNotFoundError:
            return CredentialRead("absent")
        except OSError:
            return CredentialRead("unavailable", reason="legacy-credential-read-failed")
        return CredentialRead("unavailable", reason="legacy-credential-target-missing")
    except OSError:
        return CredentialRead("unavailable", reason="legacy-credential-read-failed")
    if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_size > MAX_AUTH_BYTES:
        return CredentialRead("unavailable", reason="legacy-credential-store-unavailable")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return CredentialRead("unavailable", reason="legacy-credential-read-failed")
    if not isinstance(value, dict):
        return CredentialRead("unavailable", reason="legacy-credential-store-malformed")
    if integration_id not in value:
        return CredentialRead("absent")
    entry = value.get(integration_id)
    if not isinstance(entry, dict):
        return CredentialRead("unavailable", reason="legacy-credential-entry-malformed")
    credential = {"integration_id": integration_id, "label": None, "value": entry,
                  "source": "legacy-auth-json"}
    return CredentialRead("present", _freeze(credential))


def read_legacy_credential(integration_id: str, *, auth_path: str | Path) -> dict | None:
    """Compatible legacy reader; use the observation API for status."""
    read = read_legacy_credential_observation(integration_id, auth_path=auth_path)
    return _thaw(read.credential) if read.state == "present" else None


def classify_usage_rejection(status: int, body: bytes | str | dict) -> dict | None:
    """Normalize only provider errors whose code and scope are explicit."""
    if isinstance(body, bytes):
        body = body[:64 * 1024].decode("utf-8", errors="replace")
    if isinstance(body, str):
        try:
            decoded = json.loads(body)
        except ValueError:
            decoded = {"message": body}
    else:
        decoded = body
    if not isinstance(decoded, dict):
        decoded = {}
    nested = decoded.get("error")
    error = nested if isinstance(nested, dict) else decoded
    code = str(error.get("code") or error.get("type") or error.get("name") or "")
    message = str(error.get("message") or "")
    normalized = (code + " " + message).lower()
    if status == 401 and "api_key_expired" in normalized:
        return {"state": "unavailable", "reason": "credential-expired", "http_status": status}
    if status == 401:
        return {"state": "unavailable", "reason": "authentication-rejected", "http_status": status}
    if status == 403 and "entitlementerror" in normalized and "go subscription required" in normalized:
        return {"state": "unavailable", "reason": "subscription-missing", "http_status": status}
    return None


def normalize_json_event(row: object) -> dict | None:
    """Normalize one newline-delimited CLI event without assigning completion."""
    if isinstance(row, bytes):
        try:
            row = row.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(row, str):
        try:
            row = json.loads(row)
        except ValueError:
            return None
    if not isinstance(row, dict) or not isinstance(row.get("type"), str):
        return None
    sid = row.get("sessionID")
    if sid and (not isinstance(sid, str) or not SESSION_ID_RE.fullmatch(sid)):
        return None
    kind = row["type"]
    part = row.get("part") if isinstance(row.get("part"), dict) else {}
    result = {"type": kind, "session_id": sid, "timestamp": row.get("timestamp")}
    if kind == "text":
        result["text"] = part.get("text") if isinstance(part.get("text"), str) else ""
    elif kind == "error":
        result["error"] = row.get("error") if isinstance(row.get("error"), dict) else {}
    elif kind in ("step_start", "step_finish", "tool_use"):
        result["part"] = part
    return result


def safe_provider_error(value: object) -> dict:
    """Keep stable error fields and drop arbitrary provider payload/secrets."""
    if not isinstance(value, dict):
        return {"type": "unknown", "message": "opencode-provider-error"}
    out = {}
    for key in ("type", "code", "status", "name"):
        item = value.get(key)
        if isinstance(item, (str, int)) and not isinstance(item, bool):
            out[key] = item
    message = value.get("message")
    if isinstance(message, str):
        # Provider messages may echo credentials or request headers; retain a
        # small redacted diagnostic rather than the raw response body.
        message = re.sub(r"(?i)(bearer\s+|sk-[A-Za-z0-9_-]{8,})\S*", "[redacted]", message)
        out["message"] = message[:400]
    return out or {"type": "unknown", "message": "opencode-provider-error"}


def record_model_http_result(model: str, status: int, body: bytes | str | dict = b"") -> dict:
    """Record only successful requests or explicit auth/entitlement/rate-limit denials."""
    if isinstance(status, bool) or not isinstance(status, int):
        return {"state": "unknown", "reason": "http-status-invalid"}
    if 200 <= status < 300:
        record_model_outcome(model, success=True, source="opencode-v2-http")
        return {"state": "usable", "reason": None}
    classification = classify_usage_rejection(status, body)
    if classification:
        error = {"status": status, "code": "api_key_expired" if classification.get("reason") == "credential-expired"
                 else classification.get("reason"),
                 "message": "provider request rejected"}
        record_model_outcome(model, error=error, source="opencode-v2-http",
                             reason_override=classification.get("reason"))
        return classification
    if status == 429:
        error = {"status": status, "code": "rate_limited", "message": "provider rate limited"}
        record_model_outcome(model, error=error, source="opencode-v2-http")
        return {"state": "unavailable", "reason": "rate-limited", "http_status": status}
    return {"state": "unknown", "reason": "unclassified-http", "http_status": status}


def _record_provider_command() -> int:
    """Private plugin bridge; reads one bounded JSON value from stdin and prints no body."""
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("action", choices=("record-provider-response",))
    parser.add_argument("--model", required=True)
    parser.add_argument("--status", required=True, type=int)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            result = {"state": "unknown", "reason": "response-too-large"}
        else:
            result = record_model_http_result(args.model, args.status, raw)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception:
        print('{"state":"unknown","reason":"observer-failed"}')
        return 0


if __name__ == "__main__":
    raise SystemExit(_record_provider_command())


def _table_columns(connection, table):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(table)):
        return set()
    return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _decode_json(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


def _session_generation_rows(connection, session_id):
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('session','session_v2')")}
    found = []
    for table, generation in (("session", "v1"), ("session_v2", "v2")):
        if table not in tables:
            continue
        columns = _table_columns(connection, table)
        if not {"id", "directory", "time_updated"}.issubset(columns):
            continue
        fields = ("id", "directory", "time_created", "time_updated", "title", "slug",
                  "agent", "model", "cost", "tokens_input", "tokens_output",
                  "tokens_reasoning", "parent_id")
        selected = [field for field in fields if field in columns]
        row = connection.execute(
            "SELECT " + ", ".join('"' + field + '"' for field in selected)
            + f' FROM "{table}" WHERE id=? LIMIT 1', (session_id,)).fetchone()
        if row:
            found.append((generation, table, dict(zip(selected, row))))
    return found


def _message_text_v2(data, message_type):
    if not isinstance(data, dict):
        return ""
    parts = []
    if message_type == "user":
        value = data.get("text")
        if isinstance(value, str):
            parts.append(value)
    elif message_type == "assistant":
        for part in data.get("content", []) if isinstance(data.get("content"), list) else []:
            if (isinstance(part, dict) and part.get("type") == "text"
                    and not part.get("synthetic") and not part.get("ignored")
                    and isinstance(part.get("text"), str)):
                parts.append(part["text"])
    return "\n".join(parts)[-MAX_MESSAGE_TEXT:]


def _read_v2_messages(connection, session_id, after_seq=None):
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_message'")}
    if "session_message" not in tables:
        return [], None
    columns = _table_columns(connection, "session_message")
    if not {"session_id", "type", "seq", "data"}.issubset(columns):
        return [], None
    condition = "session_id=?"
    parameters = [session_id]
    incremental = isinstance(after_seq, int) and not isinstance(after_seq, bool) and after_seq > 0
    if incremental:
        condition += " AND seq>?"
        parameters.append(after_seq)
    parameters.append(MAX_SESSION_MESSAGES)
    order = "ASC" if incremental else "DESC"
    rows = connection.execute(
        "SELECT type, seq, data, time_created, time_updated FROM session_message WHERE "
        + condition + " ORDER BY seq " + order + " LIMIT ?", parameters,
    ).fetchall()
    if not incremental:
        rows = list(reversed(rows))
    messages = []
    max_seq = None
    for message_type, seq, raw, created, updated in rows:
        data = _decode_json(raw)
        if data is None:
            continue
        max_seq = max(seq, max_seq or seq)
        text = _message_text_v2(data, message_type)
        tools = []
        if message_type == "assistant":
            for part in data.get("content", []) if isinstance(data.get("content"), list) else []:
                if not isinstance(part, dict) or part.get("type") != "tool":
                    continue
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                if state.get("status") in ("streaming", "running"):
                    tools.append({"name": part.get("name"), "status": state.get("status")})
        messages.append({"type": message_type, "seq": seq, "text": text,
                         "created": created, "updated": updated,
                         "completed": (data.get("time") or {}).get("completed")
                         if isinstance(data.get("time"), dict) else None,
                         "agent": data.get("agent"), "model": data.get("model"),
                         "tokens": data.get("tokens"),
                         "tools": tools,
                         "finish": data.get("finish"),
                         "has_error": bool(data.get("error"))})
    return messages, max_seq


def _read_v2_shell_tasks(connection, session_id, after_seq=0, refresh_seqs=()):
    """Bounded exact V2 shell-task catch-up plus a mutable recent-row window.

    Assistant shell tool parts and native ``session_message`` shell rows are
    separate native ID namespaces. Paging covers every row type so unrelated
    records cannot pin the cursor; exact known mutable sequences are refreshed
    independently of the recent-row window.
    """
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_message'")}
    if "session_message" not in tables:
        return [], {"state": "unknown", "reason": "v2-session-message-table-missing",
                    "next_seq": after_seq, "caught_up": True, "max_seq": None}
    columns = _table_columns(connection, "session_message")
    if not {"session_id", "type", "seq", "data"}.issubset(columns):
        return [], {"state": "unknown", "reason": "v2-session-message-schema-unsupported",
                    "next_seq": after_seq, "caught_up": True, "max_seq": None}
    created_column = '"time_created"' if "time_created" in columns else "NULL"
    updated_column = '"time_updated"' if "time_updated" in columns else "NULL"
    maximum = connection.execute(
        "SELECT MAX(seq) FROM session_message WHERE session_id=?", (session_id,)).fetchone()[0]
    maximum = maximum if isinstance(maximum, int) and not isinstance(maximum, bool) else None
    after_seq = after_seq if isinstance(after_seq, int) and not isinstance(after_seq, bool) else 0
    requested_refresh = []
    refresh_gap = False
    try:
        refresh_values = iter(refresh_seqs or ())
    except TypeError:
        refresh_values = iter(())
    seen_refresh = set()
    for seq in refresh_values:
        if (not isinstance(seq, int) or isinstance(seq, bool) or seq < 0
                or seq in seen_refresh):
            continue
        if len(requested_refresh) >= MAX_SHELL_TASK_REFRESH:
            refresh_gap = True
            break
        seen_refresh.add(seq)
        requested_refresh.append(seq)
    page = connection.execute(
        "SELECT type, seq, data, " + created_column + ", " + updated_column + " FROM session_message "
        "WHERE session_id=? AND seq>? ORDER BY seq ASC LIMIT ?",
        (session_id, after_seq, MAX_SHELL_TASK_PAGE),
    ).fetchall()
    recent = connection.execute(
        "SELECT type, seq, data, " + created_column + ", " + updated_column + " FROM session_message "
        "WHERE session_id=? ORDER BY seq DESC LIMIT ?",
        (session_id, MAX_SHELL_TASK_RECENT),
    ).fetchall()
    refreshed = []
    if requested_refresh:
        placeholders = ",".join("?" for _ in requested_refresh)
        refreshed = connection.execute(
            "SELECT type, seq, data, " + created_column + ", " + updated_column
            + " FROM session_message WHERE session_id=? AND seq IN (" + placeholders + ")",
            [session_id, *requested_refresh],
        ).fetchall()
    page = [row for row in page if isinstance(row[1], int) and not isinstance(row[1], bool)]
    recent = [row for row in recent if isinstance(row[1], int) and not isinstance(row[1], bool)]
    refreshed = [row for row in refreshed
                 if isinstance(row[1], int) and not isinstance(row[1], bool)]
    rows_by_seq = {row[1]: row for row in page}
    rows_by_seq.update({row[1]: row for row in recent})
    rows_by_seq.update({row[1]: row for row in refreshed})
    tasks = {}
    parse_reasons = []
    mutable_seqs = set()
    terminal_statuses = {"exited", "completed", "complete", "failed", "error",
                         "cancelled", "canceled", "stopped"}
    for message_type, seq, raw, created, updated in sorted(
            rows_by_seq.values(), key=lambda row: row[1]):
        data = _decode_json(raw)
        if not isinstance(data, dict):
            parse_reasons.append("session-message-json-invalid")
            continue
        if message_type == "shell":
            metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            background = metadata.get("background") is True
            status = data.get("status") if isinstance(data.get("status"), str) else None
            status_key = status.lower() if status else ""
            if status_key in {"running", "starting", "queued"} or (
                    background and status_key not in terminal_statuses):
                mutable_seqs.add(seq)
            if not background:
                continue
            native_id = data.get("shellID")
            if not isinstance(native_id, str) or not native_id:
                parse_reasons.append("native-shell-id-unavailable")
                continue
            times = data.get("time") if isinstance(data.get("time"), dict) else {}
            output = data.get("output") if isinstance(data.get("output"), dict) else {}
            task_key = ("session-message-shellID", native_id)
            tasks[task_key] = {
                "native_id": native_id,
                "native_id_kind": "session-message-shellID",
                "native_record_kind": "session-message-shell",
                "name": "shell",
                "input": {"command": data.get("command"), "background": True},
                "native_status": status or "unknown",
                "native_status_source": "session_message.shell.status",
                "started_at_ms": times.get("created") if times.get("created") is not None
                else created,
                "start_source": "session_message.shell.time.created"
                if times.get("created") is not None else "session_message.time_created",
                "completed_at_ms": times.get("completed"),
                "exit_code": data.get("exit") if isinstance(data.get("exit"), int)
                and not isinstance(data.get("exit"), bool) else None,
                "native_output_observed": isinstance(data.get("output"), dict),
                "native_output_cursor": output.get("cursor")
                if isinstance(output.get("cursor"), int) and not isinstance(output.get("cursor"), bool)
                and output.get("cursor") >= 0 else None,
                "native_output_size": output.get("size")
                if isinstance(output.get("size"), int) and not isinstance(output.get("size"), bool)
                and output.get("size") >= 0 else None,
                "native_output_truncated": output.get("truncated")
                if isinstance(output.get("truncated"), bool) else None,
                "seq": seq, "time_updated": updated,
            }
            continue

        if message_type != "assistant":
            continue
        content = data.get("content") if isinstance(data.get("content"), list) else []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "tool" or part.get("name") != "shell":
                continue
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            inputs = state.get("input") if isinstance(state.get("input"), dict) else None
            if not isinstance(inputs, dict) or inputs.get("background") is not True:
                continue
            invocation_status = state.get("status")
            if invocation_status in ("streaming", "running"):
                mutable_seqs.add(seq)
            times = part.get("time") if isinstance(part.get("time"), dict) else {}
            native_id = part.get("id") or part.get("callID")
            if not isinstance(native_id, str) or not native_id:
                parse_reasons.append("assistant-shell-native-id-unavailable")
                continue
            task_key = ("assistant-tool-part", native_id)
            tasks[task_key] = {
                "native_id": native_id,
                "native_id_kind": "assistant-tool-part",
                "native_record_kind": "assistant-shell-tool-part",
                "call_id": part.get("callID"),
                "name": "shell",
                "input": {"command": inputs.get("command"), "background": True},
                "invocation_status": invocation_status,
                "started_at_ms": times.get("ran") or times.get("created") or created,
                "start_source": "assistant-shell-part.time.ran|created",
                "invocation_completed_at_ms": times.get("completed"),
                "seq": seq, "time_updated": updated,
            }
    last_page = max((row[1] for row in page), default=after_seq)
    next_seq = max(after_seq, last_page)
    caught_up = maximum is None or next_seq >= maximum
    reason = ("multiple-session-message-observation-gaps" if len(set(parse_reasons)) > 1
              else parse_reasons[0] if parse_reasons else
              "pending-row-refresh-limit" if refresh_gap else None)
    return list(tasks.values()), {"state": "partial" if reason else "observed",
                                  "reason": reason,
                                  "reasons": list(dict.fromkeys(parse_reasons +
                                                   (["pending-row-refresh-limit"] if refresh_gap else []))),
                                  "next_seq": next_seq, "caught_up": caught_up,
                                  "max_seq": maximum,
                                  "mutable_seqs": sorted(mutable_seqs),
                                  "refreshed_seqs": requested_refresh}


def read_session_shell_tasks(connection, session_id: str, *, after_seq: int = 0,
                             refresh_seqs=()) -> dict:
    """Read background task evidence from an already-open private DB snapshot."""
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        return {"state": "unknown", "reason": "session-id-invalid", "generation": None,
                "shell_tasks": [], "shell_cursor": {"state": "unknown", "reason": "session-id-invalid"}}
    try:
        candidates = _session_generation_rows(connection, session_id)
        if not candidates:
            return {"state": "missing", "reason": "exact-session-not-found", "generation": None,
                    "shell_tasks": [], "shell_cursor": {"state": "missing", "reason": "exact-session-not-found",
                                                             "caught_up": True}}
        if len(candidates) != 1:
            return {"state": "unknown", "reason": "session-generation-ambiguous", "generation": None,
                    "shell_tasks": [], "shell_cursor": {"state": "unknown",
                                                             "reason": "session-generation-ambiguous",
                                                             "caught_up": True}}
        generation = candidates[0][0]
        if generation != "v2":
            reason = "v1-background-native-record-not-observed"
            return {"state": "unobserved", "reason": reason, "generation": generation,
                    "shell_tasks": [], "shell_cursor": {"state": "unobserved", "reason": reason,
                                                             "caught_up": True}}
        tasks, cursor = _read_v2_shell_tasks(connection, session_id, after_seq, refresh_seqs)
        return {"state": "observed", "reason": None, "generation": generation,
                "shell_tasks": tasks, "shell_cursor": cursor}
    except sqlite3.Error:
        return {"state": "unknown", "reason": "database-read-failed", "generation": None,
                "shell_tasks": [], "shell_cursor": {"state": "unknown", "reason": "database-read-failed"}}


def _read_v1_messages(connection, session_id):
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('message','part')")}
    if not {"message", "part"}.issubset(tables):
        return [], None
    message_columns = _table_columns(connection, "message")
    part_columns = _table_columns(connection, "part")
    if not {"id", "session_id", "role", "time_created", "time_updated"}.issubset(message_columns):
        return [], None
    rows = connection.execute(
        "SELECT id, role, time_created, time_updated, data FROM message WHERE session_id=? "
        "ORDER BY time_created DESC LIMIT ?", (session_id, MAX_SESSION_MESSAGES),
    ).fetchall()
    messages = []
    cursor = None
    for message_id, role, created, updated, raw in reversed(rows):
        data = _decode_json(raw) or {}
        if part_columns and {"session_id", "message_id", "data"}.issubset(part_columns):
            parts = connection.execute(
                "SELECT data FROM part WHERE session_id=? AND message_id=? "
                "ORDER BY rowid DESC LIMIT ?", (session_id, message_id, MAX_SESSION_MESSAGES),
            ).fetchall()
            visible = []
            for (part_raw,) in reversed(parts):
                part = _decode_json(part_raw)
                if (isinstance(part, dict) and part.get("type") == "text"
                        and not part.get("synthetic") and not part.get("ignored")
                        and isinstance(part.get("text"), str)):
                    visible.append(part["text"])
            text = "\n".join(visible)[-MAX_MESSAGE_TEXT:]
        else:
            text = str(data.get("content") or "")[-MAX_MESSAGE_TEXT:]
        messages.append({"type": role, "seq": None, "text": text,
                         "created": created, "updated": updated,
                         "completed": (data.get("time") or {}).get("completed")
                         if isinstance(data.get("time"), dict) else None,
                         "agent": data.get("agent"), "model": data.get("model"),
                         "finish": data.get("finish"),
                         "has_error": bool(data.get("error"))})
        cursor = updated if cursor is None else max(cursor, updated)
    return messages, cursor


def read_session_observation(session_id: str, *, db_path: str | Path | None = None,
                             generation: str | None = None,
                             after_seq: int | None = None,
                             shell_after_seq: int | None = 0,
                             shell_refresh_seqs=()) -> dict:
    """Read one exact V1/V2 session with bounded, read-only schema-aware queries.

    Without an explicit runtime generation, only an exact sid row can select a
    schema. If that same sid appears in both generations the result is unknown;
    the mere presence of ``session_v2`` never declares the running CLI version.
    """
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        return {"state": "unknown", "reason": "session-id-invalid"}
    if generation not in (None, "v1", "v2"):
        return {"state": "unknown", "reason": "session-generation-invalid"}
    path = Path(db_path) if db_path is not None else opencode_db_path()
    if not path.is_file():
        return {"state": "unknown", "reason": "database-unavailable"}
    uri = "file:" + quote(str(path.resolve()), safe="/:\\") + "?mode=ro"
    connection = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=0.5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=500")
        candidates = _session_generation_rows(connection, session_id)
        if generation:
            candidates = [item for item in candidates if item[0] == generation]
        if not candidates:
            return {"state": "missing", "reason": "exact-session-not-found"}
        if len(candidates) != 1:
            return {"state": "unknown", "reason": "session-generation-ambiguous"}
        selected_generation, table, row = candidates[0]
        raw_model = _decode_json(row.get("model")) or {}
        if not raw_model and isinstance(row.get("model"), str):
            raw_model = {"id": row["model"]}
        if selected_generation == "v2":
            messages, cursor = _read_v2_messages(connection, session_id, after_seq)
            cursor_kind = "session-message-seq-v2"
            if cursor is None and isinstance(after_seq, int) and after_seq >= 0:
                cursor = after_seq
            if shell_after_seq is None:
                shell_tasks = []
                shell_cursor = {"state": "unobserved", "reason": "detail-refresh-deferred",
                                "next_seq": None, "caught_up": False, "max_seq": None}
            else:
                shell_tasks, shell_cursor = _read_v2_shell_tasks(
                    connection, session_id, shell_after_seq, shell_refresh_seqs)
        else:
            messages, cursor = _read_v1_messages(connection, session_id)
            cursor_kind = "message-updated-v1"
            shell_tasks = []
            shell_cursor = {"state": "unobserved",
                            "reason": "v1-background-native-record-not-observed",
                            "next_seq": None, "caught_up": False, "max_seq": None}
        try:
            db_stat = os.stat(path)
            db_identity = {"device": db_stat.st_dev, "inode": db_stat.st_ino}
        except OSError:
            db_identity = None
        return {"state": "observed", "generation": selected_generation, "table": table,
                "session": {"id": row.get("id"), "directory": row.get("directory"),
                            "parent_id": row.get("parent_id"), "title": row.get("title"),
                            "slug": row.get("slug"), "agent": row.get("agent"),
                            "model": raw_model, "time_created": row.get("time_created"),
                            "time_updated": row.get("time_updated"), "cost": row.get("cost"),
                            "tokens_input": row.get("tokens_input"),
                            "tokens_output": row.get("tokens_output"),
                            "tokens_reasoning": row.get("tokens_reasoning")},
                "messages": messages, "cursor": cursor,
                "cursor_kind": cursor_kind, "shell_tasks": shell_tasks,
                "shell_cursor": shell_cursor, "database_identity": db_identity}
    except (OSError, sqlite3.Error):
        return {"state": "unknown", "reason": "database-read-failed"}
    finally:
        if connection is not None:
            connection.close()


def list_session_candidates(directory: str, *, db_path: str | Path | None = None,
                            after_created: int | None = None,
                            before_created: int | None = None, limit=8) -> list[dict]:
    """Bounded candidate census for legacy cwd fallback; callers must reject ambiguity."""
    if not isinstance(directory, str) or not directory or len(directory) > 4096:
        return []
    limit = max(1, min(int(limit), 32))
    path = Path(db_path) if db_path is not None else opencode_db_path()
    if not path.is_file():
        return []
    uri = "file:" + quote(str(path.resolve()), safe="/:\\") + "?mode=ro"
    connection = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=0.5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=500")
        rows = []
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('session','session_v2')")}
        for table, generation in (("session", "v1"), ("session_v2", "v2")):
            if table not in tables:
                continue
            columns = _table_columns(connection, table)
            required = {"id", "directory", "time_created", "time_updated"}
            if not required.issubset(columns):
                continue
            selected = [name for name in ("id", "directory", "time_created", "time_updated",
                                          "title", "slug", "agent", "model", "parent_id")
                        if name in columns]
            clauses = ['directory=?']
            params = [directory]
            if "parent_id" in columns:
                clauses.append("parent_id IS NULL")
            if after_created is not None:
                clauses.append("time_created>=?")
                params.append(int(after_created))
            if before_created is not None:
                clauses.append("time_created<?")
                params.append(int(before_created))
            params.append(limit + 1)
            found = connection.execute(
                "SELECT " + ", ".join('"' + name + '"' for name in selected)
                + f' FROM "{table}" WHERE ' + " AND ".join(clauses)
                + " ORDER BY time_created DESC LIMIT ?", params,
            ).fetchall()
            for row in found:
                item = dict(row)
                item["generation"] = generation
                item["table"] = table
                model = item.get("model")
                item["model"] = _decode_json(model) or ({"id": model} if isinstance(model, str) else {})
                rows.append(item)
        return rows[:limit + 1]
    except (OSError, sqlite3.Error, ValueError):
        return []
    finally:
        if connection is not None:
            connection.close()
