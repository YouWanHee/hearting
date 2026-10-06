#!/usr/bin/env python3
"""Route authority: the one place that answers four questions about a route.

1. Who continues it (the parent session and its successor).
2. On which harness (sealed selection pins).
3. How many attempts of which kind (sub-session standing, retry links,
   round budget, the result envelope).
4. What it may access (execution access grant).

Every judgment here used to be made at its call site, several of them in
more than one copy. The old names stay where they were, as imports or thin
wrappers, so existing callers and patches keep working. Copies that
historically disagree keep distinct names here instead of being merged, so
one judgment changes in one place.

Top-level imports stay light (stdlib and two policy modules) so that
`dispatch_contract`, `model_profile` and the adapters can import this module
without a cycle; heavier collaborators are imported where they are used.
"""
from __future__ import annotations

import os
from pathlib import Path
import re

from dispatch_attempt_policy import committed_outcome
import review_round_cap as _ROUND


def _contract_error(reason, detail=""):
    from dispatch_contract import DispatchContractError
    return DispatchContractError(reason, detail)


# ---------------------------------------------------------------------------
# 1. Who continues the route
# ---------------------------------------------------------------------------

def caller_identity(environ=None) -> tuple[str, str]:
    """Resolve the caller's native identity, independently of the child adapter."""
    env = os.environ if environ is None else environ
    sessions = {
        "codex": env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID") or "",
        "claude": env.get("CLAUDE_CODE_SESSION_ID") or env.get("CLAUDE_SESSION_ID") or "",
        "opencode": env.get("OPENCODE_SESSION_ID") or "",
    }
    explicit = env.get("AGENT_DISPATCH_CALLER_HARNESS") or env.get("AGENT_DISPATCH_CURRENT_HARNESS")
    if explicit:
        if explicit not in sessions:
            raise _contract_error("caller-harness-invalid")
        return explicit, sessions[explicit]
    detected = [(harness, session) for harness, session in sessions.items() if session]
    if len(detected) > 1:
        raise _contract_error("caller-harness-ambiguous")
    return detected[0] if detected else ("", "")


def default_parent_session_id(environ=None) -> str | None:
    env = os.environ if environ is None else environ
    # A directly running interactive host is the authority for its own TUI
    # thread. Managed entry used to export a second parent id after observing
    # an App Server sibling, which could override the real Codex thread and
    # strand every completion. Preserve the explicit binding only for nested
    # workers, whose environment marks that dispatch boundary.
    if env.get("AGENT_DISPATCH_CHILD") != "1":
        native_session = caller_identity(env)[1]
        if native_session:
            return native_session
    return env.get("AGENT_DISPATCH_PARENT_SESSION_ID") or caller_identity(env)[1] or None


def default_parent_harness(fallback: str, environ=None) -> str:
    """A selected child's runtime never replaces its caller's identity."""
    env = os.environ if environ is None else environ
    return caller_identity(env)[0] or env.get("AGENT_DISPATCH_OWNER_HARNESS") or fallback


def bind_runtime_parent(args, *, honor_force: bool = False, environ=None) -> None:
    """Bind a dispatch-depth-1 job to the actual calling Codex or Claude session.

    Callers historically supplied a synthetic ``--parent-session-id``; the
    running session overrides it. Dispatch-depth-2 workers keep their explicit
    conductor/owner envelope. Only the Codex wrapper honors the legacy
    ``CODEX_DISPATCH_PARENT_CURRENT_FORCE`` switch (``honor_force``).
    """
    env = os.environ if environ is None else environ
    force_current = honor_force and env.get("CODEX_DISPATCH_PARENT_CURRENT_FORCE") == "1"
    current_thread = env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID")
    claude_session = env.get("CLAUDE_CODE_SESSION_ID")
    caller_harness = (
        env.get("AGENT_DISPATCH_CALLER_HARNESS")
        or ("codex" if current_thread and not claude_session else None)
        or ("claude" if claude_session and not current_thread else None)
    )
    if args.dispatch_depth == 1:
        if current_thread and caller_harness == "codex":
            args.parent_session_id = current_thread
            args.parent_harness = "codex"
            args.parent_slug = None
        elif claude_session and caller_harness == "claude":
            args.parent_session_id = claude_session
            args.parent_harness = "claude"
            args.parent_slug = None
        elif force_current:
            args.parent_slug = None
    elif force_current and current_thread:
        args.parent_session_id = current_thread


def correction_source_session(environ=None) -> str:
    """The session recorded as the sender of an owner correction (a label, not a check)."""
    env = os.environ if environ is None else environ
    return (env.get("CODEX_THREAD_ID") or env.get("CLAUDE_SESSION_ID")
            or env.get("OPENCODE_SESSION_ID") or "operator")


def owns(meta, session, jobs) -> bool:
    """The launching session, or its confirmed same-seat successor after a /clear (seat handover)."""
    if session and meta.get("parent_sid") == session:
        return True
    if not session or jobs is None:
        return False
    from dispatch_seat_handover import owns as seat_owns
    return seat_owns(meta, session, jobs)


def require_replacement_parent(jobs, rows, meta, *, current_session) -> None:
    """Only the attempt's parent may start its replacement.

    Dispatch depth 2: the caller's own attempt is the registered, live parent
    attempt. Otherwise: the registered parent session or its same-seat
    successor. ``current_session`` is called only on that second path.
    """
    if meta.get('dispatch_depth') == '2':
        import dispatch_contract as DC
        parent = meta.get('parent_attempt_id')
        if not parent or os.environ.get('AGENT_DISPATCH_ATTEMPT_ID') != parent or parent not in rows:
            raise DC.DispatchContractError('replacement-parent-identity-unproven')
        fields, parent_meta = rows[parent]
        if fields[1] not in {'open', 'running'} or not DC._parent_liveness_evidence(Path(jobs), parent_meta)[0]:
            raise DC.DispatchContractError('replacement-parent-not-live')
    elif not meta.get('parent_sid') or not owns(meta, current_session(), jobs):
        raise _contract_error('replacement-parent-identity-unproven')


def lineage_parent_matches(metadata, *, thread_id, parent_attempt_id) -> bool:
    """A replacement lineage row belongs to this receipt's parent: the exact
    registered session at depth 1, the exact parent attempt below it."""
    if metadata.get("dispatch_depth") == "1":
        return metadata.get("parent_sid") == thread_id
    return metadata.get("parent_attempt_id") == parent_attempt_id


LIVE_ROW_STATUSES = frozenset({"open", "running"})


def review_owner_authority(route, jobs, author_attempt_id) -> None:
    """Prove the current registered owner without taking or creating locks."""
    from dispatch_contract import parse_registry_metadata
    from owner_route_binding import resolve_owner_route_lifecycle
    if not author_attempt_id:
        raise ValueError("review-input-revision-owner-required")
    caller = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID")
    if caller and caller != author_attempt_id:
        raise ValueError("review-input-revision-owner-caller-mismatch")
    matches = []
    for line in Path(jobs).read_text(encoding="utf-8").splitlines():
        fields = line.split("\t")
        if len(fields) == 6:
            meta = parse_registry_metadata(fields[5])
            if meta.get("attempt_id") == author_attempt_id:
                matches.append((fields, meta))
    if len(matches) != 1:
        raise ValueError("review-input-revision-owner-not-exact")
    fields, meta = matches[0]
    if (fields[1] not in LIVE_ROW_STATUSES or meta.get("worker_type") != "owner"
            or meta.get("dispatch_depth") != "1" or meta.get("registered_worker") != "1"):
        raise ValueError("review-input-revision-owner-invalid")
    binding, _ = resolve_owner_route_lifecycle(jobs, owner_attempt_id=author_attempt_id)
    if binding is None or (binding.route_id, binding.route_hash) != (route["route_id"], route["route_hash"]):
        raise ValueError("review-input-revision-owner-route-mismatch")


# ---------------------------------------------------------------------------
# 2. On which harness
# ---------------------------------------------------------------------------

# `compose --pin owner|frame|worker=<harness>[:<model>[:<effort>]]` seals one
# pin per target in the route; every launch, resume, replacement and stage
# fallback reads it from the route file the launch is bound to.
PIN_TARGETS = ("owner", "frame", "worker")


def pin_target(worker_type: str | None) -> str:
    return "frame" if worker_type == "frame" else "owner" if worker_type == "owner" else "worker"


def sealed_pin_harness(route, *, worker_type: str | None) -> str | None:
    """The harness this route sealed for the launch's pin target, or None."""

    pins = route.get("selection_pins") if isinstance(route, dict) else None
    pin = pins.get(pin_target(worker_type)) if isinstance(pins, dict) else None
    return (pin.get("harness") or None) if isinstance(pin, dict) else None


def pinned_launch_harness(route, *, worker_type: str | None, requested: str | None, available) -> tuple[str | None, str | None]:
    """A sealed pin beats the requested harness while the pinned one is available.

    Returns `(harness, overridden_request)`; the second item is the request the pin replaced
    (None when nothing was replaced), so the caller can record it.  An unavailable pin, or no
    pin, leaves the request alone.  `available` is the caller's own hard-eligibility test.
    """

    pinned = sealed_pin_harness(route, worker_type=worker_type)
    if not pinned or (pinned != requested and not available(pinned)):
        return requested, None
    return pinned, (requested if requested not in (None, pinned) else None)


# A replacement replays its source on the same harness, registry and worktree.
REPLACEMENT_FIXED_KEYS = ("harness", "jobs", "worktree")


# ---------------------------------------------------------------------------
# 3. How many attempts, of which kind
# ---------------------------------------------------------------------------

def no_stage_authority(meta) -> bool:
    """A row the full-stage round census leaves out (`stage_authority=0`)."""
    return str(meta.get("stage_authority", "1")).lower() in {"0", "false"}


def subsession_row(meta) -> bool:
    """A row that is a sub-session by either mark; it never closes a stage gate itself."""
    return bool(meta.get("subsession_id")) or no_stage_authority(meta)


def linked_worktree_slice(meta) -> bool:
    """A sub-session slice (`stage_authority=0`) runs in a linked worktree while its
    owner row keeps the route cwd, so only a slice may differ from its parent's
    worktree; every other identity comparison stays exact."""
    return bool(meta.get("subsession_id")) and str(meta.get("stage_authority", "")) == "0"


def subsession_launch(subsession_id, stage_authority) -> bool:
    """A launch declared as a sub-session or without stage authority (parsed arguments)."""
    return bool(subsession_id) or stage_authority == 0


def declared_subsession(args) -> bool:
    """The one stage-session declaration judgment, read by `bind` and the dry-run preview.

    True for a complete sub-session declaration (every axis, `stage_authority=0`,
    bound to a dispatch-depth-2 route node), False for an ordinary full-stage
    launch. A partial or contradictory declaration is refused, so no caller can
    turn one raw flag into a sub-session.
    """

    def value(name):
        return getattr(args, name, None)

    values = tuple(value(name) for name in (
        "subsession_id", "subsession_index", "subsession_count", "subsession_mode",
        "session_chain_id", "phase_brief", "narrow_verify", "expected_round_trips",
    ))
    if any(item is not None for item in values) and not all(item is not None for item in values):
        raise _contract_error("subsession-arguments-incomplete", "all stage-session axes are required")
    stage_authority = getattr(args, "stage_authority", 1)
    if not value("subsession_id"):
        if stage_authority != 1:
            raise _contract_error(
                "stage-authority-zero-without-subsession", str(value("route_node") or "")
            )
        return False
    if stage_authority != 0:
        raise _contract_error("subsession-stage-authority-forbidden", value("subsession_id"))
    if value("dispatch_depth") != 2 or not value("route_id") or not value("route_node"):
        raise _contract_error("subsession-route-binding-invalid", value("subsession_id"))
    return True


def retry_predecessor(prior_rows, node, round_admission=None):
    """Transport replacement and an admitted verdict round are different work.

    The shared round admission already counts the exact node's verdict and
    revisions, and attempt_identity salts the new round. A genuine FAIL from
    a transport successor still owns a verdict; its old retry link is not a
    second death to replace. No original row or replacement budget is changed.
    Uncapped work and verdictless failures retain the existing retry binding.
    """
    if not prior_rows:
        return ""
    latest = prior_rows[-1]
    status = latest["_status"]
    if committed_outcome(status, latest) == "failed":
        from worker_bootstrap import worker_type_for_kind
        worker_type = latest.get("worker_type") or worker_type_for_kind(node["kind"])
        if (round_admission is not None and round_admission.budget.state == "admit"
                and classify_round_row(status, latest, worker_type=worker_type) == "verdict"):
            return ""
        return latest.get("attempt_id", "")
    if status == "open" and latest.get("launch_claimed") == "0":
        # Register/start reuse the same unlaunched transport successor. A
        # semantic round has no such link and keeps its own round identity.
        return latest.get("automatic_retry_of", "")
    return ""


# The round budget already has one implementation (`review_round_cap`); these
# are its names in this module, not copies.
classify_round_row = _ROUND.classify_round_row
round_budget = _ROUND.round_budget
is_round_capped_node = _ROUND.is_round_capped_node
last_verdict_blocking = _ROUND.last_verdict_blocking


# The worker's final three lines. Every terminal reader parses them with this
# one pattern, so no surface reads a child as finished while another calls it
# malformed.
HANDOFF_RE = re.compile(
    r"(?:\A|\n)artifact: (?P<artifact>[^\n]+)\n"
    r"verdict: (?P<verdict>PASS|FAIL|BLOCKED)\n"
    r"blocker: (?P<blocker>[^\n]+)\Z"
)


_PASS_NOTE_RE = re.compile(r"none\s*\((?P<note>.+)\)\s*")


def pass_blocker_note(blocker) -> str | None:
    """What a PASS envelope's `blocker: none (...)` adds: "" for a bare `none`, the
    parenthesized note for `none (...)`, None for any other blocker text. The note
    is kept as a remark; it never changes the verdict (RA-8)."""
    if blocker == "none":
        return ""
    match = _PASS_NOTE_RE.fullmatch(blocker) if isinstance(blocker, str) else None
    return match.group("note").strip() if match else None


def pass_blocker_violation(verdict, blocker) -> str | None:
    """A PASS envelope's blocker is `none`, optionally with a note in parentheses;
    any other blocker text breaks the contract."""
    if verdict == "PASS" and pass_blocker_note(blocker) is None:
        return "pass-blocker-not-none"
    return None


# ---------------------------------------------------------------------------
# 4. What it may access
# ---------------------------------------------------------------------------

def bind_access_request(*args, **kwargs):
    """Resolve, validate, constrain and grade an explicit execution access request."""
    from execution_access import bind_request
    return bind_request(*args, **kwargs)


def access_grant(*args, **kwargs):
    """The effective explicit grant for one runtime; never creates runtime argv."""
    from execution_access import build_grant
    return build_grant(*args, **kwargs)
