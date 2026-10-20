"""Pure system-section projection using caller-supplied render providers."""

from collections.abc import Mapping


def _call(provider, snapshot, state, width):
    if provider is None:
        return None
    if callable(provider):
        return provider(snapshot, state, width)
    return provider


def _rows(value):
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(row) for row in value)


def render_system(snapshot=None, state=None, width=0, providers=None):
    """Compose usage, governor and compute rows without inventing missing data.

    Providers are expected to wrap the established Fleet render helpers. Missing
    sources remain visibly unknown, and measured and reserved rows stay separate.
    """
    providers = providers or {}
    result = []
    sections = (("usage", "Usage"), ("governor", "Governor"),
                ("reserved", "Reserved"), ("compute", "Compute"),
                ("measured", "Measured"))
    for key, label in sections:
        provider = providers.get(key)
        value = _call(provider, snapshot, state, width)
        if value is None and key in ("usage", "compute"):
            value = _get_snapshot(snapshot, key)
        rows = _rows(value)
        if not rows:
            if key in ("usage", "compute"):
                result.append(f"{label}: ?")
            continue
        result.extend(row if row.startswith(label) else f"{label}: {row}" for row in rows)
    return tuple(result)


def _get_snapshot(snapshot, name):
    if isinstance(snapshot, Mapping):
        return snapshot.get(name)
    return getattr(snapshot, name, None) if snapshot is not None else None
