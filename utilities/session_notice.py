#!/usr/bin/env python3
"""One notice to one session about work that is not a dispatch attempt.

A paused route resuming by itself, or a remote run finishing, has nothing in
`jobs.log` to complete, yet the session that started it should hear about it
once. This writes one record into the existing pending-delivery store, addressed
the way completions are -- the session's harness carrier
(`harness_capabilities.parent_completion`) and its session id -- so each harness
delivers it with the carrier it already has, and the shared renderer shows the
same line everywhere.

A notice carries its own short text and stays deliverable until the session
consumes it; there is no registry row whose state could retire it. The same key
writes the same record once, so a retried writer never sends two notices.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import dispatch_pending_delivery as pending_delivery

KIND = "notice"
MAX_TEXT = 600
KEYS = frozenset({"schema_version", "kind", "subject", "text", "required_action", "key"})


def _registry(jobs=None) -> Path:
    if jobs:
        return Path(jobs)
    from dispatch_contract import stable_state_root
    return Path(os.environ.get("AGENT_DISPATCH_JOBS") or stable_state_root(os.environ) / "jobs.log")


def receipt_digest(receipt: dict) -> str:
    return pending_delivery._canonical_receipt_digest(receipt)


def validate(receipt: dict) -> dict:
    if (not isinstance(receipt, dict) or set(receipt) != KEYS or receipt.get("kind") != KIND
            or receipt.get("schema_version") != 1
            or any(not isinstance(receipt[key], str) or not receipt[key]
                   for key in ("subject", "text", "required_action", "key"))
            or len(receipt["text"]) > MAX_TEXT):
        raise ValueError("notice-shape-invalid")
    return dict(receipt)


def notify(harness: str, session_id: str, *, key: str, subject: str, text: str,
           required_action: str = "read-notice", route_id: str = "route-free", jobs=None) -> dict | None:
    """Queue one notice for `session_id` of `harness`; None when no carrier reaches it."""
    from harness_capabilities import parent_completion
    carrier = parent_completion(harness).get("carrier")
    if not carrier or not session_id or carrier not in pending_delivery.RECIPIENT_KINDS:
        return None
    receipt = validate({"schema_version": 1, "kind": KIND, "subject": subject,
                        "text": " ".join(text.split())[:MAX_TEXT], "required_action": required_action,
                        "key": key})
    digest = hashlib.sha256(f"{KIND}\0{key}".encode()).hexdigest()
    tag = "notice-" + digest[:32]
    return pending_delivery.create(
        _registry(jobs).parent, recipient_kind=carrier, recipient_key=session_id,
        delivery_id="delivery-" + digest, session_generation="", session_generation_supported="0",
        attempt_ids=[tag], parent_attempt_id=tag, route_id=route_id or "route-free", route_node=KIND,
        receipt=receipt, receipt_digest=receipt_digest(receipt), row_revisions={tag: KIND})


def render_text(receipt: dict) -> str:
    try:
        receipt = validate(receipt)
    except ValueError:
        return "notice=unreadable"
    return f"{receipt['subject']}: {receipt['text']} (required_action={receipt['required_action']})"


def notice_is_current(record: dict) -> bool:
    """A notice is retired only by its delivery, never by registry state."""
    try:
        validate(record.get("receipt"))
    except ValueError:
        return False
    return True


def context(receipt: dict, delivery_id: str) -> dict:
    return {"kind": KIND, "delivery_id": delivery_id, "text": render_text(receipt)}


def gateway_delivery_id(receipt: dict) -> str:
    return "delivery-" + hashlib.sha256(f"{KIND}\0{validate(receipt)['key']}".encode()).hexdigest()


def validate_pending_record(record: dict, **_kwargs) -> dict:
    receipt = validate(record.get("receipt"))
    if record.get("receipt_digest") != receipt_digest(receipt):
        raise ValueError("notice-digest-mismatch")
    return receipt
