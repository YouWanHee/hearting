"""Fail-soft, read-only collector for the SD-122 peer-message ledger.

Never writes. Bounded per file (tail 64KB) and overall (24h window, 200 records).
A missing or unreadable ledger returns an empty result — every Session field this
feeds stays at its snapshot default so the rendered board is byte-identical to a
pre-SD-122 board.
"""
import glob
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

_TAIL_BYTES = 64 * 1024
_WINDOW_SECONDS = 24 * 3600
_MAX_RECORDS = 200

# C-2 — a herdr sender trailer written 2026-09-08T02:54Z~11:18Z carried a trailing
# " ; ref=<32hex>" transfer-ref suffix inside the display name (the writer
# already separates them today; this is read-side tolerance for the records
# still on disk from that window, display only — the ledger itself is untouched).
_REF_TRAILER_RE = re.compile(r"\s*;\s*ref=[0-9a-f]{32}\s*$")

_peer_message_module = None


def _clean_from_name(name):
    if not isinstance(name, str):
        return name
    return _REF_TRAILER_RE.sub("", name)


def _load_peer_message():
    """Lazy-load `utilities/peer-message.py` (hyphenated — not `import`able by
    name) once, so `_state_roots()` can ask it for the peer ledger's canonical
    root (C-8). Fail-soft and cached: a read-only collector must never raise,
    and re-executing the module on every tick would be wasted work."""
    global _peer_message_module
    if _peer_message_module is not None:
        return _peer_message_module
    here = Path(__file__).resolve()
    for candidate in here.parents:
        pm_path = candidate / "utilities" / "peer-message.py"
        if pm_path.is_file():
            try:
                spec = importlib.util.spec_from_file_location("peer_message", str(pm_path))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception:
                return None
            _peer_message_module = module
            return module
    return None


def _peer_ledger_root():
    module = _load_peer_message()
    if module is None:
        return None
    try:
        return str(module.peer_state_root())
    except Exception:
        return None


def _agent_home():
    from . import dispatch
    return dispatch._registry_home()


def _runtime_ledger_roots():
    """F-100c — each installed runtime resolves ITS OWN dispatch state root
    (`~/.codex/.harness/dispatch`, `~/.config/opencode/.harness/dispatch`, …; measured
    2026-09-03), so a Codex/OpenCode receiver's `notice` lands in that runtime's ledger,
    not in the stable per-user root the Claude side writes to. The board reads all of
    them; existence-gated, so a runtime that was never activated adds nothing."""
    from . import dispatch as _dispatch
    out = []
    for home in (_dispatch._codex_home(), _dispatch._proj_home(), _dispatch._opencode_config_home()):
        if not home:
            continue
        root = os.path.join(home, ".harness", "dispatch")
        if os.path.isdir(os.path.join(root, "peer-messages")) or os.path.isdir(os.path.join(root, "peer-steward")):
            out.append(root)
    return out


def _state_roots():
    """F-98d — read through the SAME resolver chain the writer (`peer-message.py`) uses,
    not `dispatch._row_state_roots()`'s no-row default (which pins to the release tree's
    `.dispatch` and ignores an inherited `AGENT_DISPATCH_JOBS`). Modelled on
    `tools/fleet/route.py`'s `_dispatch_state_roots` — lazy import, tolerant of every
    failure (never raises out of a read-only collector). F-100c appends every installed
    runtime's own dispatch root (`_runtime_ledger_roots`).

    C-8 — index 0 is always the peer ledger's canonical root
    (`peer-message.py::peer_state_root()`), promoting it to the front (never
    duplicating it) when it is already present. This is the same list
    `peer-message.py:412 steward_marker_roots` reads back through the
    "fleet-reader" branch, and its "chain index 0 = the writer's root" contract
    depends on this ordering. A resolver failure here is swallowed — the rest
    of the roots still apply."""
    roots = []
    try:
        home = _agent_home()
    except Exception:
        home = None
    if home is not None:
        here = Path(__file__).resolve()
        for candidate in here.parents:
            utilities_dir = candidate / "utilities"
            if (utilities_dir / "dispatch_contract.py").is_file():
                if str(utilities_dir) not in sys.path:
                    sys.path.insert(0, str(utilities_dir))
                try:
                    from dispatch_contract import dispatch_state_roots
                    roots = [str(root) for root in dispatch_state_roots(
                        Path(home), jobs=os.environ.get("AGENT_DISPATCH_JOBS"))]
                except Exception:
                    roots = []
                break
    try:
        for extra in _runtime_ledger_roots():
            if extra not in roots:
                roots.append(extra)
    except Exception:
        pass
    peer_root = _peer_ledger_root()
    if peer_root is not None:
        peer_norm = os.path.normpath(peer_root)
        roots = [r for r in roots if os.path.normpath(r) != peer_norm]
        roots.insert(0, peer_root)
    return tuple(roots)


def _tail_lines(path):
    try:
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
                fh.readline()  # discard the first, possibly-incomplete line
            return fh.read().splitlines()
    except OSError:
        return []


def _parse_ts(ts):
    try:
        raw = ts.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        from datetime import datetime
        return datetime.fromisoformat(raw).timestamp()
    except Exception:
        return None


def collect(state_roots=None):
    records = []
    malformed = 0
    now = time.time()
    roots = state_roots if state_roots is not None else _state_roots()
    seen_files = set()
    try:
        for root in roots:
            pattern = os.path.join(str(root), "peer-messages", "*", "*.jsonl")
            for path in glob.glob(pattern):
                real = os.path.realpath(path)
                if real in seen_files:
                    continue
                seen_files.add(real)
                for line in _tail_lines(path):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        malformed += 1
                        continue
                    ts = _parse_ts(rec.get("ts", ""))
                    if ts is None or (now - ts) > _WINDOW_SECONDS:
                        continue
                    records.append((ts, rec))
    except Exception:
        collect.last_diagnostics = []
        collect.last_malformed = malformed
        return {"records": [], "by_session": {}}

    records.sort(key=lambda pair: pair[0], reverse=True)
    records = records[:_MAX_RECORDS]

    ordered = sorted(records, key=lambda pair: pair[0])
    by_session = {}
    pending = {}

    def _key(block):
        sid = block.get("session_id")
        return (str(block.get("harness") or "").lower(), sid) if sid else None

    def _row(key):
        return by_session.setdefault(key, {"sent_1h": 0, "recv_1h": 0,
                                           "last_recv": None, "last_sent": None})

    def _peer_name(block, key):
        """The ledger's own display name for an endpoint, or ``None``.

        Never the raw session id. A codex→claude transfer record carries
        ``from.name: null`` (measured 2026-09-09), and the old fallback pasted the
        36-character id into the name slot, which is what put
        `← 01a084f7-63f2-7961-ae60-6fc2d8e60fc2` on the board. Naming an unnamed peer
        is the renderer's job — it can see the tag of a row the collector cannot.
        """
        name = _clean_from_name(block.get("name"))
        if not isinstance(name, str):
            return None
        name = name.strip()
        if not name:
            return None
        sid = key[1] if key else None
        return None if sid and name == sid else name

    def _record_recv(to_key, kind, frm, to, from_key, age_min):
        row = _row(to_key)
        if age_min <= 60:
            row["recv_1h"] += 1
        row["last_recv"] = {"from_name": _peer_name(frm, from_key),
                             "from_session_id": from_key[1] if from_key else None,
                             "from_harness": from_key[0] if from_key else "",
                             "kind": kind, "age_min": age_min}

    def _record_sent(from_key, kind, frm, to, to_key, age_min):
        row = _row(from_key)
        row["last_sent"] = {"to_name": _peer_name(to, to_key),
                            "to_session_id": to_key[1] if to_key else None,
                            "to_harness": (to_key[0] if to_key
                                           else str(to.get("harness") or "").lower()),
                            "kind": kind, "age_min": age_min}

    def _upgrade_recv(to_key, inherited, frm, to, from_key, age_min):
        # A correlated notice is, by construction, the newest successful receipt for
        # `to_key` (it is processed in ts order and only reached once its matching
        # sent record popped off `pending`) — so it replaces `last_recv` unconditionally,
        # even when a later record from a different sender had already overwritten it.
        # `recv_1h` is untouched here: the notice and its correlated sent record are one
        # logical message and must count once (F-101i).
        row = _row(to_key)
        row["last_recv"] = {"from_name": _peer_name(frm, from_key),
                             "from_session_id": from_key[1] if from_key else None,
                             "from_harness": from_key[0] if from_key else "",
                             "kind": inherited, "age_min": age_min}

    # Correlation is bounded by the existing tail/window/max limits. A clipped sender
    # leaves a notice as `notice` (honest miss); LIFO reduces but cannot eliminate an
    # ambiguity when the same exact pair sends several kinds in quick succession.
    for ts, rec in ordered:
        frm, to = rec.get("from") or {}, rec.get("to") or {}
        from_key, to_key = _key(frm), _key(to)
        age_min = max(0, int((now - ts) // 60))
        kind = rec.get("kind")
        status = ((rec.get("delivery") or {}).get("status") or "").lower()
        deliverable = status != "failed"
        if from_key:
            # The endpoint's row exists as soon as the ledger names it, but only a
            # non-notice record is a SEND. F-101i, send side: a herdr message writes two
            # records naming the same sender — its own `steer` and the receiver's `notice`
            # receipt — so counting both made one sent message read `✉ 2/…` (measured
            # 2026-09-09 on the codex↔claude test pair). `recv_1h` already counted such a
            # pair once; this is the same rule on the other axis, and it agrees with
            # `last_sent`, which was never set from a notice.
            row = _row(from_key)
            if kind != "notice" and age_min <= 60:
                row["sent_1h"] += 1
        if kind != "notice":
            if from_key and to_key and deliverable:
                pending.setdefault((from_key, to_key), []).append(kind)
            if to_key and deliverable:
                _record_recv(to_key, kind, frm, to, from_key, age_min)
            if from_key and deliverable:
                _record_sent(from_key, kind, frm, to, to_key, age_min)
        else:
            stack = pending.get((from_key, to_key)) if from_key and to_key else None
            if stack:
                inherited = stack.pop()
                if to_key and deliverable:
                    _upgrade_recv(to_key, inherited, frm, to, from_key, age_min)
            elif to_key and deliverable:
                _record_recv(to_key, "notice", frm, to, from_key, age_min)

    collect.last_diagnostics = []
    collect.last_malformed = malformed
    return {"records": [rec for _ts, rec in records], "by_session": by_session}


collect.last_diagnostics = []
collect.last_malformed = 0
