"""Narrow compatibility and read-only account boundary for OpenCode 1.x/2.x.

OpenCode 2 uses a shared service, a different ``run`` CLI, and SQLite-backed
credentials. Keep version-specific mechanics here so callers do not infer V2
support from V1 flags or read the database independently.
"""
from __future__ import annotations

from functools import lru_cache
import json
import hashlib
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from urllib.parse import quote


VERSION_RE = re.compile(r"(?:^|\D)(\d+)\.(\d+)\.(\d+)(?:\D|$)")
SESSION_ID_RE = re.compile(r"^ses_[A-Za-z0-9]{1,252}$")
MAX_CREDENTIAL_ROWS = 64
MAX_AUTH_BYTES = 64 * 1024
MAX_SESSION_MESSAGES = 64
MAX_MESSAGE_TEXT = 2048


class OpenCodeRuntimeError(RuntimeError):
    """A bounded runtime or credential read could not be completed safely."""


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
             version: tuple[int, int, int] | None = None) -> list[str]:
    """Build a run command while preserving the V1 spelling and V2 cwd contract.

    The caller supplies ``cwd`` to ``Popen``/the shell. V2 removed ``--dir`` and
    ``--variant``; a V2 model variant is part of ``provider/model#variant``.
    """
    modern = is_v2(binary, version=version)
    result = [binary, "run"]
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


def _legacy_account(integration_id: str, environ) -> dict | None:
    home = Path(environ.get("HOME") or Path.home())
    data = Path(environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    auth_path = environ.get("OPENCODE_AUTH") or data / "opencode" / "auth.json"
    return read_legacy_credential(integration_id, auth_path=auth_path)


def account_scope(integration_id: str, *, environ=None) -> str:
    """Stable opaque scope for the runtime's selected account; never a secret."""
    env = os.environ if environ is None else environ
    row = (read_credential(integration_id, environ=env) if is_v2(environ=env)
           else _legacy_account(integration_id, env))
    return _scope_for_row(integration_id, row)


def provider_account_scope(provider: str, *, environ=None) -> str:
    """Resolve Go models against the account OpenCode actually uses for Go."""
    env = os.environ if environ is None else environ
    if provider != "opencode-go" or not is_v2(environ=env):
        return account_scope(provider, environ=env)
    oauth = read_credential("opencode", environ=env)
    if oauth:
        return _scope_for_row("opencode", oauth)
    api_key = read_credential("opencode-go", environ=env)
    if api_key:
        return _scope_for_row("opencode-go", api_key)
    return _scope_for_row("opencode-go", _legacy_account("opencode-go", env))


def model_availability(model: str, *, environ=None) -> dict:
    """Read provider/account/model evidence from Fleet's existing cache."""
    if not isinstance(model, str) or "/" not in model:
        return {"state": "unknown", "reason": "model-scope-unavailable"}
    provider, model_id = model.split("/", 1)
    model_id = model_id.split("#", 1)[0]
    scope = provider_account_scope(provider, environ=environ)
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
        if provider == "opencode-go" and result.get("state") == "unknown":
            entitlement_scope = account_scope("opencode", environ=environ)
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
    scope = provider_account_scope(provider)
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


def read_credential(integration_id: str, *, db_path: str | Path | None = None,
                    environ=None) -> dict | None:
    """Read one native selected credential with bounded, read-only SQLite access.

    The returned object contains the secret only in process memory for its
    immediate caller. Callers must not serialize or log its ``value`` field.
    """
    if not isinstance(integration_id, str) or not integration_id or len(integration_id) > 128:
        return None
    path = Path(db_path) if db_path is not None else opencode_db_path(environ)
    if not path.is_file():
        return None
    uri = "file:" + quote(str(path.resolve()), safe="/:\\") + "?mode=ro"
    con = None
    try:
        con = sqlite3.connect(uri, uri=True, timeout=0.5)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        con.execute("PRAGMA busy_timeout=500")
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='credential'")}
        if "credential" not in tables:
            return None
        columns = {row[1] for row in con.execute("PRAGMA table_info(credential)")}
        scope_col = "integration_id" if "integration_id" in columns else (
            "connector_id" if "connector_id" in columns else None)
        if scope_col is None or "value" not in columns:
            return None
        selected = [name for name in ("id", "label", "value", "active", "time_created", "time_updated")
                    if name in columns]
        order = ", ".join(name + " DESC" for name in ("active", "time_created", "time_updated", "id")
                           if name in columns)
        rows = con.execute(
            "SELECT " + ", ".join('"' + name + '"' for name in selected)
            + " FROM credential WHERE \"" + scope_col + "\"=?"
            + (" ORDER BY " + order if order else "") + " LIMIT ?",
            (integration_id, MAX_CREDENTIAL_ROWS),
        ).fetchall()
        normalized = []
        for row in rows:
            item = dict(row)
            item.setdefault("active", None)
            item["value"] = _decode_credential(item.get("value"))
            normalized.append(item)
        chosen = _selected_row(normalized)
        if not chosen:
            return None
        # No secret is returned in diagnostic metadata; the caller needs it
        # only for an immediate request or local eligibility classification.
        # Keep a selected malformed row present with value=None so callers do
        # not mistake it for absent OAuth and fall through to a stale key.
        return {"integration_id": integration_id, "credential_id": chosen.get("id"),
                "label": chosen.get("label"), "value": chosen["value"],
                "source": "opencode-sqlite",
                "credential_valid": bool(chosen["value"])}
    except (OSError, sqlite3.Error, OpenCodeRuntimeError):
        return None
    finally:
        if con is not None:
            con.close()


def read_legacy_credential(integration_id: str, *, auth_path: str | Path) -> dict | None:
    """Read the selected V1 ``auth.json`` entry without returning file contents."""
    path = Path(auth_path).expanduser()
    try:
        if path.stat().st_size > MAX_AUTH_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    entry = value.get(integration_id) if isinstance(value, dict) else None
    if not isinstance(entry, dict):
        return None
    return {"integration_id": integration_id, "label": None, "value": entry,
            "source": "legacy-auth-json"}


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
                             after_seq: int | None = None) -> dict:
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
        else:
            messages, cursor = _read_v1_messages(connection, session_id)
            cursor_kind = "message-updated-v1"
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
                "cursor_kind": cursor_kind}
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
