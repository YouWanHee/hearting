"""Disposable filename indexes for write-once/atomically replaced JSON records.

Directory entries supply names and inodes without one NAS stat/read per record.
The listing and checksum bind a compact key -> filename projection. Selected
records are always read and classified afresh; the projection is never evidence.
An unavailable projection uses the same scan and opportunistically repairs it.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

import route_identity


ROUTE_SIDECARS = (".outcome.json", ".gate-release.json")
_CACHE_NAMES = {".route-children-index.json", ".cycle-routes-index.json"}


def route_key(route_id, route_hash):
    return json.dumps([route_id, route_hash], separators=(",", ":"))


def route_keys(name, value):
    if (not isinstance(value, dict) or value.get("continuation_contract_version") != 1
            or Path(name).stem == value.get("source_route_id")
            or not isinstance(value.get("source_route_id"), str)
            or route_identity.route_hash(value) != value.get("route_hash")):
        return []
    return [route_key(value["source_route_id"], value.get("source_route_hash"))]


def cycle_keys(name, value):
    return ([value["route_id"]] if Path(name).suffix == ".json" and isinstance(value, dict)
            and isinstance(value.get("route_id"), str)
            else [])


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("utf-8")).hexdigest()


def _listing(directory, ignored):
    try:
        with os.scandir(directory) as entries:
            return {entry.name: entry.inode() for entry in entries
                    if entry.name.endswith(".json") and entry.name not in _CACHE_NAMES
                    and not entry.name.endswith(ignored)}
    except FileNotFoundError:
        return {}


def _path(directory, kind):
    return Path(directory) / f".{kind}-index.json"


def _load(path):
    try:
        if path.is_symlink():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != 1:
            return None
        body = {key: value[key] for key in ("version", "listing", "listing_digest", "groups")}
        if value.get("digest") != _digest(body) or body["listing_digest"] != _digest(body["listing"]):
            return None
        listing, groups = body["listing"], body["groups"]
        if not isinstance(listing, dict) or not isinstance(groups, dict):
            return None
        if any(not isinstance(name, str) or Path(name).name != name
               or not name.endswith(".json") or name in _CACHE_NAMES
               or type(inode) is not int for name, inode in listing.items()):
            return None
        if any(not isinstance(key, str) or not isinstance(names, list)
               or any(not isinstance(name, str) or name not in listing for name in names)
               or names != sorted(set(names)) for key, names in groups.items()):
            return None
        return body
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _save(path, listing, groups):
    """Best effort, atomic, and preserve a foreign symlink at the cache name."""
    temporary = None
    try:
        if path.is_symlink():
            return
        body = {"version": 1, "listing": listing, "listing_digest": _digest(listing), "groups": groups}
        data = json.dumps(dict(body, digest=_digest(body)), ensure_ascii=True, separators=(",", ":")) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
        if not path.is_symlink():
            os.replace(temporary, path)
            temporary = None
    except OSError:
        pass
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def select(directory, keys, *, kind, classify, read_json, ignored=()):
    """Read records whose current classification intersects ``keys``.

    A concurrent publication invalidates the listing binding. Rebuild once,
    without a retry loop or an obligation on the caller.
    """
    directory = Path(directory)
    keys = set(keys)
    before = _listing(directory, ignored)
    path = _path(directory, kind)
    held = _load(path)
    if held is not None and held["listing"] == before:
        names = sorted({name for key in keys for name in held["groups"].get(key, [])})
        rows = []
        valid = True
        for name in names:
            value = read_json(directory / name)
            actual = set(classify(name, value))
            expected = {key for key, members in held["groups"].items() if name in members}
            if actual != expected:
                valid = False
                break
            rows.append(value)
        if valid and _listing(directory, ignored) == before:
            return rows
        before = _listing(directory, ignored)
    groups, rows, complete = {}, [], True
    for name in sorted(before):
        value = read_json(directory / name)
        if value is None:
            complete = False
        actual = set(classify(name, value))
        for key in actual:
            groups.setdefault(key, []).append(name)
        if keys & actual:
            rows.append(value)
    if complete and _listing(directory, ignored) == before:
        _save(path, before, groups)
    return rows


def published(path, value, *, kind, classify, ignored=()):
    """Maintain a complete existing index after an ordinary record publication.

    Omit a cold or concurrently changed index; the next lookup rebuilds it.
    No writer reads unrelated record payloads, and a failed update changes no
    publication result.
    """
    try:
        path = Path(path)
        cache = _path(path.parent, kind)
        held = _load(cache)
        if held is None:
            return
        listing = _listing(path.parent, ignored)
        if path.name not in listing or ({name: inode for name, inode in held["listing"].items() if name != path.name}
                                        != {name: inode for name, inode in listing.items() if name != path.name}):
            return
        groups = {key: [name for name in names if name != path.name]
                  for key, names in held["groups"].items()}
        for key in classify(path.name, value):
            groups.setdefault(key, []).append(path.name)
        groups = {key: sorted(set(names)) for key, names in groups.items() if names}
        if _listing(path.parent, ignored) == listing:
            _save(cache, listing, groups)
    except (OSError, ValueError, TypeError):
        pass
