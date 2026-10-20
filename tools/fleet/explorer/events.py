"""Pure recent-event projection with identity-based deduplication."""

from collections.abc import Mapping


def _get(value, *names, default=None):
    for name in names:
        item = value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)
        if item is not None and item != "":
            return item
    return default


def _sort_key(record, index):
    stamp = _get(record, "timestamp", "time", "observed_at", default="")
    return (str(stamp), index)


def build_events(records, width=None):
    """Return newest-first event lines, deduped by source identity/reference."""
    unique = {}
    for index, record in enumerate(records or ()):
        identity = _get(record, "identity", "event_id", "id", "ref")
        stamp = _get(record, "timestamp", "time", "observed_at", default="")
        text = _get(record, "text", "message", "label", "kind", default="사건 근거 미확인")
        key = (str(identity), str(stamp), str(text)) if identity is not None else ("row", index)
        unique.setdefault(key, (index, record, str(text), stamp, identity))
    ordered = sorted(unique.values(), key=lambda item: _sort_key(item[1], item[0]), reverse=True)
    result = []
    for _index, _record, text, stamp, identity in ordered:
        prefix = " ".join(str(part) for part in (stamp, identity) if part not in (None, ""))
        line = f"{prefix} {text}".strip()
        if width is not None:
            from .output import clip
            line = clip(line, max(0, int(width)))
        result.append(line)
    return tuple(result)
