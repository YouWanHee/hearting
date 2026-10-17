"""Read-only projection of unanswered human-gate decisions for Fleet."""
from __future__ import annotations

import hashlib
import json
import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "utilities") not in sys.path:
    sys.path.insert(0, str(ROOT / "utilities"))

import frame_interview as FI
import workflow_state as WS

_CACHE_LIMIT = 256
_JOURNAL_CACHE = OrderedDict()
_JSON_CACHE = OrderedDict()
_OBSERVATION_CACHE = OrderedDict()


def _stamp(path):
    """Stable identity for an existing or missing input; never follows symlinks."""
    try:
        meta = Path(path).lstat()
        return (meta.st_dev, meta.st_ino, meta.st_mtime_ns, meta.st_ctime_ns, meta.st_size)
    except OSError:
        return None


def _cached(cache, key, loader):
    if key in cache:
        cache.move_to_end(key)
        return cache[key]
    value = loader()
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > _CACHE_LIMIT:
        cache.popitem(last=False)
    return value


def _stable_read(path, cache):
    before = _stamp(path)
    if before is None:
        return None
    key = (str(path), before)
    if key in cache:
        cache.move_to_end(key)
        return cache[key]
    try:
        value = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    after = _stamp(path)
    if before != after:
        return None
    return _cached(cache, key, lambda: value)


def _journal(ledger):
    stamp = _stamp(ledger.journal_path)
    if stamp is None:
        return []
    key = (str(ledger.journal_path), stamp)
    def parse():
        raw = _stable_read(ledger.journal_path, _JOURNAL_CACHE)
        if raw is None:
            return []
        entries = []
        for line in raw.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                entries.append(item)
        return entries
    return _cached(_OBSERVATION_CACHE, ("journal", key), parse)


def _json_file(path):
    stamp = _stamp(path)
    if stamp is None:
        return None
    key = ("json", str(path), stamp)
    def parse():
        raw = _stable_read(path, _JSON_CACHE)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return _cached(_OBSERVATION_CACHE, key, parse)

def _inside(path, root):
    try:
        resolved, base = Path(path).resolve(strict=True), Path(root).resolve(strict=True)
        return resolved.is_relative_to(base) and not Path(path).is_symlink()
    except (OSError, ValueError):
        return False


def _valid_frame_answer(interview_path, route, answer_path):
    try:
        interview = _json_file(interview_path)
        answers = _json_file(answer_path)
        if not FI.is_interview(interview):
            return False
        if interview.get("route_id") != route.get("route_id"):
            return False
        if FI.pending_answer_response(interview, answers) or FI.validate_answers(interview, answers):
            return False
        actor = FI.answer_actor_kind(answers)
        return FI.answer_releases_gate(actor, "frame-review")
    except (OSError, ValueError, TypeError):
        return False


def pending_human_waits(route, *, jobs, outcome=None):
    """Return unresolved raised gates as private route/gate ids and public-safe fields.

    This reads only the exact route's registry-selected workflow journal and, for
    frame-review, answer files beside the artifact named by the latest raise.
    It never repairs ledger state or changes gate state.
    """
    if not isinstance(route, dict) or not route.get("route_id") or not route.get("route_hash"):
        return []
    try:
        from route_identity import route_hash
        if route_hash(route) != route["route_hash"]:
            return []
        ledger_root, _ = WS.ledger_root_for(jobs)
        ledger = WS.WorkflowLedger(route["route_id"], route["route_hash"], root=ledger_root)
        journal = [row for row in _journal(ledger)
                   if row.get("route_id") == route["route_id"]
                   and row.get("route_hash") == route["route_hash"]]
    except (OSError, ValueError, TypeError, KeyError):
        return []
    terminal = any(row.get("workflow_state") in {"CANCELLED", "COMPLETE", "FAILED_TERMINAL"}
                   for row in journal)
    if outcome and outcome.get("closed_at") and outcome.get("match") is True \
            and not outcome.get("finish_pending"):
        terminal = True
    if terminal:
        return []
    latest_lifecycle = next((row.get("workflow_state") for row in reversed(journal)
                            if row.get("workflow_state")), None)
    if latest_lifecycle in {"CANCELLED", "COMPLETE", "FAILED_TERMINAL"}:
        return []
    gates = []
    raised = []
    for row in journal:
        evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
        gate = evidence.get("gate")
        if row.get("workflow_state") == "BLOCKED_HUMAN_GATE" and isinstance(gate, str):
            raised.append(gate)
    for gate in dict.fromkeys(raised):
        resolution = WS.human_gate_resolution(journal, gate)
        if resolution.get("status") != "blocked":
            continue
        raised_at = resolution.get("raised_at")
        try:
            parsed = datetime.fromisoformat(str(raised_at).replace("Z", "+00:00"))
            waiting_since = parsed.timestamp()
        except (ValueError, TypeError, OverflowError):
            continue
        if gate == "frame-review":
            artifact = resolution.get("artifact")
            artifact_root = route.get("artifact_root")
            if artifact and artifact_root and _inside(artifact, artifact_root):
                try:
                    question_path = Path(artifact).resolve(strict=True)
                    digest = resolution.get("artifact_sha256")
                    current = not digest or hashlib.sha256(question_path.read_bytes()).hexdigest() == digest
                    if current and FI.is_interview(_json_file(question_path)):
                        if (_valid_frame_answer(question_path, route, question_path.parent / "answers.native.json")
                                or _valid_frame_answer(question_path, route, question_path.parent / "answers.json")):
                            continue
                except (OSError, ValueError, TypeError):
                    # Invalid/missing answer evidence never clears a wait.
                    pass
        gates.append({"gate_id": gate, "kind": "decision", "source": "hearting-ledger",
                      "waiting_since": waiting_since, "route_id": route["route_id"]})
    return gates
