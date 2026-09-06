"""F-100c — steward (depth −1) flag projection, read-only.

The ledger tool (`utilities/peer-message.py`) keeps one marker per session under
`<dispatch-state-root>/peer-steward/<harness>/<sid>.json`. Since 2026-09-06 the flag
is a ROLE: a marker entry is evidence only when its `source` is `explicit`
(`peer-steward.py steward on`), `watch` (a SENT `watch` record) or `start`
(`peer-steward.py start` launched the target). Sending a steer/handoff/gate-relay
raises nothing — under the old rule every worker that handed off to its steward
wore the steward tag. This collector joins markers onto live sessions by exact
(harness, session_id) and asks the ledger tool's `steward_evidence_targets` — the
one definition of the rule — which entries count; a marker with none (an old
handoff-only leftover) is treated as absent, so `steward_targets` holds evidence
entries only. Nothing here writes, and a missing or unreadable marker root (or an
unavailable ledger module) leaves every session's default (`steward=False`).
"""
import importlib.util
import sys
from pathlib import Path


def _peer_message_module():
    here = Path(__file__).resolve()
    for candidate in here.parents:
        tool = candidate / "utilities" / "peer-message.py"
        if tool.is_file():
            try:
                spec = importlib.util.spec_from_file_location("_peer_message_ro", str(tool))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
            except Exception:
                return None
    return None


def read_markers():
    """``{(harness, session_id): marker}`` over every ledger root the board reads (the
    F-98d resolver chain plus each installed runtime's own root); empty on any failure."""
    mod = _peer_message_module()
    if mod is None:
        return {}
    try:
        from . import peer_messages as _pm
        roots = _pm._state_roots()
    except Exception:
        roots = None
    try:
        return mod.read_steward_markers(roots or None) or {}
    except Exception:
        return {}


def enrich(sessions, markers=None):
    if markers is None:
        markers = read_markers()
    if not markers:
        return
    mod = _peer_message_module()
    evidence = getattr(mod, "steward_evidence_targets", None) if mod is not None else None
    if evidence is None:
        return  # no rule available → no steward claims (fail-soft, never a guess)
    for s in sessions:
        sid = getattr(s, "session_id", None)
        harness = str(getattr(s, "harness", "") or "").lower()
        marker = markers.get((harness, sid)) if sid else None
        if not marker:
            continue
        try:
            targets = evidence(marker)
        except Exception:
            targets = []
        if not targets:
            continue
        s.steward = True
        # `steward_evidence_targets` returns oldest-first — the stable order the
        # renderer's front-preserving +N fold relies on.
        s.steward_targets = list(targets)
