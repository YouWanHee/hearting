#!/usr/bin/env python3
"""Bind native recipient evidence to the existing pending-delivery identity.

This module keeps receipt observation separate from receipt settlement. It
creates no durable seen state: each carrier re-reads its own native source and
uses ``ack_received`` to settle the exact record when the source proves receipt.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import dispatch_pending_delivery as pending

MARKER_RE = re.compile(r"HEARTING_RECEIPT_V1\s+(delivery-[A-Za-z0-9._-]{1,128})\s+([0-9a-f]{64})")
MAX_CLAUDE_TRANSCRIPT_BYTES = 4 * 1024 * 1024
LEGACY_COMPLETION_RE = re.compile(
    r"Runtime owner completion receipt schema=2 state=(success|attention) "
    r"attempt_id=([^\s]+) armed=([^\s]+) status=([^\s]+) "
    r"row_revision=([0-9a-f]{64}) row_digest=([0-9a-f]{64}) "
    r"marker_current=1 quiescent=[01] owned_children=\d+ advanced=[01] "
    r"reason=([^\s]+) required_action=([^\s.]+)\."
)
SOURCE_BY_HARNESS = {
    "claude": "claude-transcript",
    "codex": "codex-native-queue",
    "opencode": "opencode-session-messages",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def binding_digest(record: dict, storage_recipient: str) -> str:
    """Hash the complete immutable delivery identity, excluding lease state."""
    identity = pending.received_ack_identity(
        record, storage_recipient, record.get("delivery_id", ""))
    return hashlib.sha256(_canonical(identity)).hexdigest()


def receipt_marker(record: dict, storage_recipient: str) -> str:
    """A transport annotation outside the canonical receipt payload."""
    return (f"HEARTING_RECEIPT_V1 {record['delivery_id']} "
            f"{binding_digest(record, storage_recipient)}")


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(item.get("text", "") for item in value
                         if isinstance(item, dict) and item.get("type") in {"text", "input_text"}
                         and isinstance(item.get("text"), str))
    return ""


def _evidence(harness: str, session_id: str, source: str,
              texts: list[str]) -> dict[str, Any]:
    bindings = sorted({(match.group(1), match.group(2))
                       for text in texts for match in MARKER_RE.finditer(text)})
    return {"harness": harness, "session_id": session_id, "source": source,
            "bindings": bindings, "legacy_texts": list(texts)}


def claude_feedback_evidence(events: Any, session_id: str) -> dict[str, Any] | None:
    """Accept only this session's native user feedback transcript events."""
    if not isinstance(session_id, str) or not session_id or not isinstance(events, list):
        return None
    texts = []
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "user":
            continue
        if event.get("sessionId", event.get("session_id")) != session_id:
            continue
        if event.get("summary") != "Stop hook feedback":
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else event.get("content")
        text = _text_content(content)
        if text:
            texts.append(text)
    return _evidence("claude", session_id, "claude-transcript", texts)


def claude_transcript_evidence(*, transcript_path: str | Path, session_id: str,
                               cwd: str, expected_cwd: str,
                               runtime_home: str) -> dict[str, Any] | None:
    """Read only the verified Claude session transcript's bounded tail.

    Only actual user feedback events are eligible. A missing, foreign, partial,
    or unreadable transcript returns unknown (``None``), which leaves normal
    delivery fallback intact.
    """
    try:
        from claude_project_dir import resolve_session_transcript
        resolved, _reason = resolve_session_transcript(
            session_id, cwd, expected_cwd=expected_cwd, runtime_home=runtime_home)
        supplied = Path(transcript_path).resolve(strict=True)
        if resolved is None or supplied != resolved.resolve(strict=True):
            return None
        size = supplied.stat().st_size
        with supplied.open("rb") as handle:
            if size > MAX_CLAUDE_TRANSCRIPT_BYTES:
                handle.seek(size - MAX_CLAUDE_TRANSCRIPT_BYTES)
                handle.readline()  # discard a possibly partial first event
            raw = handle.read(MAX_CLAUDE_TRANSCRIPT_BYTES + 1)
        events = []
        for line in raw.splitlines():
            try:
                event = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(event, dict):
                events.append(event)
        return claude_feedback_evidence(events, session_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def opencode_messages_evidence(messages: Any, session_id: str) -> dict[str, Any] | None:
    """Translate exact user messages returned by this session's native SDK."""
    if not isinstance(session_id, str) or not session_id or not isinstance(messages, list):
        return None
    texts = []
    for row in messages:
        if not isinstance(row, dict):
            continue
        info = row.get("info")
        if (not isinstance(info, dict) or info.get("sessionID") != session_id
                or info.get("role") != "user"):
            continue
        parts = row.get("parts")
        text = _text_content(parts)
        if text:
            texts.append(text)
    return _evidence("opencode", session_id, "opencode-session-messages", texts)


def codex_queue_evidence(*, endpoint: str | Path, session_id: str,
                         delivery_id: str, expected_message: str,
                         timeout: float = 5.0, session: Any = None) -> dict[str, Any] | None:
    """Translate Codex's exact consumed client-message observation."""
    try:
        import codex_queue_delivery
        turn = codex_queue_delivery._known_consumed(
            Path(endpoint), session_id, delivery_id, timeout=timeout,
            strict_history=True, expected_message=expected_message, session=session)
        if turn is None:
            return _evidence("codex", session_id, "codex-native-queue", [])
        items = turn.get("items")
        texts = []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("type") == "userMessage":
                    texts.append(_text_content(item.get("content")))
        evidence = _evidence("codex", session_id, "codex-native-queue", texts)
        evidence["receiving_turn"] = {"thread_id": session_id, "turn_id": turn.get("id"),
                                      "status": turn.get("status")}
        return evidence
    except (OSError, ValueError, RuntimeError):
        return None


def codex_consumed_evidence(*, session_id: str, client_message_id: str,
                            confirmed_client_message_id: str,
                            expected_message: str) -> dict[str, Any] | None:
    """Translate the queue sender's exact ``consumed`` result.

    Callers pass the same client id and message used by the strict native
    history check that produced ``status=consumed``. Queue acceptance alone
    must never call this translator.
    """
    if (not session_id or not client_message_id
            or confirmed_client_message_id != client_message_id
            or not isinstance(expected_message, str)):
        return None
    return _evidence("codex", session_id, "codex-native-queue", [expected_message])


def _legacy_completion_match(root: Path, storage: str, session_id: str,
                             record: dict, text: str) -> bool:
    matches = list(LEGACY_COMPLETION_RE.finditer(text))
    if len(matches) != 1 or record.get("recipient_kind") != "claude-parent-runtime":
        return False
    state, attempt_id, _armed, status, revision, row_digest, _reason, action = matches[0].groups()
    if (record.get("attempt_ids") != [attempt_id]
            or (record.get("row_revisions") or {}).get(attempt_id) != revision):
        return False
    receipt = record.get("receipt")
    if not isinstance(receipt, dict):
        return False
    children = receipt.get("children")
    if not isinstance(children, list) or len(children) != 1 or not isinstance(children[0], dict):
        return False
    child = children[0]
    if (child.get("attempt_id") != attempt_id or child.get("status") != status
            or child.get("required_action") != action
            or child.get("delivery_classification") != state
            or receipt.get("delivery_classification") != state):
        return False
    jobs_value = receipt.get("job_registry")
    if not isinstance(jobs_value, str) or not jobs_value:
        return False
    try:
        from dispatch_completion_join import (
            child_row_revision, current_attempt_row, pending_record_identity,
            unseal_delivery_receipt,
        )
        from dispatch_seat_handover import effective_parent, effective_parent_harness
        jobs = Path(jobs_value).expanduser().resolve(strict=True)
        if jobs.parent != Path(root).resolve():
            return False
        row = current_attempt_row(jobs, attempt_id)
        if (row is None or row.status != status
                or row.metadata.get("parent_sid") != storage
                or row.metadata.get("parent_completion_delivery") != record.get("recipient_kind")
                or row.metadata.get("delivery_id") != record.get("delivery_id")
                or row.metadata.get("delivery_receipt_digest") != record.get("receipt_digest")
                or row.metadata.get("delivery_row_revision") != revision
                or child_row_revision(row) != revision
                or hashlib.sha256(row.raw.encode("utf-8")).hexdigest() != row_digest
                or effective_parent(row.metadata, jobs) != session_id
                or effective_parent_harness(row.metadata, jobs) != "claude"):
            return False
        encoded = row.metadata.get("delivery_receipt_b64")
        if not isinstance(encoded, str) or unseal_delivery_receipt(encoded) != receipt:
            return False
        route_id, route_node, parent_attempt_id = pending_record_identity(row.metadata, jobs)
        identity = pending.received_ack_identity(record, storage, record["delivery_id"])
        return (identity.get("route_id") == route_id
                and identity.get("route_node") == route_node
                and identity.get("parent_attempt_id") == parent_attempt_id)
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, pending.PendingDeliveryError):
        return False


def legacy_claude_receipt_bindings(root: str | Path, recipient_kind: str,
                                   session_id: str, records: list[dict],
                                   evidence: dict | None) -> dict[tuple[str, str], tuple[str, str]]:
    """Bind marker-free feedback only when one complete current record matches.

    New output always carries a marker. This read-only compatibility path is
    limited to native Stop feedback and requires unique full-record matching;
    partial attempts, stale rows and duplicate candidates remain unknown.
    """
    if (not isinstance(evidence, dict) or evidence.get("harness") != "claude"
            or evidence.get("source") != "claude-transcript"
            or evidence.get("session_id") != session_id):
        return {}
    texts = evidence.get("legacy_texts")
    if not isinstance(texts, list):
        return {}
    candidates = []
    for record in records:
        storage = record.get("_storage_key") or session_id
        if (record.get("recipient_kind") != recipient_kind
                or not isinstance(record.get("delivery_id"), str)):
            continue
        candidates.append((storage, record))
    result: dict[tuple[str, str], tuple[str, str]] = {}
    for text in texts:
        matches = []
        for storage, record in candidates:
            if recipient_kind != "claude-parent-runtime":
                continue
            if _legacy_completion_match(Path(root), storage, session_id, record, text):
                matches.append((storage, record))
                continue
            receipt = record.get("receipt")
            if not isinstance(receipt, dict) or receipt.get("kind") not in {
                    "supervision", "human-gate", "notice"}:
                continue
            try:
                from dispatch_session_sweep import _bounded_receipt_text
                rendered = {_bounded_receipt_text(record)}
                if receipt.get("kind") == "supervision":
                    from dispatch_supervision import CONTINUED_KEY, continue_for_parent
                    try:
                        continued = continue_for_parent(
                            record, session_id=session_id, recipient_kind=recipient_kind)
                    except (OSError, RuntimeError, TypeError, ValueError):
                        continued = None
                    if continued:
                        rendered.add(_bounded_receipt_text({**record, CONTINUED_KEY: continued}))
                if any(value and value in text for value in rendered):
                    matches.append((storage, record))
            except (KeyError, TypeError, ValueError, RuntimeError):
                continue
        unique = {(storage, record["delivery_id"]): (storage, record)
                  for storage, record in matches}
        if len(unique) != 1:
            continue
        (storage, delivery_id), (_storage, record) = next(iter(unique.items()))
        try:
            result[(storage, delivery_id)] = (
                delivery_id, binding_digest(record, storage))
        except (KeyError, TypeError, ValueError, pending.PendingDeliveryError):
            continue
    return result


def settle_native_receipt(root: str | Path, storage_recipient: str,
                          receiving_harness: str, receiving_session: str,
                          record: dict, evidence: dict | None, *,
                          acked_by: str) -> dict[str, Any]:
    """Return receipt observation and settlement independently.

    A write failure after exact receipt verification returns ``received=True``
    and ``settled=False`` so another carrier suppresses its send and a later
    natural pass can retry only the CAS. Identity changes remain unrecognized.
    """
    result = {"received": False, "settled": False, "reason": "native-receipt-absent"}
    delivery_id = record.get("delivery_id") if isinstance(record, dict) else None
    if not isinstance(delivery_id, str):
        result["reason"] = "delivery-identity-invalid"
        return result
    try:
        identity = pending.received_ack_identity(record, storage_recipient, delivery_id)
        if record.get("claim_authority") not in pending.CLAIM_AUTHORITIES:
            result["reason"] = "delivery-was-never-claimed"
            return result
        expected_source = SOURCE_BY_HARNESS.get(receiving_harness)
        if (not isinstance(evidence, dict) or not expected_source
                or evidence.get("harness") != receiving_harness
                or evidence.get("session_id") != receiving_session
                or evidence.get("source") != expected_source):
            result["reason"] = "native-evidence-unknown"
            return result
        bindings = evidence.get("bindings")
        expected_binding = (delivery_id, hashlib.sha256(_canonical(identity)).hexdigest())
        if not isinstance(bindings, (list, tuple, set)) or expected_binding not in bindings:
            return result
        current = pending.read(Path(root), storage_recipient, delivery_id)
        if current is None or pending.received_ack_identity(
                current, storage_recipient, delivery_id) != identity:
            result["reason"] = "delivery-identity-changed"
            return result
        result.update(received=True, reason="native-receipt-verified")
        try:
            settled = pending.ack_received(
                Path(root), storage_recipient, delivery_id,
                expected_identity=identity, acked_by=acked_by)
            result["settled"] = settled.get("state") == "acked"
            result["reason"] = ("native-receipt-settled" if result["settled"]
                                else "native-receipt-terminal")
        except OSError:
            result["reason"] = "native-receipt-ack-write-failed"
        except pending.PendingDeliveryError as exc:
            # A concurrent identity replacement is stale evidence; otherwise
            # the exact receipt remains valid even if the CAS needs recovery.
            latest = pending.read(Path(root), storage_recipient, delivery_id)
            if (latest is None or pending.received_ack_identity(
                    latest, storage_recipient, delivery_id) != identity):
                result.update(received=False, reason="delivery-identity-changed")
            else:
                result["reason"] = str(exc)
        return result
    except (OSError, ValueError, TypeError, pending.PendingDeliveryError):
        result["reason"] = "native-receipt-identity-unavailable"
        return result
