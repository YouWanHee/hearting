"""Cross-harness session registry — reader + writer (F-26b, plan.md §7.1).

Claude Code has always had a tier-1 identity file (``~/.claude/sessions/<pid>.json``,
written by the runtime itself) that gives a session its name, status, and PID-reuse
guard. Codex and OpenCode had no equivalent, so their rows fell back to weaker
tier-2/tier-3 evidence. This module is the harness-neutral contract both sides
share: Claude stays runtime-native and read-only here; a hearting-managed Codex
launch (``utilities/codex-managed-entry.py`` + ``codex-managed-gateway.py``) is the
first writer; OpenCode has no writer yet (``writer_support("opencode") ==
"not-implemented"``).

Hearting-owned records live under ``FLEET_SESSION_REGISTRY_DIR`` when set, else
``${XDG_STATE_HOME:-~/.local/state}/agent-fleet/sessions/<harness>/<pid>.json`` — the
same state-root shape as ``tools/fleet/interaction.py``, deliberately NOT the
``AGENT_DISPATCH_JOBS``-anchored resolver chain that peer-message ledgers used
(that coupling is exactly what split the peer ledger into two roots, plan.md §2.2).

Every reader call returns every key in ``FIELDS`` — an absent value is ``None``,
never synthesized. A missing, foreign-owned, oversized, symlinked, or malformed
file is silence (``None``), matching ``interaction.py``'s validation posture.

Import contract: this module must stay importable with only the repository's
``tools`` directory on ``sys.path`` (``from fleet import session_registry``,
mirroring ``peer-message.py:412``'s lazy sys.path insert for non-Fleet
consumers such as ``utilities/peer-steward.py``). It therefore never imports
``render``, ``model``, or ``collectors`` at module scope — only
``fleet.session_handle`` is imported lazily, inside ``apply_to_session``, and
only for the Claude branch.
"""
import json
import os
import stat
import tempfile

HARNESSES = ("claude", "codex", "opencode")
FIELDS = ("pid", "sessionId", "cwd", "startedAt", "procStart", "version", "kind",
          "entrypoint", "name", "nameSource", "nameSince", "status", "updatedAt",
          "statusUpdatedAt", "harness")
STATUSES = ("idle", "busy", "shell", "exited")
WRITER_SUPPORT = {"claude": "runtime-native",
                  "codex": "hearting-managed",
                  "opencode": "not-implemented"}

_MAX_BYTES = 64 * 1024


class RegistryWriteUnsupported(RuntimeError):
    """Raised by write()/remove() for a harness with no hearting-managed writer."""


def _check_harness(harness):
    if harness not in HARNESSES:
        raise ValueError("unknown harness: %r" % (harness,))


def writer_support(harness):
    _check_harness(harness)
    return WRITER_SUPPORT[harness]


def state_root():
    explicit = os.environ.get("FLEET_SESSION_REGISTRY_DIR")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(xdg, "agent-fleet", "sessions")


def _dir_for(harness, home=None):
    if harness == "claude":
        base = home or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
        return os.path.join(base, "sessions")
    return os.path.join(state_root(), harness)


def registry_path(harness, pid, *, home=None):
    _check_harness(harness)
    return os.path.join(_dir_for(harness, home=home), "%d.json" % int(pid))


def read(harness, pid, *, home=None):
    """dict with every FIELDS key present (absent → None), or None on any failure."""
    _check_harness(harness)
    path = registry_path(harness, pid, home=home)
    try:
        metadata = os.lstat(path)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            return None
        if metadata.st_size > _MAX_BYTES:
            return None
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    record = {field: payload.get(field) for field in FIELDS}
    record["harness"] = harness   # directory-declared identity wins over file content
    return record


def _prepare_directory(directory):
    os.makedirs(directory, mode=0o700, exist_ok=True)
    metadata = os.lstat(directory)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise OSError("session registry directory must be an owner directory")
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass


def write(harness, pid, fields, *, home=None):
    """Merge-update the record for (harness, pid); tmp → os.replace, 0600."""
    _check_harness(harness)
    if WRITER_SUPPORT[harness] != "hearting-managed":
        raise RegistryWriteUnsupported(harness)
    for key in fields:
        if key not in FIELDS:
            raise ValueError("unknown session registry field: %r" % (key,))
    path = registry_path(harness, pid, home=home)
    directory = os.path.dirname(path)
    _prepare_directory(directory)
    existing = read(harness, pid, home=home) or {}
    merged = dict(existing)
    merged.update(fields)
    merged["pid"] = int(pid)
    merged["harness"] = harness
    descriptor, tmp_path = tempfile.mkstemp(dir=directory, prefix=".session-", suffix=".tmp")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(merged, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return True


def remove(harness, pid, *, home=None):
    _check_harness(harness)
    if WRITER_SUPPORT[harness] != "hearting-managed":
        raise RegistryWriteUnsupported(harness)
    path = registry_path(harness, pid, home=home)
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _ms_to_sec(value):
    """registry epoch-ms → epoch-sec; anything non-numeric (or bool) → None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value / 1000.0


def apply_to_session(sess, record, harness):
    """Load every tier-1 registry field onto the Session (moved verbatim from the
    former claude.py:443 ``_apply_registry`` — F-26b generalizes it across harnesses).
    Each key is independently optional: a fresh row carrying only pid/sessionId must
    not lose the ones it has. The only harness-specific branch is the derived `<xx>`
    tag: Claude mints it from its own `derived` registry name; Codex/OpenCode mint
    their tag from the session id elsewhere (``collectors/codex.py`` `minted_tag`)
    and must not have this branch overwrite it.
    """
    sess.session_id = record.get("sessionId") or sess.session_id
    sess.status = record.get("status")               # idle | shell | busy | (absent → None)
    name = record.get("name")
    if name:
        sess.slug = name                              # friendly name disambiguates same-cwd sessions
        sess.registry_name = name                     # explicit link in the name chain (F-26)
        # F-99a ① — the registry `name` is a runtime-exposed user-set name only when
        # `nameSource` says the user actually set it (not the default derived label).
        if record.get("nameSource") != "derived":
            sess.runtime_name = name
        elif harness == "claude":
            # F-100a — the derived `<basename>-<xx>` name is the only carrier of the
            # 2-hex tag; read it while the record still says "derived".
            try:
                from fleet.session_handle import derived_tag
                sess.session_tag = derived_tag(name)
            except Exception:
                sess.session_tag = None
    kind = record.get("kind")
    if isinstance(kind, str):
        sess.kind = kind
    proc_start = record.get("procStart")
    if proc_start is not None and not isinstance(proc_start, bool):
        sess.registry_proc_start = str(proc_start)    # compared against /proc in the classifier
    sess.started_at = _ms_to_sec(record.get("startedAt"))
    sess.updated_at = _ms_to_sec(record.get("updatedAt"))


def name_for_session_id(session_id, harness, *, home=None):
    """The registry `name` for a session id, or None — used by peer-steward (C-3)
    to name a Codex/OpenCode sender the same way claude_session_name() already
    names a Claude one."""
    if not session_id:
        return None
    _check_harness(harness)
    directory = _dir_for(harness, home=home)
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    for entry in names:
        if not entry.endswith(".json"):
            continue
        try:
            pid = int(entry[:-len(".json")])
        except ValueError:
            continue
        record = read(harness, pid, home=home)
        if record and record.get("sessionId") == session_id:
            name = record.get("name")
            return name if isinstance(name, str) and name else None
    return None
