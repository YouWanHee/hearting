"""Campaign satisfaction: verified native acceptance -> immutable event -> projection.

Cycle/route success is not inferred from campaign satisfaction. Native session
stores are the existing local trust boundary, not cryptographic proof against
a process allowed to forge those stores. No caller-supplied user actor is used.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

import artifact_admission as admission
import artifact_identity as identity
import artifact_index as index_module
import artifact_lifecycle as lifecycle
import artifact_locator as locator
import artifact_manifest as manifest

CONTRACT = "artifact-campaign-closure/v1"
EVENT_NAME = "campaign.satisfied.json"
MAX_JSON = 16 * 1024 * 1024


class CampaignError(Exception):
    def __init__(self, code, detail=""):
        self.code, self.detail = code, str(detail)
        super().__init__(code + (": " + self.detail if self.detail else ""))


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def _safe(root, path):
    root, path = Path(root).absolute(), Path(path).absolute()
    try:
        parts = path.relative_to(root).parts
    except ValueError as exc:
        raise CampaignError("campaign-path-outside-root", path) from exc
    if ".." in parts:
        raise CampaignError("campaign-path-outside-root", path)
    current = root
    for part in ("", *parts):
        current = current / part
        if current.is_symlink():
            raise CampaignError("campaign-symlink", current)
    return path


def read_json(root, path):
    path = _safe(root, path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JSON:
                raise CampaignError("campaign-input-kind-or-size", path)
            raw = stream.read(MAX_JSON + 1)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("object required")
        return value, raw
    except (OSError, ValueError, UnicodeError) as exc:
        raise CampaignError("campaign-input-invalid", path) from exc


def campaign_path(root, selection):
    root = Path(root).resolve()
    if identity.is_well_formed(str(selection), "campaign"):
        directory = locator.find_path_by_id(root, str(selection))
        if directory is None:
            raise CampaignError("campaign-unknown", selection)
        path = directory / "campaign.json"
    else:
        path = Path(selection)
        if not path.is_absolute():
            path = root / path
        if path.name != "campaign.json":
            path = path / "campaign.json"
    _safe(root / "campaigns", path)
    if path.parent.parent != root / "campaigns":
        raise CampaignError("campaign-path-invalid", path)
    return path


def _event_path(path):
    return path.parent / EVENT_NAME


def _load_event(root, path):
    event_path = _event_path(path)
    if not event_path.exists() and not event_path.is_symlink():
        return None
    event, raw = read_json(root, event_path)
    violations = []
    manifest._v_event_row(event, "$", violations)
    if violations:
        raise CampaignError("campaign-event-invalid", violations[:3])
    payload = event.get("payload", {})
    if not isinstance(payload, dict):
        raise CampaignError("campaign-event-invalid", event_path)
    snapshot = payload.get("snapshot", {})
    approved = payload.get("approval", {})
    if not all(isinstance(value, dict) for value in (snapshot, approved)) or not isinstance(snapshot.get("campaign"), dict):
        raise CampaignError("campaign-event-invalid", event_path)
    campaign = snapshot.get("campaign", {})
    expected = approval_text(campaign.get("campaign_id", ""), digest(snapshot))
    actor_id = "native-user:" + hashlib.sha256(
        (str(approved.get("harness")) + ":" + str(approved.get("session_id"))).encode()).hexdigest()
    if (raw != canonical(event) + b"\n" or event.get("event_type") != "campaign.satisfied"
            or payload.get("contract") != CONTRACT or event.get("target_id") != campaign.get("campaign_id")
            or campaign.get("state") != "active" or approved.get("statement") != expected
            or approved.get("decision") != "accepted"
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(approved.get("native_message_digest")))
            or approved.get("harness") not in {"claude", "codex", "opencode"}
            or approved.get("actor_id") != actor_id
            or event.get("actor") != {"kind": "user", "id": approved.get("actor_id")}
            or event.get("stream_sequence") != 1
            or event.get("event_id") != _id("evt", {k: v for k, v in event.items() if k != "event_id"})
            or payload.get("root") != str(Path(root).resolve())):
        raise CampaignError("campaign-event-invalid", event_path)
    return event


def _id(prefix, value):
    return prefix + "_" + hashlib.sha256(canonical(value)).hexdigest()[:32]


def _projection(event):
    value = dict(event["payload"]["snapshot"]["campaign"])
    value.update(state="satisfied", satisfied_on=event["recorded_at"],
                 satisfaction_event_id=event["event_id"])
    return value


def fold_campaign(root, path, campaign):
    """Pure read: a committed event fences begin even before cache recovery."""
    event = _load_event(root, path)
    if event is None:
        return campaign
    before = event["payload"]["snapshot"]["campaign"]
    after = _projection(event)
    if campaign not in (before, after):
        raise CampaignError("campaign-projection-conflict", path)
    return after


def check_campaign_write(root, path, proposed):
    event = _load_event(root, path)
    if event is not None:
        current, _ = read_json(root, path)
        fold_campaign(root, path, current)
        if proposed != _projection(event):
            raise CampaignError("campaign-terminal-write-conflict", path)


def _cycle_rows(root, path, campaign):
    ids = campaign.get("cycles")
    if (not isinstance(ids, list) or not ids or not all(isinstance(cid, str) for cid in ids)
            or len(set(ids)) != len(ids)):
        raise CampaignError("campaign-membership-invalid")
    records = root / ".runtime/artifact-producer/v1/cycles"
    root_id = lifecycle.read_root_identity(root)
    if root_id is None:
        raise CampaignError("root-identity-missing")
    index = admission.load_index(root)
    if index.artifact_root_id != root_id.artifact_root_id:
        raise CampaignError("campaign-index-root-mismatch")
    # A campaign list alone cannot hide an open member or an unregistered tree.
    members = set()
    for entry in records.glob("*.json"):
        record, _ = read_json(root, entry)
        if record.get("campaign_id") == campaign["campaign_id"]:
            members.add(record.get("cycle_id"))
    if members != set(ids):
        raise CampaignError("campaign-membership-drift")
    directories = {}
    for entry, layout in locator.iter_cycle_dirs(path.parent):
        _safe(path.parent, entry)
        if (entry / ".cycle.json").exists():
            binding, _ = read_json(root, entry / ".cycle.json")
        elif (entry / "manifest.json").exists():
            # Historical sealed cycles predate .cycle.json. Their immutable
            # manifest plus producer record and index still prove the binding.
            document, _ = read_json(root, entry / "manifest.json")
            binding = document.get("cycle", {})
        elif layout == "legacy-id" and entry.name in ids:
            binding = {"cycle_id": entry.name, "campaign_id": campaign["campaign_id"]}
        else:
            # Undeclared material is not silently made a cycle or a new
            # residual-zero requirement. Declared members still must resolve.
            continue
        if not isinstance(binding, dict) or binding.get("campaign_id") != campaign["campaign_id"]:
            raise CampaignError("campaign-cycle-binding-mismatch", entry)
        cid = binding.get("cycle_id")
        if not isinstance(cid, str) or cid in directories:
            raise CampaignError("campaign-cycle-binding-duplicate", entry)
        directories[cid] = entry
    if set(directories) != set(ids):
        raise CampaignError("campaign-cycle-bindings-incomplete")
    rows = []
    for cid in sorted(ids):
        if not identity.is_well_formed(cid, "cycle"):
            raise CampaignError("campaign-cycle-id-invalid", cid)
        record, _ = read_json(root, records / (cid + ".json"))
        if record.get("state") not in {"sealed", "superseded"} or not record.get("sealed_on"):
            raise CampaignError("campaign-cycle-not-sealed", cid)
        directory = directories[cid]
        _safe(path.parent, directory)
        expected_directory = (path.parent / str(record["locator"]) if record.get("locator")
                              else path.parent / "cycles" / cid)
        if directory != expected_directory:
            raise CampaignError("campaign-cycle-locator-invalid", cid)
        document, raw = read_json(root, directory / "manifest.json")
        report = manifest.validate(document)
        if not report.ok:
            raise CampaignError("campaign-manifest-invalid", cid + ": " + str(report.violations[:3]))
        # The stored/indexed seal binds bytes. Legacy approved merges retained
        # noncanonical JSON formatting; never rewrite them to today's encoder.
        mdigest = "sha256:" + hashlib.sha256(raw).hexdigest()
        cycle = document["cycle"]
        if (record.get("manifest_digest") != mdigest
                or cycle.get("cycle_id") != cid or cycle.get("campaign_id") != campaign["campaign_id"]
                or document["campaign"]["campaign_id"] != campaign["campaign_id"]
                or document["artifact_root_id"] != root_id.artifact_root_id
                or document["producer"]["producer_id"] != record.get("producer_id")
                or cycle["state"] not in {"completed", "abandoned"}
                or cycle["state"] != record.get("cycle_state")):
            raise CampaignError("campaign-seal-mismatch", cid)
        expected = index_module.apply(index_module.empty(root_id.artifact_root_id), document,
                                      cycle_path=str(directory.relative_to(root)),
                                      manifest_digest=manifest.manifest_digest(document), idempotency_key=cid)
        if (index.manifests.get(cid) != expected.manifests[cid]
                or index.cycles.get(cid) != expected.cycles[cid]):
            raise CampaignError("campaign-index-mismatch", cid)
        for revision in document["artifact_revisions"]:
            _safe(directory, directory / revision["locator"]["path"])
        failures = lifecycle.verify_artifact_revisions(document, directory)
        if failures:
            raise CampaignError("campaign-artifact-mismatch", cid + ": " + ";".join(failures[:5]))
        rows.append({"cycle_id": cid, "state": cycle["state"], "manifest_digest": mdigest,
                     "index_digest": manifest.manifest_digest(document),
                     "route_id": record["route_id"],
                     "manifest_id": document["manifest_id"],
                     "manifest_revision_id": document["manifest_revision_id"]})
    return root_id, rows


def approval_text(campaign_id, snapshot_digest):
    return f"campaign-satisfy {campaign_id} {snapshot_digest}"


def _snapshot(root, path):
    campaign, _ = read_json(root, path)
    effective = fold_campaign(root, path, campaign)
    if effective.get("state") != "active":
        raise CampaignError("campaign-not-active", effective.get("state"))
    criterion = campaign.get("completion_criterion", {}).get("statement")
    if (not isinstance(criterion, str) or not criterion.strip()
            or not isinstance(campaign.get("goal"), str) or not campaign["goal"].strip()
            or not identity.is_well_formed(campaign.get("campaign_id"), "campaign")):
        raise CampaignError("campaign-criterion-missing")
    root_id, rows = _cycle_rows(root, path, campaign)
    return {"artifact_root_id": root_id.artifact_root_id, "root": str(root),
            "campaign": campaign, "cycles": rows}


def status(root, selection):
    root = Path(root).resolve()
    path = campaign_path(root, selection)
    raw, _ = read_json(root, path)
    effective = fold_campaign(root, path, raw)
    event = _load_event(root, path)
    if event:
        return {"status": "satisfied", "campaign_id": effective["campaign_id"],
                "projection_pending": raw != effective, "event_id": event["event_id"],
                "recovery_command": ["python3", str(Path(__file__).with_name("artifact_producer.py")),
                                     "campaign-recover", "--artifact-root", str(root), "--campaign", str(path)]}
    snapshot = _snapshot(root, path)
    return {"status": "awaiting-user-acceptance", "campaign_id": raw["campaign_id"],
            "goal": raw["goal"], "completion_criterion": raw["completion_criterion"],
            "cycles": snapshot["cycles"], "snapshot_digest": digest(snapshot),
            "approval_statement": approval_text(raw["campaign_id"], digest(snapshot)),
            "instruction": "After reviewing the goal, criterion and cycle outcomes, the USER may send this exact statement in their native session. The agent must not submit it."}


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(isinstance(p, dict) and p.get("type") in {"text", "input_text"}
                                         and isinstance(p.get("text"), str) for p in content):
        return "\n".join(p.get("text", "") for p in content)
    return ""


def _native_messages(harness, session):
    """Read the native store, never a caller-created export or approval file."""
    if harness == "opencode":
        if not re.fullmatch(r"ses_[A-Za-z0-9]+", session):
            raise CampaignError("approval-session-invalid")
        with tempfile.TemporaryFile() as output:
            try:
                result = subprocess.run(["opencode", "export", session], stdout=output,
                                        stderr=subprocess.DEVNULL, timeout=30, check=False)
                if result.returncode or output.tell() > MAX_JSON:
                    raise CampaignError("approval-native-export-unavailable")
                output.seek(0); data = json.load(output)
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                raise CampaignError("approval-native-export-unavailable") from exc
        if not isinstance(data, dict) or not isinstance(data.get("info"), dict) or data["info"].get("id") != session:
            raise CampaignError("approval-session-mismatch")
        for row in data.get("messages", []):
            if not isinstance(row, dict) or not isinstance(row.get("info"), dict):
                raise CampaignError("approval-native-record-invalid")
            info = row.get("info", {})
            if info.get("sessionID") == session and info.get("role") == "user":
                parts = row.get("parts", [])
                if not isinstance(parts, list) or any(not isinstance(p, dict) or p.get("synthetic") or p.get("ignored") for p in parts):
                    continue
                yield _text(parts), {"source": "opencode-export", "message_id": info.get("id"),
                                    "native_message_digest": digest(row)}
        return
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", session):
        raise CampaignError("approval-session-invalid")
    home = Path.home()
    if harness == "codex":
        base = Path(os.environ.get("CODEX_HOME", home / ".codex")) / "sessions"
        candidates = list(base.glob(f"*/*/*/rollout-*-{session}.jsonl"))
    elif harness == "claude":
        base = Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude")) / "projects"
        candidates = list(base.glob(f"*/{session}.jsonl"))
    else:
        raise CampaignError("approval-harness-unsupported", harness)
    if len(candidates) != 1:
        raise CampaignError("approval-native-session-unavailable", session)
    path = _safe(base, candidates[0])
    witnessed = harness == "claude"
    with path.open("rb") as stream:
        for number, line in enumerate(stream, 1):
            if len(line) > MAX_JSON:
                raise CampaignError("approval-native-record-oversized")
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise CampaignError("approval-native-record-invalid", number) from exc
            if not isinstance(row, dict):
                raise CampaignError("approval-native-record-invalid", number)
            if harness == "codex":
                payload = row.get("payload", {})
                if not isinstance(payload, dict):
                    continue
                if row.get("type") == "session_meta":
                    witnessed = payload.get("id") == session
                if not witnessed or row.get("type") != "response_item" or payload.get("type") != "message" or payload.get("role") != "user":
                    continue
                text = _text(payload.get("content"))
            else:
                message = row.get("message", {})
                if (not isinstance(message, dict) or row.get("type") != "user" or row.get("sessionId") != session
                        or row.get("isMeta") or row.get("isSidechain") or message.get("role") != "user"):
                    continue
                text = _text(message.get("content"))
            yield text, {"source": str(path), "line": number,
                         "native_message_digest": "sha256:" + hashlib.sha256(line).hexdigest()}


def verify_approval(harness, session, statement):
    matches = []
    rejection = statement.replace("campaign-satisfy ", "campaign-reject ", 1)
    for text, evidence in _native_messages(harness, session):
        if text.strip() in (statement, rejection):
            matches.append((text.strip(), evidence))
    if not matches or matches[-1][0] != statement:
        raise CampaignError("campaign-user-acceptance-required", statement)
    evidence = matches[-1][1]
    return {**evidence, "harness": harness, "session_id": session,
            "actor_id": "native-user:" + hashlib.sha256((harness + ":" + session).encode()).hexdigest(),
            "statement": statement, "decision": "accepted"}


def _publish_event(root, path, event):
    """No-replace atomic publication; interrupted staging is never authority."""
    target = _event_path(path)
    _safe(root, target)
    target.parent.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".campaign-close-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(event) + b"\n"); stream.flush(); os.fsync(stream.fileno())
        os.link(tmp, target, follow_symlinks=False)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(tmp)


def _materialize(root, path, event):
    import artifact_producer as producer
    current, _ = read_json(root, path)
    projected = fold_campaign(root, path, current)
    if projected != _projection(event):
        raise CampaignError("campaign-projection-conflict")
    if current != projected:
        producer._write_campaign(root, projected, exclusive=False)
    return {"status": "satisfied", "campaign_id": event["target_id"],
            "event_id": event["event_id"], "projection_pending": False}


def close(root, selection, *, harness=None, session=None, recover=False):
    root = Path(root).resolve()
    path = campaign_path(root, selection)
    # Validate before creating lock/staging or mutating any campaign state.
    event = _load_event(root, path)
    if event is None:
        if recover:
            raise CampaignError("campaign-no-committed-close")
        snapshot = _snapshot(root, path)
        statement = approval_text(snapshot["campaign"]["campaign_id"], digest(snapshot))
        if not harness or not session:
            raise CampaignError("campaign-user-acceptance-required", statement)
        approval = verify_approval(harness, session, statement)
    lock = admission._acquire_lock(root, admission.LOCK_TIMEOUT_DEFAULT)
    try:
        committed = _load_event(root, path)
        if committed:
            return _materialize(root, path, committed)
        if event is not None:
            raise CampaignError("campaign-committed-event-disappeared")
        latest = _snapshot(root, path)
        if latest != snapshot:
            raise CampaignError("campaign-approval-snapshot-changed")
        # A rejection recorded while acquiring the lock must not be ignored.
        approval = verify_approval(harness, session, statement)
        first = snapshot["cycles"][0]
        when = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        event = {"stream_id": _id("strm", {"root": str(root), "campaign": snapshot["campaign"]["campaign_id"]}),
                 "stream_sequence": 1, "event_type": "campaign.satisfied",
                 "target_id": snapshot["campaign"]["campaign_id"],
                 "actor": {"kind": "user", "id": approval["actor_id"]}, "recorded_at": when,
                 "provenance": {"source_manifest_id": first["manifest_id"],
                                "source_revision_id": first["manifest_revision_id"],
                                "producer_route_id": first["route_id"], "schema_version": 1,
                                "algorithm_version": CONTRACT, "source_digest": digest(snapshot)},
                 "evidence_ids": [], "payload": {"contract": CONTRACT, "root": str(root),
                                                   "snapshot": snapshot, "approval": approval}}
        event["event_id"] = _id("evt", event)
        violations = []
        manifest._v_event_row(event, "$", violations)
        if violations:
            raise CampaignError("campaign-event-invalid", violations[:3])
        try:
            _publish_event(root, path, event)
            return _materialize(root, path, event)
        except OSError as exc:
            if _load_event(root, path) is not None:
                recovery = ["python3", str(Path(__file__).with_name("artifact_producer.py")),
                            "campaign-recover", "--artifact-root", str(root), "--campaign", str(path)]
                raise CampaignError("campaign-close-committed-recovery-required",
                                    json.dumps({"recovery_command": recovery, "error": str(exc)})) from exc
            raise CampaignError("campaign-close-not-committed", str(exc)) from exc
    finally:
        admission._release_lock(root, lock)
