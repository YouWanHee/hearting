"""Small read-only session helpers shared by the live snapshot adapter."""

from collections.abc import Mapping


def _get(value, *names, default=None):
    for name in names:
        item = value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)
        if item is not None and item != "":
            return item
    return default


def is_main(session):
    return not _get(session, "is_child") and _get(session, "process_role") != "registered-worker"


def find_session(snapshot, sid):
    for session in _get(snapshot, "sessions", default=()) or ():
        ids = [_get(session, "session_id", "sid", "id")]
        ids.extend(_get(session, "session_aliases", default=()) or ())
        if sid and sid in ids:
            return session
    return None
