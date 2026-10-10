"""The identified conversation's directory, projected after pane attribution.

Collectors supply only their already bound session's native record. This module
does not select a session, change a process, or derive a directory from neighbors.
"""
import json
import os
from collections import OrderedDict

_CACHE = OrderedDict()
_CACHE_MAX = 256


def valid_cwd(value):
    return value if isinstance(value, str) and os.path.isabs(value) and "\0" not in value else None


def _rows_backward(handle, size, chunk=65536):
    """Complete JSONL rows only; a partially appended last row is not evidence."""
    end, carry = size, b""
    complete = False
    while end > 0:
        start = max(0, end - chunk)
        handle.seek(start)
        data = handle.read(end - start) + carry
        lines = data.split(b"\n")
        if not complete:
            if len(lines) == 1:
                carry, end = data, start
                continue
            lines.pop()  # either the empty suffix or an incomplete append
            complete = True
        carry = lines.pop(0) if start > 0 else b""
        for raw in reversed(lines):
            yield raw
        end = start


def jsonl_cwd(path, harness, sid):
    """Latest native cwd within this exact transcript, cached by full file stamp."""
    if not path or not sid:
        return None
    key = (os.path.realpath(path), harness, sid)
    try:
        st = os.stat(path)
        stamp = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        cached = _CACHE.get(key)
        if cached and cached[0] == stamp:
            _CACHE.move_to_end(key)
            return cached[1]
        cwd = None
        with open(path, "rb") as handle:
            if harness == "codex":
                meta = json.loads(handle.readline())
                payload = meta.get("payload") or {}
                if meta.get("type") != "session_meta" or payload.get("id", sid) != sid:
                    return None
                cwd = valid_cwd(payload.get("cwd"))
            for raw in _rows_backward(handle, st.st_size):
                if b'"cwd"' not in raw:
                    continue
                try:
                    row = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(row, dict):
                    continue
                if harness == "claude":
                    candidate = row.get("cwd") if row.get("sessionId") == sid else None
                elif harness == "codex":
                    payload = row.get("payload")
                    candidate = payload.get("cwd") if row.get("type") == "turn_context" and isinstance(payload, dict) else None
                else:
                    return None
                if valid_cwd(candidate):
                    cwd = candidate
                    break
        after = os.stat(path)
        if stamp == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            _CACHE[key] = (stamp, cwd)
            _CACHE.move_to_end(key)
            while len(_CACHE) > _CACHE_MAX:
                _CACHE.popitem(last=False)
        return cwd
    except (OSError, ValueError, TypeError, AttributeError):
        _CACHE.pop(key, None)
        return None


def observe(session, cwd, sid):
    """Hold a candidate until native identity and foreground pane reads finish."""
    if valid_cwd(cwd) and sid:
        session._session_cwd = (sid, cwd)


def project(sessions):
    """Display/group by the current session's directory; retain process fallback."""
    for session in sessions:
        sid, cwd = getattr(session, "_session_cwd", (None, None))
        if not sid or sid != session.session_id or not valid_cwd(cwd):
            continue
        old_slug = os.path.basename((session.cwd or "").rstrip("/"))
        if not session.slug or session.slug == old_slug:
            session.slug = os.path.basename(cwd.rstrip("/"))
        session.cwd = cwd
