#!/usr/bin/env python3
"""W7C artifact write-cutover: producer begin/finalize lifecycle.

Correction to the W7 relocation: new cycle output is written in place under
`<artifact-root>/campaigns/<campaign-locator>/<cycle-locator>/artifacts/` and the
typed IDs are issued by `begin` *before* the first write (D-2, D-4).  The
step-1 modules stay the only lineage authorities: `artifact_identity` issues
IDs, `artifact_manifest` validates the closed D-6 schema, `artifact_index`
guards uniqueness, and `artifact_admission` owns the root identity, the global
mutex, and the derived index.

Layout (D-2, closed):

    campaigns/<campaign-locator>/campaign.json              mutable campaign record
    campaigns/<campaign-locator>/<cycle-locator>/.cycle.json stable-ID locator binding + started_on
    campaigns/<campaign-locator>/<cycle-locator>/artifacts  producer output (open)
    campaigns/<campaign-locator>/<cycle-locator>/manifest.json finalize commit point
    shared/<spec|analysis|research>/<ref>/reference.json
    shared/<kind>/<ref>/revisions/<rrev>/...          immutable revision
    .runtime/artifact-producer/v1/cutover.json        cutover state (approval-gated)
    .runtime/artifact-producer/v1/cycles/<cyc>.json   cycle record open|sealed|abandoned
    .runtime/artifact-producer/v1/journal/<cyc>.json  finalize crash journal
    .runtime/artifact-producer/v1/shared-journal/<rrev>.json

Two cutover states.  While `cutover.json` is absent (`inactive`) the legacy
top-level buckets remain writable (compatibility window) and `begin` reports
`layout=legacy`; once the approval package activates the root, `begin` issues
a cycle and every new write outside an open cycle's `artifacts/` is denied.
Shared revisions are immutable in both states and are only created by
`admit-shared` from a sealed cycle.  Research is admitted to `shared/` only
with an explicit promotion (D-3).
"""
from __future__ import annotations

import argparse
import contextlib
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import fcntl
import functools
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, NamedTuple, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission  # noqa: E402
import artifact_cycle_titles  # noqa: E402
import artifact_identity  # noqa: E402
import artifact_index  # noqa: E402
import artifact_lifecycle  # noqa: E402
import artifact_locator  # noqa: E402
import artifact_manifest  # noqa: E402
import artifact_campaign  # noqa: E402
import route_identity  # noqa: E402
import route_lineage  # noqa: E402
import dispatch_contract  # noqa: E402
import dispatch_lock_order  # noqa: E402
import dispatch_terminal_commit  # noqa: E402
from dispatch_contract import (  # noqa: E402
    _PROCESS_IDENTITY_METADATA_KEYS,
    REVIEW_GOVERNED_LEASE_KIND,
    REVIEW_GOVERNED_LEASE_NONCE_RE,
    encode_review_output_locator,
    review_governed_lease_is_held,
    review_lease_record_digest,
    process_start_ticks,
    resolve_dispatch_state_root,
    resolve_agent_home,
    review_output_binding_digest,
    review_output_write_authorized,
    validate_review_output_binding,
    review_holder_disposition,
)
from dispatch_lifecycle import (
    FiniteWatchdogBudget,
    begin_finite_watchdog,
    remaining_watchdog_seconds,
)

PRODUCER_REL = ".runtime/artifact-producer/v1"
CONTRACT = "artifact-producer/v1"
REVISION_RECORD_NAME = "revision.json"
ALGORITHM_VERSION = "w7c-producer/v1"
OK, BLOCKED, USAGE = 0, 65, 64

# D-86: the one-line hint attached to a legacy-top-level-write-denied result.
# The `reason` token itself (compared verbatim by fleet_cutover_gate's
# negative probe) never changes; this hint rides in a separate field/detail.
LEGACY_WRITE_HINT = ("run `artifact_producer.py begin --route <route file>` first; if begin already ran, "
                     "export its --env-file output (AGENT_ARTIFACT_*) into this shell, then retry")

# A cycle that is sealed, abandoned or closed automatically after it sat unused
# (route_autoclose.py) takes no more writes; the way forward is a new route.
CLOSED_CYCLE_HINT = ("this cycle takes no more writes (sealed, abandoned or closed automatically after it "
                     "sat unused); compose the work again for an open cycle")

# D-81: campaign.json `related[]` row kinds (producer-internal API only).
RELATED_KINDS = ("related", "precedes", "supersedes")

# Attached to a first-publication `finalize --allow-open-route` response whose
# cycle sealed `state: active` (D-6): closing the route later, proven or not,
# cannot retroactively make this cycle `completed`.
PROVISIONAL_SEAL_WARNING = (
    "sealed provisionally active: closing the route later, with or without proof, cannot make "
    "this cycle completed; campaign closure lists it as sealed-unproven. Order for new cycles: "
    "complete -> close -> finalize."
)

COMPAT_OVERRIDE_NAME = "compat-override.json"
INACTIVE_FALLBACK_ENV = "AGENT_ARTIFACT_INACTIVE_FALLBACK"
ROOT_CLASSES = ("active", "inactive-with-legacy", "inactive-empty", "malformed")
ACTIVATION_KINDS = ("approval", "bootstrap-empty-root")
RUNTIME_OWNED_EXACT = ("_scratch",)          # `.`-prefix is a separate predicate
COMPAT_OVERRIDE_FIELDS = ("schema_version", "contract", "canonical_root",
                          "reason", "issuer", "created_at", "expires_at")
WAIVER_FIELDS = ("reason", "issuer", "created_at", "expires_at")

# SD-117 §13.34.5-(2): a cycle's abandonment sealing decision must always
# name why -- a closed enum, disjoint from review verdict vocabulary
# (PASS/FAIL/BLOCKED, allow/deny) so the two can never be confused (E47-7).
ABANDON_REASONS = frozenset({
    "operator-decision",
    "route-unrecoverable",
    "lease-expired-no-publisher",
    "operator-override-live-review",
})
REVIEW_LEASE_REL = "review-leases"

INTENSITIES = ("direct", "quick", "standard", "strong", "thorough", "adversarial")
ENTRY_CAPABILITIES = (
    "analyze-project", "analyze-user", "audit", "autopilot-apply", "autopilot-code",
    "autopilot-design", "autopilot-draft", "autopilot-lab", "autopilot-refine",
    "autopilot-research", "autopilot-ship", "autopilot-spec",
)
STAGE_CAPABILITIES = (
    "code-plan", "code-execute", "code-refine", "code-report", "code-test",
    "design-init", "design-refs", "design-tokens", "design-components",
    "design-review", "design-handoff", "draft-strategy", "draft-refine",
)
# Compiler-internal capabilities: no Skill and no person invokes them, but the route
# compiler seals routes for them and a producer cycle must be issuable for that route.
INTERNAL_CAPABILITIES = ("route-frame",)
CANONICAL_ROOTS = ("campaigns", "shared")
# Legacy capability buckets (CORE.md §3 C-DUR) plus the undeclared containers.
LEGACY_TOP_LEVEL = (
    "analysis_project", "research", "spec", "plans", "documents", "experiments",
    "designs", "_internal", "reviews", "shards", "routes", "_routes", "notes",
    "proposals", "spec-research-alternative", "research-alternative", "release-config",
    "evidence", "dev_logs", "test_logs", "user_profile",
)
SHARED_KINDS = {
    "spec": "shared-spec",
    "analysis": "cumulative-analysis",
    "research": "shared-research",
}
BUCKET_TYPES = {
    "plans": "plan", "documents": "document", "designs": "design", "spec": "spec",
    "research": "research", "experiments": "experiment", "analysis_project": "analysis",
    "analysis": "analysis", "reviews": "review", "release-config": "release-config",
    "apply-log": "apply-log", "user_profile": "profile",
}
CAPABILITY_BUCKETS = {
    "analyze-project": "analysis_project", "analyze-user": "user_profile", "audit": "reviews",
    "autopilot-apply": "apply-log", "autopilot-code": "plans", "autopilot-design": "designs",
    "autopilot-draft": "documents", "autopilot-lab": "experiments", "autopilot-refine": "documents",
    "autopilot-research": "research", "autopilot-ship": "release-config", "autopilot-spec": "spec",
}


def default_bucket(capability: str) -> str:
    return CAPABILITY_BUCKETS.get(capability, capability if capability in BUCKET_TYPES else "analysis")


MEDIA_TYPES = {
    ".md": "text/markdown", ".json": "application/json", ".yaml": "application/yaml",
    ".yml": "application/yaml", ".txt": "text/plain", ".csv": "text/csv",
    ".html": "text/html", ".svg": "image/svg+xml", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".pdf": "application/pdf",
    ".py": "text/x-python", ".sh": "text/x-shellscript", ".log": "text/plain",
    ".jsonl": "application/x-ndjson", ".toml": "application/toml",
}
PRIMARY_CANDIDATES = (
    "final_report.md", "report.md", "report.html", "prd.md", "plan.md", "handoff.md", "verdict.json",
)
# CORE §3 top-level `C-INT` names that are not a cycle bucket: support material is
# kept in the manifest but is not auto-nominated as a cycle's primary artifact.
SUPPORT_SEGMENTS = frozenset({"_internal", "shards"})
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ProducerError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# small filesystem helpers
# ---------------------------------------------------------------------------


def _rfc3339(now: Optional[float] = None) -> str:
    t = time.time() if now is None else now
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + "Z"


def _rfc3339_precise(now: Optional[float] = None) -> str:
    t = time.time() if now is None else float(now)
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_exclusive(path: Path, data: bytes, mode: int = 0o644) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _ensure_dir(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise ProducerError("path-not-directory", str(path))
        return
    path.mkdir(parents=True, exist_ok=True)


def _walk_files(top: Path) -> List[Path]:
    out: List[Path] = []
    for current, dirs, files in os.walk(str(top), followlinks=False):
        dirs.sort()
        for name in sorted(files):
            out.append(Path(current) / name)
        for name in list(dirs):
            if os.path.islink(os.path.join(current, name)):
                out.append(Path(current) / name)
                dirs.remove(name)
    return out


def _copy_tree_files(source: Path, target: Path) -> Tuple[List[Tuple[str, str, int]], List[str]]:
    """Copy regular files only. Returns (rows, violations)."""
    rows: List[Tuple[str, str, int]] = []
    violations: List[str] = []
    if source.is_file():
        entries = [source]
        base = source.parent
    else:
        entries = _walk_files(source)
        base = source
    for entry in entries:
        rel = entry.relative_to(base).as_posix()
        if os.path.islink(str(entry)):
            violations.append(f"symlink-forbidden:{rel}")
            continue
        if not entry.is_file():
            violations.append(f"non-regular-file:{rel}")
            continue
        data = entry.read_bytes()
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        _write_exclusive(dst, data)
        rows.append((rel, _digest(data), len(data)))
    for current, _dirs, _files in os.walk(str(target)):
        _fsync_dir(Path(current))
    return rows, violations


# ---------------------------------------------------------------------------
# state paths
# ---------------------------------------------------------------------------


def producer_dir(root: Path) -> Path:
    return Path(root) / PRODUCER_REL


def cutover_path(root: Path) -> Path:
    return producer_dir(root) / "cutover.json"


def compat_override_path(root: Path) -> Path:
    return producer_dir(root) / COMPAT_OVERRIDE_NAME


def cycle_record_path(root: Path, cycle_id: str) -> Path:
    if not artifact_identity.is_well_formed(cycle_id, "cycle"):
        raise ProducerError("cycle-id-invalid", str(cycle_id))
    return producer_dir(root) / "cycles" / f"{cycle_id}.json"


def journal_path(root: Path, cycle_id: str) -> Path:
    return producer_dir(root) / "journal" / f"{cycle_id}.json"


def shared_journal_path(root: Path, revision_id: str) -> Path:
    return producer_dir(root) / "shared-journal" / f"{revision_id}.json"


def campaign_dir(root: Path, campaign_id: str, record: Optional[Mapping[str, Any]] = None) -> Path:
    """Resolve an existing campaign by record identity, with old-ID fallback.

    Creation sites must pass a record containing its persisted ``locator``;
    this function never derives a readable path from the stable ID.
    """
    root = Path(root)

    def candidates() -> Iterator[Path]:
        if record is not None and record.get("campaign_id") == campaign_id and record.get("locator"):
            try:
                campaigns = artifact_locator.safe_child(root, root, "campaigns")
                yield artifact_locator.safe_child(root, campaigns, record["locator"])
            except artifact_locator.LocatorError:
                return

    found = artifact_locator.locate(root, campaign_id, candidates=candidates)
    if found is not None and (found / "campaign.json").is_file():
        return found
    try:
        campaigns = artifact_locator.safe_child(root, root, "campaigns")
        if record is not None and record.get("campaign_id") == campaign_id and record.get("locator"):
            return artifact_locator.safe_child(root, campaigns, record["locator"])
        return artifact_locator.safe_child(root, campaigns, campaign_id)
    except artifact_locator.LocatorError as exc:
        raise ProducerError("record-locator-invalid", exc.detail or exc.code) from exc


def cycle_dir(root: Path, campaign_id: str, cycle_id: str,
              record: Optional[Mapping[str, Any]] = None) -> Path:
    """Resolve an existing cycle, accepting readable, hybrid, and old layouts."""
    root = Path(root)
    if record is None:
        record = read_cycle_record(root, cycle_id)
    campaign_record = read_campaign(root, campaign_id)

    def owner_campaign_candidates() -> Iterator[Path]:
        if campaign_record is not None and campaign_record.get("locator"):
            try:
                campaigns = artifact_locator.safe_child(root, root, "campaigns")
                yield artifact_locator.safe_child(root, campaigns, campaign_record["locator"])
            except artifact_locator.LocatorError:
                return

    def cycle_candidates() -> Iterator[Path]:
        if record is None or record.get("campaign_id") != campaign_id:
            return
        try:
            parent = campaign_dir(root, campaign_id, campaign_record)
        except ProducerError:
            return
        if record.get("locator"):
            try:
                yield artifact_locator.safe_child(root, parent, record["locator"])
            except artifact_locator.LocatorError:
                pass
        try:
            cycles = artifact_locator.safe_child(root, parent, "cycles")
            yield artifact_locator.safe_child(root, cycles, cycle_id)
        except artifact_locator.LocatorError:
            pass

    found = artifact_locator.locate(
        root, cycle_id, candidates=cycle_candidates,
        owner_campaign_id=campaign_id, owner_campaign_candidates=owner_campaign_candidates,
    )
    if found is not None:
        return found
    parent = campaign_dir(root, campaign_id, campaign_record)
    try:
        if record is not None and record.get("campaign_id") == campaign_id and record.get("locator"):
            return artifact_locator.safe_child(root, parent, record["locator"])
        cycles = artifact_locator.safe_child(root, parent, "cycles")
        return artifact_locator.safe_child(root, cycles, cycle_id)
    except artifact_locator.LocatorError as exc:
        raise ProducerError("record-locator-invalid", exc.detail or exc.code) from exc


def read_cutover(root: Path) -> Dict[str, Any]:
    value = _read_json(cutover_path(root))
    if value is None:
        return {"state": "inactive"}
    return value


def is_active(root: Path) -> bool:
    return read_cutover(root).get("state") == "active"


RELAYOUT_STATE_NAME = "relayout.json"


def relayout_state_path(root: Path) -> Path:
    return producer_dir(root) / RELAYOUT_STATE_NAME


def read_relayout_state(root: Path) -> Dict[str, Any]:
    """W7I Cycle B per-root state. Absent means the D-91 transition window is
    still open (slugless pre-W7I routes are named by derivation, not refused)."""
    value = _read_json(relayout_state_path(root))
    if value is None:
        return {"state": "pending", "transition_window": "open"}
    return value


def transition_window_closed(root: Path) -> bool:
    return read_relayout_state(root).get("transition_window") == "closed"


def _is_runtime_owned_top_level(name: str) -> bool:
    return name.startswith(".") or name in RUNTIME_OWNED_EXACT


def _legacy_content_names(root: Path, *, exhaustive: bool = False) -> List[str]:
    """Top-level names holding non-runtime content.

    Mirrors `_walk_files`'s symlink policy: never follow a symlink, but count
    it as content. Stops at the first hit unless `exhaustive` is set, so the
    hot-path predicate never walks a large legacy tree.
    """
    root = Path(root)
    if not root.is_dir():
        return []
    found: List[str] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        name = entry.name
        if _is_runtime_owned_top_level(name):
            continue
        has_content = False
        if entry.is_symlink():
            has_content = True
        elif entry.is_file():
            has_content = True
        elif entry.is_dir():
            for current, dirs, files in os.walk(str(entry), followlinks=False):
                if files:
                    has_content = True
                    break
                linked = [d for d in dirs if os.path.islink(os.path.join(current, d))]
                if linked:
                    has_content = True
                    break
        if has_content:
            found.append(name)
            if not exhaustive:
                return found
    return found


def classify_root(root: Path, *, collect_legacy_top_level: bool = False) -> Dict[str, Any]:
    """D-72 root classification. Never creates or modifies anything.

    Returns {"state": active|inactive-with-legacy|inactive-empty|malformed,
             "root": str, "cutover_state": str|None, "activation_kind": str|None,
             "identity": {"repository_id":…, "artifact_root_id":…}|None,
             "reason": str|None,
             "legacy_top_level": List[str], "legacy_top_level_complete": bool}
    """
    root = Path(root)
    result: Dict[str, Any] = {
        "state": None, "root": str(root), "cutover_state": None, "activation_kind": None,
        "identity": None, "reason": None,
        "legacy_top_level": [], "legacy_top_level_complete": collect_legacy_top_level,
    }
    path = cutover_path(root)
    cutover: Optional[Dict[str, Any]] = None
    if path.exists():
        cutover = _read_json(path)
        if cutover is None:
            result["state"] = "malformed"
            result["reason"] = "cutover-record-unreadable"
            return result
    if cutover is not None and cutover.get("state") not in ("active", "inactive"):
        result["state"] = "malformed"
        result["reason"] = "cutover-schema-unknown"
        return result
    try:
        identity = artifact_lifecycle.read_root_identity(root)
    except artifact_lifecycle.LifecycleError:
        result["state"] = "malformed"
        result["reason"] = "root-identity-invalid"
        return result
    if cutover is not None and cutover.get("state") == "active":
        cutover_root_id = (cutover.get("identity") or {}).get("artifact_root_id")
        if identity is None or cutover_root_id != identity.artifact_root_id:
            result["state"] = "malformed"
            result["reason"] = "identity-conflict"
            return result
        result["state"] = "active"
        result["cutover_state"] = "active"
        result["activation_kind"] = cutover.get("activation_kind", "approval")
        result["identity"] = {"repository_id": identity.repository_id,
                              "artifact_root_id": identity.artifact_root_id}
        return result
    if not root.exists():
        result["state"] = "inactive-empty"
        return result
    try:
        names = _legacy_content_names(root, exhaustive=collect_legacy_top_level)
    except OSError:
        result["state"] = "malformed"
        result["reason"] = "root-unreadable"
        return result
    result["legacy_top_level"] = names
    result["state"] = "inactive-with-legacy" if names else "inactive-empty"
    return result


def validate_time_bounded_grant(payload: Any, *, canonical_root: Path,
                                required_fields: Sequence[str],
                                now: Optional[float] = None) -> Dict[str, Any]:
    """Shared fail-closed rule for D-74 compat overrides and D-75 waivers.

    Returns {"status": "accepted"|"rejected",
             "reason": None|"malformed"|"expired"|"foreign-root",
             "expires_at": str|None}
    """
    if not isinstance(payload, dict):
        return {"status": "rejected", "reason": "malformed", "expires_at": None}
    for field in required_fields:
        value = payload.get(field)
        if value is None or value == "":
            return {"status": "rejected", "reason": "malformed", "expires_at": None}
    if "schema_version" in required_fields and payload.get("schema_version") != 1:
        return {"status": "rejected", "reason": "malformed", "expires_at": None}
    if "contract" in required_fields and payload.get("contract") != CONTRACT:
        return {"status": "rejected", "reason": "malformed", "expires_at": None}
    expires_at = payload.get("expires_at")
    try:
        expires_ts = _rfc3339_to_epoch(str(expires_at))
    except (ValueError, OverflowError):
        return {"status": "rejected", "reason": "malformed", "expires_at": None}
    if "canonical_root" in payload:
        if os.path.realpath(str(payload["canonical_root"])) != os.path.realpath(str(canonical_root)):
            return {"status": "rejected", "reason": "foreign-root", "expires_at": expires_at}
    when = time.time() if now is None else now
    if expires_ts <= when:
        return {"status": "rejected", "reason": "expired", "expires_at": expires_at}
    return {"status": "accepted", "reason": None, "expires_at": expires_at}


def read_compat_override(root: Path, *, now: Optional[float] = None) -> Dict[str, Any]:
    """{"status": "absent"|"accepted"|"rejected",
        "reason": None|"override-malformed"|"override-expired"|"override-foreign-root",
        "path": str, "expires_at": str|None}"""
    path = compat_override_path(root)
    if not path.exists():
        return {"status": "absent", "reason": None, "path": str(path), "expires_at": None}
    payload = _read_json(path)
    verdict = validate_time_bounded_grant(
        payload, canonical_root=root, required_fields=COMPAT_OVERRIDE_FIELDS, now=now)
    reason = f"override-{verdict['reason']}" if verdict["reason"] else None
    return {"status": verdict["status"], "reason": reason, "path": str(path),
            "expires_at": verdict["expires_at"]}


def inactive_fallback_level() -> str:
    """`warn` unless AGENT_ARTIFACT_INACTIVE_FALLBACK is exactly `deny`;
    any other non-empty value is fail-closed to `deny`."""
    value = os.environ.get(INACTIVE_FALLBACK_ENV, "")
    if value in ("", "warn"):
        return "warn"
    return "deny"


def legacy_fallback_state(root: Path, *, now: Optional[float] = None,
                          classification: Optional[Mapping[str, Any]] = None
                          ) -> Optional[Dict[str, Any]]:
    """D-74 typed block; None unless the root is `inactive-with-legacy`."""
    klass = classification if classification is not None else classify_root(root)
    if klass["state"] != "inactive-with-legacy":
        return None
    return {
        "level": inactive_fallback_level(),
        "reason": "cutover-inactive-legacy-root",
        "override": read_compat_override(root, now=now),
    }


def _fallback_blocks(fallback: Optional[Mapping[str, Any]]) -> bool:
    return bool(fallback) and fallback["level"] == "deny" \
        and fallback["override"]["status"] != "accepted"


def read_cycle_record(root: Path, cycle_id: str) -> Optional[Dict[str, Any]]:
    if not artifact_identity.is_well_formed(cycle_id, "cycle"):
        return None
    return _read_json(cycle_record_path(root, cycle_id))


def _write_cycle_record(root: Path, record: Dict[str, Any], *, exclusive: bool) -> None:
    path = cycle_record_path(root, record["cycle_id"])
    _ensure_dir(path.parent)
    data = _json_bytes(record)
    if exclusive:
        _write_exclusive(path, data, 0o600)
    else:
        _write_atomic(path, data, 0o600)


def _write_cycle_binding(directory: Path, campaign_id: str, cycle_id: str,
                         *, started_on: Optional[str] = None) -> None:
    marker = Path(directory) / artifact_locator.CYCLE_BINDING
    data = artifact_locator.cycle_binding_bytes(campaign_id, cycle_id, started_on=started_on)
    if marker.is_file() and not marker.is_symlink():
        if marker.read_bytes() != data:
            raise ProducerError("cycle-binding-conflict", str(marker))
        return
    _write_exclusive(marker, data)


def list_cycle_records(root: Path) -> List[Dict[str, Any]]:
    directory = producer_dir(root) / "cycles"
    rows: List[Dict[str, Any]] = []
    if not directory.is_dir():
        return rows
    for entry in sorted(directory.iterdir(), key=lambda p: p.name):
        if entry.suffix == ".json":
            value = _read_json(entry)
            if value is not None:
                rows.append(value)
    return rows


# ---------------------------------------------------------------------------
# D-120: route lineage binding -- "route W may write cycle C" is judged fresh
# from the sealed route file and the cycle record's begin values every time,
# never from the producer's own audit copy (`route_bindings[]`, which
# `bind_cycle_route` writes and only readers consume). SD-155's
# `verified_route_lineage` is the one hash-verified walk this admission and
# `route_cycle_for` both stand on.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Admission:
    allow: bool
    reason: Optional[str]
    detail: str
    path: List[Dict[str, Any]]
    next_action: Optional[str]


_D120_NEXT_ACTION = {
    "cycle-not-open": "start a new cycle (--parent-cycle to keep it linked)",
    "route-lineage-unverified": ("restore the sealed route file, or start a new route; a continuation that "
                                  "changed capability needs a new child cycle (--parent-cycle)"),
    "route-hash-drift": "restore the sealed route file",
    "cycle-route-binding-mismatch": "start a new cycle (--parent-cycle to keep it linked)",
    "cycle-route-binding-mismatch:material-input": "start a new child cycle (--parent-cycle)",
    "cycle-route-binding-mismatch:lineage-fork": "continue this branch in a new child cycle (--parent-cycle)",
    "cycle-route-binding-mismatch:superseded-route": "close and seal from the end route instead",
}


def _d120_next_action(reason: str) -> str:
    return _D120_NEXT_ACTION.get(reason, _D120_NEXT_ACTION.get(reason.split(":", 1)[0], "retry"))


def _routes_dir(root: Path) -> Path:
    return route_lineage.canonical_route_path(root, "x").parent


def _qualified_continuation(entry_stem: str, candidate: Any, route_id: Optional[str] = None,
                            route_hash_value: Optional[str] = None) -> bool:
    """Whether a route file is a sealed continuation edge (of ``route_id`` when given).

    The one qualification rule shared by `_lineage_children` and
    `closed_lineage_handover`: not its own source, contract version 1, source id
    and hash match, and the route hash recomputes -- a tampered sibling proves
    nothing and is not part of any lineage.
    """
    if not isinstance(candidate, dict) or candidate.get("continuation_contract_version") != 1:
        return False
    source_id = candidate.get("source_route_id")
    if entry_stem == source_id or (route_id is not None and source_id != route_id):
        return False
    if route_hash_value is not None and candidate.get("source_route_hash") != route_hash_value:
        return False
    return route_identity.route_hash(candidate) == candidate.get("route_hash")


def _lineage_children(root: Path, route_id: str, route_hash_value: str) -> List[Dict[str, Any]]:
    """Every continuation whose sealed edge names ``route_id``/``route_hash_value`` as its source.

    A digest-keyed memo is allowed by D-120 ("구현은 디렉터리 목록 digest에 결속된
    자식 색인을 캐시로 둘 수 있다") but correctness never depends on one; this
    reads the directory fresh, which is fine at the scale a single cycle's
    lineage tree reaches.
    """
    directory = _routes_dir(root)
    if not directory.is_dir():
        return []
    children: List[Dict[str, Any]] = []
    for entry in sorted(directory.glob("*.json")):
        if entry.stem == route_id:
            continue
        candidate = _read_json(entry)
        if not isinstance(candidate, dict):
            continue
        if _qualified_continuation(entry.stem, candidate, route_id, route_hash_value):
            children.append(candidate)
    return children


class LineageHandover(NamedTuple):
    """`closed`: the cycle's whole qualifying lineage tree has an outcome.
    `handed_over`: routes of that tree that begin another cycle (empty unless closed)."""
    closed: bool
    handed_over: frozenset


def closed_lineage_handover(root: Path, record: Mapping[str, Any]) -> LineageHandover:
    """Whether ``record``'s lineage is closed, and which routes it handed to other cycles.

    The tree is every material-input-qualifying continuation below the cycle's
    begin route, read with one scan of the routes directory. It is closed only
    when every route in it has an outcome; a live tree keeps D-120's "which cycle
    continues" judgment untouched (`(False, frozenset())`). In a closed tree the
    routes that begin a *different* cycle belong to that cycle, so this cycle
    seals on its own stretch, up to just before them.
    """
    begin_route = load_route(root, Path(record["route_file"]))
    if begin_route["route_hash"] != record["route_hash"]:
        raise ProducerError("route-hash-drift", record["cycle_id"])
    children_of: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    directory = _routes_dir(root)
    if directory.is_dir():
        for entry in sorted(directory.glob("*.json")):
            candidate = _read_json(entry)
            if _qualified_continuation(entry.stem, candidate):
                key = (candidate["source_route_id"], candidate.get("source_route_hash"))
                children_of.setdefault(key, []).append(candidate)
    tree = {begin_route["route_id"]}
    queue = [begin_route]
    while queue:
        node = queue.pop()
        if not route_is_closed(root, node):
            return LineageHandover(False, frozenset())
        for child in children_of.get((node["route_id"], node["route_hash"]), []):
            if (child.get("capability") != record.get("capability")
                    or child.get("effective_intensity") != record.get("intensity")):
                continue
            if child["route_id"] in tree:
                return LineageHandover(False, frozenset())  # a cycle in the lineage is never closed
            tree.add(child["route_id"])
            queue.append(child)
    others = {rec.get("route_id") for rec in list_cycle_records(root)
              if rec.get("cycle_id") != record.get("cycle_id")}
    return LineageHandover(True, frozenset(tree & others) - {begin_route["route_id"]})


def _handed_over_routes(root: Path, record: Mapping[str, Any]) -> frozenset:
    try:
        return closed_lineage_handover(root, record).handed_over
    except (ProducerError, KeyError, OSError):
        return frozenset()


def route_cycle_for(root: Path, route: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The one open cycle begun by a route in ``route``'s verified lineage, if any.

    Shared by every "find the cycle for this route" call site (env resolution,
    begin resume, checkpoint, `require_cycle_output`). Two or more open cycles
    whose begin route sits in the same verified lineage is the existing
    `route-cycle-binding-ambiguous` refusal.
    """
    try:
        lineage = route_lineage.verified_route_lineage(dict(route), artifact_root=root)
    except route_lineage.RouteLineageError as exc:
        raise ProducerError(exc.code, exc.detail) from exc
    lineage_ids = {r.get("route_id") for r in lineage}
    matches = [rec for rec in list_cycle_records(root)
               if rec.get("state") == "open" and rec.get("route_id") in lineage_ids]
    if len(matches) > 1:
        raise ProducerError("route-cycle-binding-ambiguous", route.get("route_id", ""))
    return matches[0] if matches else None


def cycle_route_admission(root: Path, record: Mapping[str, Any], route: Mapping[str, Any],
                          *, finalize: bool = False, validation_only: bool = False) -> Admission:
    """D-120's one lineage judgment for a cycle route.

    Steps 0-4 exactly as spelled out in artifact-path-contract §42: cycle
    open, ``route``'s hash-verified lineage, the begin route's membership and
    hash in that lineage, material-input (capability/intensity) parity along
    the path, and sibling-fork detection at every path node but ``route``
    itself. ``finalize=True`` adds D-120's finalize-only rule: a route with a
    still-attached T(C) continuation child cannot seal
    (`...:superseded-route`). ``validation_only`` is read-only manifest
    verification: it permits an already sealed record but never grants write
    authority or changes the record.
    """
    state = record.get("state")
    if state != "open" and not (validation_only and state == "sealed"):
        reason = "cycle-not-open"
        return Admission(False, reason, str(state), [], _d120_next_action(reason))
    try:
        lineage = route_lineage.verified_route_lineage(dict(route), artifact_root=root)
    except route_lineage.RouteLineageError as exc:
        reason = "route-lineage-unverified"
        return Admission(False, reason, str(exc), [], _d120_next_action(reason))
    by_id = {r["route_id"]: r for r in lineage}
    begin_id = record.get("route_id")
    if begin_id not in by_id:
        reason = "cycle-route-binding-mismatch"
        return Admission(False, reason, f"begin={begin_id} not in lineage of {route.get('route_id')}",
                         [], _d120_next_action(reason))
    if by_id[begin_id].get("route_hash") != record.get("route_hash"):
        reason = "route-hash-drift"
        return Admission(False, reason, str(record.get("cycle_id", "")), [], _d120_next_action(reason))
    # lineage is [W, parent, ..., A]; the admitted path P runs begin (A) -> W.
    begin_index = next(i for i, r in enumerate(lineage) if r["route_id"] == begin_id)
    path = list(reversed(lineage[: begin_index + 1]))
    for node in path:
        if node.get("capability") != record.get("capability") or node.get("effective_intensity") != record.get("intensity"):
            reason = "cycle-route-binding-mismatch:material-input"
            return Admission(False, reason, f"route={node['route_id']}", path, _d120_next_action(reason))
    handed_over: Optional[frozenset] = None  # computed only when a refusal is about to be issued
    for node in path[:-1]:
        siblings = _lineage_children(root, node["route_id"], node["route_hash"])
        qualifying = [s for s in siblings
                      if s.get("capability") == record.get("capability")
                      and s.get("effective_intensity") == record.get("intensity")]
        for sibling in qualifying:
            if sibling["route_id"] not in by_id:
                if handed_over is None:
                    handed_over = _handed_over_routes(root, record)
                if sibling["route_id"] in handed_over:
                    continue  # closed lineage: that branch belongs to another cycle
                reason = "cycle-route-binding-mismatch:lineage-fork"
                return Admission(False, reason, f"branch={node['route_id']} siblings={sibling['route_id']},{path[path.index(node)+1]['route_id']}",
                                 path, _d120_next_action(reason))
    if finalize:
        qualifying = [s for s in _lineage_children(root, route["route_id"], route["route_hash"])
                      if s.get("capability") == record.get("capability")
                      and s.get("effective_intensity") == record.get("intensity")]
        if qualifying:
            if handed_over is None:
                handed_over = _handed_over_routes(root, record)
            qualifying = [s for s in qualifying if s["route_id"] not in handed_over]
        if qualifying:
            reason = "cycle-route-binding-mismatch:superseded-route"
            return Admission(False, reason, route["route_id"], path, _d120_next_action(reason))
    return Admission(True, None, "", path, None)


def _inline_producer_binding_check(root: Path, cycle_id: str,
                                   binding: Mapping[str, Any]) -> None:
    """Check a direct finish against D-120's verified route lineage.

    The cycle record names its *begin* route. A continuation may be the route
    that closes and seals it, so comparing the two route IDs would reject a
    valid finish and would bypass the common lineage predicate.
    """
    record = read_cycle_record(root, cycle_id)
    campaign = read_campaign(root, record["campaign_id"]) if record else None
    identity = artifact_lifecycle.read_root_identity(root)
    route_id = binding.get("route_id")
    if not isinstance(route_id, str) or not _ROUTE_ID_RE.fullmatch(route_id):
        raise ProducerError("inline-producer-binding-mismatch", "route-id")
    path = route_lineage.canonical_route_path(root, route_id)
    try:
        info = path.lstat()
    except OSError as exc:
        raise ProducerError("inline-producer-binding-mismatch", "route-missing") from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or path.resolve() != path:
        raise ProducerError("inline-producer-binding-mismatch", "route-kind")
    route = load_route(root, path)
    if (not record or not campaign or not identity
            or binding.get("kind") != "inline_producer_binding_v1"
            or binding.get("cycle_id") != cycle_id
            or binding.get("campaign_id") != record.get("campaign_id")
            or binding.get("campaign_key") != campaign.get("key")
            or binding.get("producer_id") != record.get("producer_id")
            or binding.get("artifact_root_id") != identity.artifact_root_id
            or binding.get("route_hash") != route.get("route_hash")
            or binding.get("cycle_record_digest") != dispatch_terminal_commit.cycle_identity_digest(record)
            or not binding.get("inline_finish_id")
            or not binding.get("terminal_marker_digest")
            or not binding.get("evidence_sha256")):
        raise ProducerError("inline-producer-binding-mismatch", cycle_id)
    admission = cycle_route_admission(
        root, record, route, finalize=True, validation_only=record.get("state") == "sealed")
    if not admission.allow:
        raise ProducerError(admission.reason or "inline-producer-binding-mismatch", admission.detail)
    import inline_finish
    finish = inline_finish.pending_state(root, route_id)
    intent = (finish or {}).get("intent") or {}
    if (not finish or finish.get("inline_finish_id") != binding["inline_finish_id"]
            or finish.get("terminal_marker_digest") != binding["terminal_marker_digest"]
            or intent.get("route_id") != route_id
            or intent.get("route_hash") != binding["route_hash"]
            or intent.get("cycle_id") != cycle_id
            or intent.get("campaign_id") != record["campaign_id"]
            or intent.get("campaign_key") != campaign["key"]
            or intent.get("producer_id") != record["producer_id"]
            or intent.get("artifact_root_id") != identity.artifact_root_id
            or intent.get("evidence_sha256") != binding["evidence_sha256"]):
        raise ProducerError("inline-producer-binding-mismatch", "finish-intent")


def resolve_cycle_manifest_route(root: Path, record: Mapping[str, Any],
                                 document: Mapping[str, Any]) -> Tuple[Path, Dict[str, Any]]:
    """Resolve and read-only admit the one route sealed by a cycle manifest.

    The cycle record's route tuple remains the begin identity.  A manifest may
    name its verified continuation leaf, so its sole route row is resolved
    through the canonical route directory and admitted against that begin
    identity.  This helper is safe for both pre-seal recovery and sealed replay;
    it never changes cycle state or consults ``route_bindings``.
    """
    rows = document.get("routes") if isinstance(document, Mapping) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        raise ProducerError("completion-route-composite-mismatch", str(record.get("cycle_id", "")))
    row = rows[0]
    identity = artifact_lifecycle.read_root_identity(Path(root))
    if (identity is None or row.get("artifact_root_id") != identity.artifact_root_id
            or document.get("artifact_root_id") != identity.artifact_root_id
            or document.get("cycle", {}).get("cycle_id") != record.get("cycle_id")):
        raise ProducerError("completion-route-composite-mismatch", str(record.get("cycle_id", "")))
    route_id = row.get("route_id")
    if not isinstance(route_id, str) or not re.fullmatch(r"rt-[A-Za-z0-9][A-Za-z0-9._-]{0,126}", route_id):
        raise ProducerError("completion-route-composite-mismatch", "route-id")
    route_path = route_lineage.canonical_route_path(root, route_id)
    try:
        info = route_path.lstat()
    except OSError as exc:
        raise ProducerError("route-lineage-unverified", f"canonical-route-missing:{route_id}") from exc
    if not stat.S_ISREG(info.st_mode) or route_path.is_symlink() or route_path.resolve() != route_path:
        raise ProducerError("route-lineage-unverified", f"canonical-route-kind:{route_id}")
    target_check = artifact_lifecycle.validate_route_target(route_path, Path(root), route_id)
    if not target_check.ok:
        reason = target_check.reasons[0]
        raise ProducerError(reason.code, reason.detail)
    route = load_route(Path(root), route_path, expected_identity=row)
    admission = cycle_route_admission(Path(root), record, route, finalize=True, validation_only=True)
    if not admission.allow:
        raise ProducerError(admission.reason or "route-lineage-unverified", admission.detail)
    return route_path, route


def _route_binding_entry(route_row: Mapping[str, Any], root: Path, *, is_begin: bool) -> Dict[str, Any]:
    return {
        "route_id": route_row["route_id"],
        "route_hash": route_row["route_hash"],
        "route_file": str(route_lineage.canonical_route_path(root, route_row["route_id"])),
        "basis": "begin" if is_begin else "continuation",
        "continuation_id": None if is_begin else route_row["route_id"],
        "source_route_id": None if is_begin else route_row.get("source_route_id"),
    }


def _binding_core(entry: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: entry.get(key) for key in
            ("route_id", "route_hash", "route_file", "basis", "continuation_id", "source_route_id")}


def _bind_cycle_route_locked(root: Path, record: Mapping[str, Any], route: Mapping[str, Any]) -> Dict[str, Any]:
    """`bind_cycle_route`'s body, for a caller that already holds the admission lock."""
    admission = cycle_route_admission(root, record, route)
    if not admission.allow:
        raise ProducerError(admission.reason, admission.detail)
    expected = [_route_binding_entry(row, root, is_begin=(i == 0)) for i, row in enumerate(admission.path)]
    # A record that never had `route_bindings` at all reads as the compat
    # single-element view (begin route only) -- a legitimate, common state.
    # A record whose `route_bindings` field is *present but empty* is not:
    # every real writer only ever stores a non-empty list (at least the begin
    # entry), so an explicit `[]` can only be tampering (A-25.9 (k)) and must
    # not be silently absorbed into the same "nothing bound yet" prefix match.
    if "route_bindings" not in record:
        stored = [{**_route_binding_entry(admission.path[0], root, is_begin=True),
                  "bound_at": record.get("started_on")}]
    else:
        stored = list(record.get("route_bindings") or [])
    stored_core = [_binding_core(e) for e in stored]
    expected_core = [_binding_core(e) for e in expected]
    advisory = None
    if stored and stored_core == expected_core[: len(stored_core)]:
        new_list = stored + [dict(e, bound_at=_rfc3339()) for e in expected[len(stored_core):]]
    else:
        index = 0
        field = "route_id"
        for index in range(min(len(stored_core), len(expected_core))):
            mismatched = [k for k in expected_core[index] if stored_core[index].get(k) != expected_core[index].get(k)]
            if mismatched:
                field = mismatched[0]
                break
        else:
            index = min(len(stored_core), len(expected_core))
        advisory = f"route-binding-record-drift:index={index};field={field}"
        new_list = [dict(e, bound_at=(stored[i].get("bound_at") if i < len(stored) else _rfc3339()))
                    for i, e in enumerate(expected)]
    written = new_list != stored
    if written:
        _write_cycle_record(root, {**record, "route_bindings": new_list}, exclusive=False)
    return {"written": written, "advisory": advisory}


def bind_cycle_route(root: Path, cycle_id: str, route: Mapping[str, Any]) -> Dict[str, Any]:
    """D-120's one audit-record writer: `route_bindings[]` derived from the sealed lineage.

    Re-judges admission under the admission lock and only writes on allow.
    The write is append-if-prefix, replace-with-advisory otherwise -- never an
    input to any judgment (D-120 "권한은 봉인 파일에서만 나온다"). Returns
    ``{"written": bool, "advisory": str|None}``.
    """
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT)
    try:
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        return _bind_cycle_route_locked(root, record, route)
    finally:
        artifact_admission._release_lock(root, lock_fd)


def _write_journal(root: Path, cycle_id: str, **fields: Any) -> None:
    path = journal_path(root, cycle_id)
    _ensure_dir(path.parent)
    payload = {"schema_version": 1, "cycle_id": cycle_id, "updated_at": _rfc3339()}
    payload.update(fields)
    _write_atomic(path, _json_bytes(payload), 0o600)


def _remove_journal(root: Path, cycle_id: str) -> None:
    try:
        journal_path(root, cycle_id).unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# route helpers
# ---------------------------------------------------------------------------


_ROUTE_ID_RE = re.compile(r"^rt-[0-9a-f]{6,}$")


def resolve_route_argument(root: Path, value: "str | Path") -> Path:
    """Accept either a route file path or a bare route id.

    A `quick` dispatch-depth-1 owner is handed only the route **id** — the
    prompt and the wrapper args carry `--route-id`, never the file path — so an
    owner that passed that id straight through used to die with
    `route-unreadable` naming the id it was given. A bare id is unambiguous: it
    resolves to exactly one canonical location under the artifact root. Resolve
    it instead of refusing. Anything else is returned untouched and is read as
    the path it is, so an explicit path always wins.
    """

    text = str(value)
    if _ROUTE_ID_RE.match(text):
        candidate = artifact_lifecycle.canonical_route_path(Path(root), text)
        if candidate.is_file():
            return candidate
    return Path(value)


def load_route(root: Path, route_file: Path, *,
               expected_identity: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    route_file = resolve_route_argument(root, route_file)
    route = _read_json(Path(route_file))
    if route is None:
        raise ProducerError("route-unreadable", str(route_file))
    for key in ("route_id", "route_hash", "capability", "effective_intensity", "artifact_root", "nodes"):
        if key not in route:
            raise ProducerError("route-invalid", f"missing {key}")
    if Path(str(route["artifact_root"])).resolve() != Path(root).resolve():
        raise ProducerError("route-artifact-root-mismatch")
    if route["effective_intensity"] not in INTENSITIES:
        raise ProducerError("route-invalid", "effective_intensity")
    if route.get("route_hash") != route_identity.route_hash(route):
        raise ProducerError("route-invalid", "stale or modified route hash")
    if "resplit_cycle_key" in route:
        nonce = route.get("resplit_route_nonce")
        if not isinstance(nonce, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", nonce):
            raise ProducerError("route-invalid", "resplit route nonce")
        expected_route_id = "rt-" + nonce.split(":", 1)[-1][:16]
    else:
        expected_route_id = "rt-" + str(route["route_hash"]).split(":", 1)[-1][:16]
    if route.get("route_id") != expected_route_id:
        raise ProducerError("route-invalid", "route id/hash mismatch")
    if "slug" in route:
        if not isinstance(route.get("slug"), str) or not isinstance(route.get("slug_truncated"), bool):
            raise ProducerError("route-invalid", "slug metadata")
        try:
            canonical_slug, _truncated = artifact_locator.slugify(route["slug"])
        except artifact_locator.LocatorError as exc:
            raise ProducerError("route-invalid", "slug metadata") from exc
        if canonical_slug != route["slug"]:
            raise ProducerError("route-invalid", "slug is not canonical")
    elif "slug_truncated" in route:
        raise ProducerError("route-invalid", "slug metadata incomplete")
    if expected_identity is not None and (
            route.get("route_id") != expected_identity.get("route_id")
            or route.get("route_hash") != expected_identity.get("route_hash")):
        raise ProducerError("completion-route-hash-mismatch", str(expected_identity.get("route_id", "")))
    return route


def route_is_closed(root: Path, route: Mapping[str, Any]) -> bool:
    try:
        outcome = artifact_lifecycle.canonical_outcome_path(root, route["route_id"])
    except artifact_lifecycle.LifecycleError:
        return False
    return outcome.is_file()


def _route_node(route: Mapping[str, Any], node_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not node_id or node_id == "-":
        return None
    for row in route.get("nodes", []):
        if isinstance(row, dict) and row.get("id") == node_id:
            return row
    raise ProducerError("route-node-unknown", node_id)


# ---------------------------------------------------------------------------
# campaign records
# ---------------------------------------------------------------------------


def _campaign_path(root: Path, campaign_id: str,
                   record: Optional[Mapping[str, Any]] = None, *, creating: bool = False) -> Path:
    if creating and record is not None and record.get("campaign_id") == campaign_id and record.get("locator"):
        # A campaign directory being created does not exist yet, so any
        # lookup would fail and fall through to a needless lenient full scan.
        # The record's own `locator` is authoritative at creation time.
        try:
            campaigns = artifact_locator.safe_child(root, root, "campaigns")
            return artifact_locator.safe_child(root, campaigns, record["locator"]) / "campaign.json"
        except artifact_locator.LocatorError as exc:
            raise ProducerError("record-locator-invalid", exc.detail or exc.code) from exc
    return campaign_dir(root, campaign_id, record) / "campaign.json"


def read_campaign(root: Path, campaign_id: str) -> Optional[Dict[str, Any]]:
    if not artifact_identity.is_well_formed(campaign_id, "campaign"):
        return None
    found = artifact_locator.locate(root, campaign_id)
    if found is not None:
        record = _read_json(found / "campaign.json")
        if record is not None and record.get("campaign_id") == campaign_id:
            return artifact_campaign.fold_campaign(root, found / "campaign.json", record)
    fallback = _read_json(Path(root) / "campaigns" / campaign_id / "campaign.json")
    if fallback is not None and fallback.get("campaign_id") == campaign_id:
        return artifact_campaign.fold_campaign(root, Path(root) / "campaigns" / campaign_id / "campaign.json", fallback)
    return None


def find_campaign_by_key(root: Path, key: str) -> Optional[Dict[str, Any]]:
    return next((row for row in _campaigns_by_key(root, key) if row.get("state") == "active"), None)


def _campaigns_by_key(root: Path, key: str) -> List[Dict[str, Any]]:
    campaigns = Path(root) / "campaigns"
    if not campaigns.is_dir():
        return []
    rows = []
    for entry in artifact_locator.iter_campaign_dirs(root):
        record = _read_json(entry / "campaign.json")
        if record and record.get("key") == key:
            folded = artifact_campaign.fold_campaign(root, entry / "campaign.json", record)
            folded["_campaign_path"] = str(entry / "campaign.json")
            rows.append(folded)
    return rows


def classify_campaign_key(rows: Sequence[Mapping[str, Any]], key: str) -> Dict[str, Any]:
    matches = [row for row in rows if row.get("key") == key]
    invalid = [row for row in matches if row.get("state") == "invalid"]
    if invalid:
        return {"mode": "blocked", "code": invalid[0].get("state_error") or "campaign-state-invalid"}
    active = [row for row in matches if row.get("state") == "active"]
    closed = [row for row in matches if row.get("state") == "satisfied"]
    dead = [row for row in matches if row.get("state") in {"abandoned", "superseded"}]
    if len(closed) > 1:
        return {"mode": "blocked", "code": "campaign-key-reopen-ambiguous"}
    if len(active) > 1:
        return {"mode": "blocked", "code": "campaign-key-ambiguous"}
    if active:
        return {"mode": "join", "campaign_id": active[0].get("campaign_id")}
    if closed:
        return {"mode": "reopen", "campaign_id": closed[0].get("campaign_id")}
    if dead:
        return {"mode": "blocked", "code": "campaign-not-active"}
    return {"mode": "create", "campaign_id": None}


def _write_campaign(root: Path, record: Dict[str, Any], *, exclusive: bool) -> None:
    path = _campaign_path(root, record["campaign_id"], record, creating=exclusive)
    artifact_campaign.check_campaign_write(root, path, record)
    _ensure_dir(path.parent)
    if exclusive:
        _write_exclusive(path, _json_bytes(record))
    else:
        _write_atomic(path, _json_bytes(record))


# ---------------------------------------------------------------------------
# activate / status
# ---------------------------------------------------------------------------


def activate(
    root: Path,
    *,
    repository_id: str,
    artifact_root_id: str,
    w7: Optional[Mapping[str, Any]] = None,
    approval_receipt_sha256: Optional[str] = None,
    activation_kind: str = "approval",
    adopt_existing_identity: bool = False,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    root = Path(root).resolve()
    if not artifact_identity.is_well_formed(repository_id, "repository"):
        raise ProducerError("identity-malformed", "repository_id")
    if not artifact_identity.is_well_formed(artifact_root_id, "artifact_root"):
        raise ProducerError("identity-malformed", "artifact_root_id")
    if activation_kind not in ACTIVATION_KINDS:
        raise ProducerError("activation-kind-unknown", activation_kind)
    requested_ids = (repository_id, artifact_root_id)
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        existing = artifact_lifecycle.read_root_identity(root)
        if existing is None:
            payload = artifact_identity.RootIdentity(
                schema_version=1,
                artifact_root_id=artifact_root_id,
                repository_id=repository_id,
                issued_at=_rfc3339(now),
                producer_contract_version=artifact_manifest.CONTRACT_VERSION,
            ).to_payload()
            identity_path = artifact_admission._root_identity_path(root)
            _ensure_dir(identity_path.parent)
            _write_exclusive(identity_path, _json_bytes(payload), 0o600)
            identity = artifact_identity.RootIdentity.parse(payload)
            identity_state = "created"
        else:
            if adopt_existing_identity:
                repository_id = existing.repository_id
                artifact_root_id = existing.artifact_root_id
            elif (existing.repository_id, existing.artifact_root_id) != requested_ids:
                raise ProducerError("identity-conflict", "root identity already frozen with other ids")
            identity = existing
            identity_state = "adopted" if (adopt_existing_identity and
                (existing.repository_id, existing.artifact_root_id) != requested_ids) else "matched"
        current = read_cutover(root)
        if current.get("state") == "active":
            if current.get("identity", {}).get("artifact_root_id") != artifact_root_id:
                raise ProducerError("cutover-identity-conflict")
            return {"status": "already-active", "cutover": current, "identity": identity_state}
        body = {
            "schema_version": 1,
            "contract": CONTRACT,
            "state": "active",
            "activated_at": _rfc3339(now),
            "identity": {"repository_id": identity.repository_id, "artifact_root_id": identity.artifact_root_id},
            "w7": dict(w7 or {}),
            "approval_receipt_sha256": approval_receipt_sha256,
            "activation_kind": activation_kind,
        }
        _ensure_dir(producer_dir(root))
        _write_exclusive(cutover_path(root), _json_bytes(body), 0o600)
        return {"status": "activated", "cutover": body, "identity": identity_state}
    finally:
        artifact_admission._release_lock(root, lock_fd)


def status(root: Path) -> Dict[str, Any]:
    root = Path(root).resolve()
    identity = artifact_lifecycle.read_root_identity(root)
    records = list_cycle_records(root)
    counts: Dict[str, int] = {}
    for row in records:
        counts[row.get("state", "?")] = counts.get(row.get("state", "?"), 0) + 1
    journal_dir = producer_dir(root) / "journal"
    pending = sorted(p.stem for p in journal_dir.glob("*.json")) if journal_dir.is_dir() else []
    klass = classify_root(root)
    fallback = legacy_fallback_state(root, classification=klass)
    return {
        "artifact_root": str(root),
        "cutover": read_cutover(root),
        "identity": identity.to_payload() if identity else None,
        "cycle_counts": counts,
        "open_cycles": [r["cycle_id"] for r in records if r.get("state") == "open"],
        "pending_journals": pending,
        "root_classification": klass["state"],
        "activation_kind": read_cutover(root).get("activation_kind", "approval") if klass["state"] == "active" else None,
        "legacy_fallback": fallback,
    }


# ---------------------------------------------------------------------------
# begin
# ---------------------------------------------------------------------------


# SD-163: read-only lookup of a prior cycle's same-named output. Every failure
# below is "not found"; nothing here refuses, gates, or writes.
INPUT_SOURCE_MAX_DEPTH = 6
INPUT_SOURCE_MAX_ENTRIES = 4096
INPUT_SOURCE_MAX_CANDIDATES = 16


def _input_target(name: Any) -> Optional[Tuple[Tuple[str, ...], bool]]:
    """Return (path components, is_dir) for a declared input, or None when it is not a plain path."""
    if not isinstance(name, str):
        return None
    is_dir = name.endswith("/**")
    parts = tuple((name[:-3] if is_dir else name).split("/"))
    if any(part in ("", ".", "..") or any(char in part for char in "*?[<>") for part in parts):
        return None
    return parts, is_dir


def _cycle_artifacts_dir(root: Path, cycle_id: str) -> Optional[Path]:
    record = read_cycle_record(root, cycle_id)
    if record is None:
        return None
    artifacts = cycle_dir(root, record["campaign_id"], cycle_id, record) / "artifacts"
    if artifacts.is_symlink() or not artifacts.is_dir():
        return None
    resolved = artifacts.resolve(strict=True)
    return resolved if resolved.is_relative_to(Path(root).resolve()) else None


def _scan_cycle_artifacts(artifacts: Path) -> Optional[List[Tuple[str, bool]]]:
    """List (relative posix path, is_dir) below artifacts/; symlinks are skipped, caps give None."""
    found: List[Tuple[str, bool]] = []
    stack = [(artifacts, 0)]
    visited = 0
    while stack:
        directory, depth = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                visited += 1
                if visited > INPUT_SOURCE_MAX_ENTRIES:
                    return None
                if entry.is_symlink():
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                if not is_dir and not entry.is_file(follow_symlinks=False):
                    continue
                found.append((Path(entry.path).relative_to(artifacts).as_posix(), is_dir))
                if is_dir and depth < INPUT_SOURCE_MAX_DEPTH:
                    stack.append((Path(entry.path), depth + 1))
    return found


def _session_cycle_ids(root: Path, campaign: Mapping[str, Any], route_chain_identity,
                       begin_cycle: Mapping[str, str]) -> set:
    """Cycles of the routes this session composed for the campaign, read from its route-chain ledger.

    A ledger line names a route, never a cycle. A cycle record names its begin route, so a line
    maps to a cycle directly when that route began one, and through its verified lineage when it is
    a continuation of a route that did. The cycle's own audit copy of rebound routes is not read.
    """
    try:
        harness, session_id = route_chain_identity[:2]
        tools_dir = Path(__file__).resolve().parents[1] / "tools"
        if str(tools_dir) not in sys.path:
            sys.path.insert(0, str(tools_dir))
        from fleet import route_chain
        if route_chain.WRITER_SUPPORT.get(harness) != "env":
            return set()
        resolved = Path(root).resolve()
        composed = {line["route_id"]
                    for line in route_chain.read_tail(harness, session_id, max_bytes=1024 * 1024)
                    if line.get("event") in route_chain.COMPOSING_EVENTS
                    and line.get("campaign_key") == campaign.get("key")
                    and Path(str(line.get("artifact_root") or "")).resolve() == resolved}
    except Exception:
        return set()
    cycles = set()
    for route_id in composed:
        found = begin_cycle.get(route_id)
        if found is None:
            try:
                route = load_route(root, route_lineage.canonical_route_path(root, route_id))
                for ancestor in route_lineage.verified_route_lineage(route, artifact_root=root):
                    found = found or begin_cycle.get(ancestor["route_id"])
            except Exception:
                continue
        if found is not None:
            cycles.add(found)
    return cycles


def same_flow_source_cycle(root: Path, campaign_id: str, *, capability: str,
                           route_chain_identity=None, before: Optional[str] = None) -> Optional[str]:
    """The most recent cycle of the same flow, or None; never another flow's cycle.

    Order: the composing session's own route cycle in this campaign, then the
    latest cycle whose route capability is ``capability``. ``before`` keeps only
    cycles that started earlier than that cycle.
    """
    campaign = read_campaign(root, campaign_id)
    cycles = (campaign or {}).get("cycles")
    if (not campaign or campaign.get("degraded") is True or campaign.get("key") == UNASSIGNED_KEY
            or not isinstance(cycles, list) or not cycles):
        return None
    if before is not None:
        if before not in cycles:
            return None
        cycles = cycles[:cycles.index(before)]
    records = {row.get("cycle_id"): row for row in list_cycle_records(root)
               if row.get("campaign_id") == campaign_id and row.get("cycle_id") in cycles}
    begin_cycle = {rec["route_id"]: cycle_id for cycle_id, rec in records.items() if rec.get("route_id")}
    session_cycles = (_session_cycle_ids(root, campaign, route_chain_identity, begin_cycle)
                      if route_chain_identity else set())
    for cycle_id in reversed(cycles):
        if cycle_id in session_cycles:
            return cycle_id
    for cycle_id in reversed(cycles):
        rec = records.get(cycle_id)
        route_capability = rec.get("route_capability") if rec else None
        if not route_capability and rec and rec.get("route_file"):
            try:
                route_file = Path(rec["route_file"]).resolve(strict=True)
                if route_file.is_relative_to(Path(root).resolve()):
                    route_capability = (_read_json(route_file) or {}).get("capability")
            except Exception:
                pass
        if route_capability == capability:
            return cycle_id
    return None


def input_source_cycle(root: Path, *, parent_cycle_id: Optional[str] = None,
                       campaign_key: Optional[str] = None, capability: Optional[str] = None,
                       route_chain_identity=None) -> Optional[str]:
    """The one source cycle: the parent, else the joined campaign's most recent cycle."""
    if parent_cycle_id:
        return parent_cycle_id
    if not campaign_key or campaign_key == UNASSIGNED_KEY:
        return None
    decision = classify_campaign_key(list_campaign_summaries(root, active_only=False), campaign_key)
    if decision.get("mode") not in ("join", "reopen"):
        return None
    return (same_flow_source_cycle(root, decision["campaign_id"], capability=capability,
                                   route_chain_identity=route_chain_identity)
            if capability else None)


def input_source_finder(root: Path, *, parent_cycle_id: Optional[str] = None,
                        campaign_key: Optional[str] = None, capability: Optional[str] = None,
                        route_chain_identity=None):
    """Return find(name) -> {"cycle_id", "path"} | None, resolving the source lazily and once."""
    root = Path(root)
    memo: Dict[str, Optional[Dict[str, str]]] = {}
    loaded: Dict[str, Any] = {}

    def lookup(name: str) -> Optional[Dict[str, str]]:
        if not loaded:
            loaded["entries"] = None
            cycle_id = input_source_cycle(root, parent_cycle_id=parent_cycle_id, campaign_key=campaign_key,
                                          capability=capability, route_chain_identity=route_chain_identity)
            artifacts = _cycle_artifacts_dir(root, cycle_id) if cycle_id else None
            if artifacts is not None:
                loaded.update(cycle_id=cycle_id, artifacts=artifacts,
                              entries=_scan_cycle_artifacts(artifacts))
        target = _input_target(name)
        if target is None or loaded["entries"] is None:
            return None
        parts, is_dir = target
        rels = [rel for rel, entry_dir in loaded["entries"]
                if entry_dir == is_dir and tuple(rel.split("/"))[-len(parts):] == parts]
        if not rels or len(rels) > INPUT_SOURCE_MAX_CANDIDATES:
            return None
        # Shortest cycle-relative path by character length, then lexicographic; never by recency.
        chosen = loaded["artifacts"] / min(rels, key=lambda rel: (len(rel), rel))
        resolved = chosen.resolve(strict=True)
        if not resolved.is_relative_to(loaded["artifacts"]):
            return None
        return {"cycle_id": loaded["cycle_id"], "path": resolved.relative_to(root.resolve()).as_posix()}

    def find(name: str) -> Optional[Dict[str, str]]:
        if name not in memo:
            try:
                memo[name] = lookup(name)
            except Exception:  # SD-163: every lookup failure is "not found"
                memo[name] = None
        return dict(memo[name]) if memo[name] else None

    return find


def _composing_anchor(route_id, root=None):
    """The depth-0 session whose route-chain ledger says it composed ``route_id``, else None.

    A route file is written once, so its mtime bounds the ledgers that can hold the compose line.
    """
    try:
        created = None
        try:
            created = (Path(root) / ".runtime" / "routes" / f"{route_id}.json").stat().st_mtime if root else None
        except OSError:
            pass
        tools_dir = Path(__file__).resolve().parents[1] / "tools"
        if str(tools_dir) not in sys.path:
            sys.path.insert(0, str(tools_dir))
        from fleet import route_chain
        return route_chain.composing_anchor(route_id, not_before=created)
    except Exception:
        return None


def _parent_output_dir(root: Path, record: Mapping[str, Any], route: Optional[Mapping[str, Any]],
                       route_chain_identity=None) -> Optional[str]:
    """Absolute artifacts/ of the source cycle: parent, else sealed input_sources, else the campaign's previous cycle."""
    try:
        cycle_id = record.get("parent_cycle_id")
        for node in (route or {}).get("nodes") or []:
            if cycle_id:
                break
            sources = node.get("input_sources") if isinstance(node, Mapping) else None
            for source in (sources.values() if isinstance(sources, Mapping) else ()):
                cycle_id = cycle_id or (source.get("cycle_id") if isinstance(source, Mapping) else None)
        cycle_id = cycle_id or same_flow_source_cycle(
            root, record["campaign_id"], capability=(route or {}).get("capability") or record.get("route_capability"),
            route_chain_identity=route_chain_identity, before=record["cycle_id"])
        directory = _cycle_artifacts_dir(root, cycle_id) if cycle_id and cycle_id != record["cycle_id"] else None
        return str(directory) if directory else None
    except Exception:  # SD-163: every lookup failure is "not found"
        return None


def _env_for(root: Path, record: Mapping[str, Any], route: Optional[Mapping[str, Any]] = None,
             route_chain_identity=None) -> Dict[str, str]:
    directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
    env = {
        "AGENT_ARTIFACT_ROOT": str(root),
        "AGENT_ARTIFACT_CAMPAIGN_ID": record["campaign_id"],
        "AGENT_ARTIFACT_CYCLE_ID": record["cycle_id"],
        "AGENT_ARTIFACT_PRODUCER_ID": record["producer_id"],
        "AGENT_ARTIFACT_CYCLE_DIR": str(directory),
        "AGENT_ARTIFACT_OUTPUT_DIR": str(directory / "artifacts"),
    }
    # This is an already-declared producer context. Carrying it through the
    # existing artifact env lets the next explicit same-campaign cycle join
    # without an agent remembering a group ID or changing route lineage.
    import artifact_workflow_groups
    group_id = artifact_workflow_groups.group_for_cycle(root, record["campaign_id"], record["cycle_id"])
    if group_id:
        env["AGENT_ARTIFACT_WORKFLOW_GROUP_ID"] = group_id
    if route_chain_identity is None and route:
        route_chain_identity = _composing_anchor(route.get("route_id"), root)
    parent_output = _parent_output_dir(root, record, route, route_chain_identity)
    if parent_output:
        env["AGENT_ARTIFACT_PARENT_OUTPUT_DIR"] = parent_output
    return env


def bind_owner_launch(args, jobs: Path, *, environ=None) -> Optional[Dict[str, Any]]:
    """Publish the owner binding at the registered launch seam (§13.53.3).

    The wrapper has claimed the owner row but has not spawned it yet. Reusing
    begin's resume-only success path preserves every admission check and keeps
    settlement read-only.
    """
    environ = os.environ if environ is None else environ
    if not dispatch_contract.is_runtime_owner_launch(args) or not environ.get("AGENT_ARTIFACT_CYCLE_ID"):
        return None
    owner_binding = getattr(args, "owner_route_binding", None)
    route_file = (owner_binding.get("route_file") if isinstance(owner_binding, Mapping)
                  else getattr(owner_binding, "route_file", None)) or getattr(args, "route_file", None)
    if not route_file:
        return None
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
        root = Path(route["artifact_root"]).resolve()
        validated_route = load_route(root, Path(route_file))
        existing_open = route_cycle_for(root, validated_route)
        env_cycle = environ["AGENT_ARTIFACT_CYCLE_ID"]
        if existing_open is not None and existing_open["cycle_id"] != env_cycle:
            raise ProducerError("producer-binding-mismatch",
                                f"launch-cycle={env_cycle} bound={existing_open['cycle_id']}")
        result = begin(root, route_file=Path(route_file), capability=route["capability"],
                       intensity=route["effective_intensity"], require_cycle=True,
                       jobs=Path(jobs), owner_attempt_id=args.attempt_id, resume_only=True)
        if result.get("cycle_id") != env_cycle:
            raise ProducerError("producer-binding-mismatch",
                                f"launch-cycle={env_cycle} bound={result.get('cycle_id', '')}")
        return result
    except ProducerError:
        raise
    except Exception as exc:
        detail = str(getattr(exc, "detail", "") or exc)
        raise ProducerError(getattr(exc, "code", type(exc).__name__), detail) from exc


def prepare_route_artifact_env(route_file: Path, *, start: bool, jobs: Path) -> Dict[str, str]:
    """Resolve the route's own output context; callers need not copy begin's env.

    Start owns idempotent preparation. Readiness checks only read an existing
    cycle. No inherited cycle or 'latest' directory participates in selection.
    """
    raw = _read_json(route_file)
    if not isinstance(raw, dict) or not raw.get("artifact_root"):
        raise ProducerError("route-artifact-root-missing", str(route_file))
    root = Path(raw["artifact_root"]).resolve()
    route = load_route(root, route_file)
    if start:
        context = (route.get("work_request") or {}).get("workflow_group_context")
        selection = {}
        if context:
            from work_start import group_context_matches_campaign
            if not group_context_matches_campaign(route, context):
                raise ProducerError("workflow-group-campaign-mismatch", context["campaign_id"])
            selection = {"workflow_group_id": context["group_id"]}
        return begin(root, route_file=route_file, capability=route["capability"],
                     intensity=route["effective_intensity"], require_cycle=True, jobs=jobs,
                     **selection)["env"]
    record = route_cycle_for(root, route)
    if record is None:
        return {"AGENT_ARTIFACT_ROOT": str(root), **{name: "" for name in (
            "AGENT_ARTIFACT_CAMPAIGN_ID", "AGENT_ARTIFACT_CYCLE_ID", "AGENT_ARTIFACT_PRODUCER_ID",
            "AGENT_ARTIFACT_CYCLE_DIR", "AGENT_ARTIFACT_OUTPUT_DIR")}}
    return _env_for(root, record, route)


def _route_naming(
    route: Mapping[str, Any], campaign: Optional[Mapping[str, Any]],
    *, title: Optional[str], goal: Optional[str], root: Optional[Path] = None,
) -> Tuple[str, str, str, bool]:
    """Return canonical slug, display title, source, and truncation fact.

    Slugless records are pre-W7I routes.  During the explicit transition
    window they remain usable and are marked so migration can distinguish
    them from routes that sealed a slug.  D-91 closes that window when the
    root's relayout completes; from then on a slugless route is a typed
    refusal, never a silent derived name.
    """
    route_slug = route.get("slug")
    if isinstance(route_slug, str) and route_slug:
        slug, normalized_truncated = artifact_locator.slugify(route_slug)
        return slug, str(title or slug), "route", bool(route.get("slug_truncated")) or normalized_truncated
    if root is not None and transition_window_closed(root):
        raise ProducerError("route-slug-missing", str(route.get("route_id") or "route"))
    candidates = []
    if campaign is not None:
        candidates.extend((campaign.get("slug"), campaign.get("title")))
    candidates.extend((title, goal))
    if campaign is not None:
        candidates.append(campaign.get("goal"))
    raw = next((str(value) for value in candidates if isinstance(value, str) and value.strip()), "unnamed")
    slug, truncated = artifact_locator.slugify(raw, fallback="unnamed")
    display_title = str(title or (campaign or {}).get("title") or slug)
    return slug, display_title, "derived-legacy-route", truncated


UNASSIGNED_KEY = "_unassigned"


def _campaign_naming(campaign_key: Optional[str]) -> Tuple[str, str, str, bool]:
    """Return (slug, title, slug_source, truncated) for a *campaign* record.

    A campaign is the work stream the agent proposed; its locator and title
    come from that proposal, never from the first route that happened to
    join it.  Deriving the campaign name from the first cycle's slug produced
    ``<date>_tf-rehancer-analysis-cx`` for the stream ``tf-rehancer-icassp``
    (TF-Rehancer 2026-09-15).  The reserved `_unassigned` container keeps its
    fixed name.
    """
    if campaign_key is None:
        return "unassigned", UNASSIGNED_KEY, "reserved", False
    slug, truncated = artifact_locator.slugify(campaign_key, fallback="stream")
    return slug, campaign_key, "campaign-key", truncated


RECOVERED_SOURCE = "retirement-backup-mtime"


def default_backup_store() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "hearting" / "artifact-retirement"


def _retirement_mtime_index(run_dir: Path) -> Dict[str, int]:
    """``source path -> mtime`` of a retirement archive, cached beside it.

    The archive is the pre-migration bytes with their original mtimes (the
    W7C/W7G copies lost theirs). Listing a multi-GB gzip means decompressing
    it once; the cache is keyed by the seal's archive digest."""
    import tarfile
    seal = _read_json(run_dir / "backup-seal.json") or {}
    cache_path = run_dir / "mtime-index.json"
    cached = _read_json(cache_path)
    if (isinstance(cached, dict) and cached.get("archive_sha256") == seal.get("archive_sha256")
            and isinstance(cached.get("members"), dict)):
        return {str(k): int(v) for k, v in cached["members"].items()}
    members: Dict[str, int] = {}
    with tarfile.open(run_dir / "retired-sources.tar.gz", "r:gz") as archive:
        for member in archive:
            if member.isfile():
                members[member.name] = int(member.mtime)
    try:
        _write_atomic(cache_path, _json_bytes({"schema_version": 1, "archive_sha256": seal.get("archive_sha256"),
                                               "members": members}))
    except OSError:
        pass  # the cache is a convenience; the archive stays the source
    return members


def _retirement_digest_index(store: Path, root_id: str) -> Tuple[Dict[str, Tuple[str, int, str]], List[str]]:
    """``sha256 -> (source path, mtime, run)`` over every retirement run of a root."""
    by_sha: Dict[str, Tuple[str, int, str]] = {}
    runs: List[str] = []
    base = store / root_id
    if not base.is_dir():
        return by_sha, runs
    for run_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        manifest = run_dir / "retired-manifest.jsonl"
        if not manifest.is_file() or not (run_dir / "retired-sources.tar.gz").is_file():
            continue
        mtimes = _retirement_mtime_index(run_dir)
        runs.append(run_dir.name)
        for line in manifest.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            source, sha = row.get("source"), row.get("sha256")
            if isinstance(source, str) and isinstance(sha, str) and source in mtimes:
                by_sha.setdefault(sha, (source, mtimes[source], run_dir.name))
    return by_sha, runs


def recover_cycle_times(root: Path, *, backup_store: Optional[Path] = None,
                        apply: bool = False) -> Dict[str, Any]:
    """Recover the start time of cycles that only know their work's date.

    W7G resplit and W7H residue cycles were built from copies whose mtimes
    were the copy time, so their records carry a date (or the resplit run
    time) and no clock. The retirement backup of the same root keeps the
    original files with their original mtimes, and its manifest keys them by
    sha256. Each sealed artifact revision's ``content_digest`` therefore leads
    back to the original file; the earliest such mtime is the cycle's
    ``recovered_started_on`` (UTC, second precision), kept beside the
    untouched ``started_on`` with its evidence. An mtime is a *last* write:
    when the earliest one lands after the folder's date (a later bulk
    rewrite), it cannot be the start, so it is stored as evidence only
    (``recovered_earliest_write``) and the display keeps the date. Earlier
    than the folder date means the folder date was wrong (a copy date) and
    the recovered time wins. Dry run by default; ``apply``
    holds the producer admission lock, journals every record pre-image under
    ``.runtime/artifact-producer/v1/time-recovery/`` and rebuilds the indexes.
    """
    root = Path(root).resolve()
    store = Path(backup_store) if backup_store is not None else default_backup_store()
    identity = artifact_lifecycle.read_root_identity(root)
    if identity is None:
        raise ProducerError("root-identity-missing", str(root))
    by_sha, runs = _retirement_digest_index(store, identity.artifact_root_id)
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT) if apply else None
    journal: List[Dict[str, Any]] = []
    try:
        rows: List[Dict[str, Any]] = []
        for record_path in sorted((producer_dir(root) / "cycles").glob("cyc_*.json")):
            record = _read_json(record_path)
            if not isinstance(record, dict):
                continue
            if not (record.get("derived_from_cycle_id") or record.get("started_on_source")):
                continue  # a producer-born cycle already has its real clock
            cycle_id = record["cycle_id"]
            row: Dict[str, Any] = {"cycle_id": cycle_id, "locator": record.get("locator"),
                                   "previous": record.get("recovered_started_on")}
            try:
                directory = artifact_locator.resolve_path(root, cycle_id)
            except artifact_locator.LocatorError as exc:
                rows.append({**row, "action": "unresolved", "detail": exc.code}); continue
            if directory is None:
                rows.append({**row, "action": "unresolved", "detail": "no-directory"}); continue
            manifest = _read_json(directory / "manifest.json")
            if not isinstance(manifest, dict):
                rows.append({**row, "action": "no-manifest"}); continue
            digests = [str(rev.get("content_digest", "")).split(":", 1)[-1]
                       for rev in manifest.get("artifact_revisions", []) if isinstance(rev, dict)]
            hits = [by_sha[d] for d in digests if d in by_sha]
            row.update({"matched": len(hits), "total": len(digests)})
            if not digests:
                rows.append({**row, "action": "no-artifacts"}); continue
            if not hits:
                rows.append({**row, "action": "no-match"}); continue
            earliest = min(h[1] for h in hits)
            latest = max(h[1] for h in hits)
            recovered = _rfc3339(earliest)
            folder_date = str(record.get("locator") or "")[:10]
            usable = not folder_date or recovered[:10] <= folder_date
            evidence = {"matched": len(hits), "total": len(digests), "earliest": recovered,
                        "latest": _rfc3339(latest), "backup_runs": sorted({h[2] for h in hits})}
            row.update({"recovered_started_on": recovered if usable else None,
                        "earliest_write": recovered, "latest_write": evidence["latest"],
                        "backup_run": evidence["backup_runs"],
                        "folder_date_agrees": recovered[:10] == folder_date,
                        "display": "recovered" if usable else "evidence-only"})
            if (record.get("recovered_earliest_write") == recovered
                    and record.get("recovered_started_on") == (recovered if usable else None)):
                rows.append({**row, "action": "already"}); continue
            row["action"] = "recovered" if apply else "would-recover"
            if apply:
                journal.append({"cycle_id": cycle_id, "pre": dict(record)})
                record["recovered_earliest_write"] = recovered
                record["recovered_started_on_evidence"] = evidence
                if usable:
                    record["recovered_started_on"] = recovered
                    record["recovered_started_on_source"] = RECOVERED_SOURCE
                else:
                    record.pop("recovered_started_on", None)
                    record.pop("recovered_started_on_source", None)
                _write_cycle_record(root, record, exclusive=False)
            rows.append(row)
        counts: Dict[str, int] = {}
        for row in rows:
            counts[row["action"]] = counts.get(row["action"], 0) + 1
        result: Dict[str, Any] = {"status": "applied" if apply else "dry-run", "artifact_root": str(root),
                                  "backup_store": str(store), "backup_runs": runs, "counts": counts,
                                  "cycles": rows}
        if apply and journal:
            journal_dir = producer_dir(root) / "time-recovery"
            _ensure_dir(journal_dir)
            journal_file = journal_dir / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                                          + "-" + os.urandom(3).hex() + ".jsonl")
            _write_exclusive(journal_file, "".join(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
                                                    for entry in journal).encode("utf-8"), 0o600)
            result["journal"] = str(journal_file)
            artifact_locator.rebuild_indexes(root)
        return result
    finally:
        if lock_fd is not None:
            artifact_admission._release_lock(root, lock_fd)


def backfill_cycle_bindings(root: Path, *, apply: bool = False) -> Dict[str, Any]:
    """Add ``started_on`` to readable-layout ``.cycle.json`` bindings that predate it.

    The value follows ``artifact_locator.display_started_on``: a resplit cycle's
    work date (D-79 ``resplit_started_on``, date-only), else the record's own
    ``started_on``, else the sealed manifest's. Nothing is estimated from
    directory names or mtimes: the field is display data and the record wins
    (D-88). A binding that
    already carries a different time is reported as ``conflict`` and left
    alone. Dry run by default; ``apply`` holds the producer admission lock,
    replaces each binding atomically and rebuilds the indexes. Idempotent.
    """
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT) if apply else None
    try:
        rows: List[Dict[str, Any]] = []
        counts: Dict[str, int] = {}
        for campaign_path in artifact_locator.iter_campaign_dirs(root):
            for entry, layout in artifact_locator.iter_cycle_dirs(campaign_path):
                if layout != "readable":
                    continue
                rel = entry.relative_to(root).as_posix()
                try:
                    binding = artifact_locator.read_cycle_binding(entry)
                except artifact_locator.LocatorError as exc:
                    rows.append({"path": rel, "action": "invalid", "detail": exc.code})
                    continue
                if binding is None:
                    rows.append({"path": rel, "action": "no-binding"})
                    continue
                cycle_id = binding["cycle_id"]
                record = artifact_locator.read_cycle_record(root, cycle_id) or {}
                manifest = artifact_locator._read_json(entry / "manifest.json") or {}
                started_on = artifact_locator.display_started_on(record, manifest)
                source = None
                if started_on is not None:
                    # `display_started_on` may have trimmed a placeholder clock; match on the prefix.
                    source = ("record:resplit_started_on" if str(record.get("resplit_started_on")).startswith(started_on)
                              else "record" if str(record.get("started_on")).startswith(started_on) else "manifest")
                row: Dict[str, Any] = {"cycle_id": cycle_id, "path": rel, "source": source,
                                       "started_on": started_on}
                if started_on is None:
                    row["action"] = "missing"
                elif "started_on" in binding:
                    row["action"] = "present" if binding["started_on"] == started_on else "conflict"
                    if row["action"] == "conflict":
                        row["binding_started_on"] = binding["started_on"]
                else:
                    row["action"] = "added" if apply else "would-add"
                    if apply:
                        data = artifact_locator.cycle_binding_bytes(
                            binding["campaign_id"], cycle_id, started_on=started_on)
                        _write_atomic(entry / artifact_locator.CYCLE_BINDING, data)
                rows.append(row)
        for row in rows:
            counts[row["action"]] = counts.get(row["action"], 0) + 1
        if apply and counts.get("added"):
            artifact_locator.rebuild_indexes(root)
        return {"status": "applied" if apply else "dry-run", "artifact_root": str(root),
                "counts": counts, "cycles": rows}
    finally:
        if lock_fd is not None:
            artifact_admission._release_lock(root, lock_fd)


def list_campaign_summaries(root: Path, *, active_only: bool = True) -> List[Dict[str, Any]]:
    """Cheap, read-only listing of the root's campaigns for callers that
    must show the agent which work streams already exist (compose).

    Each row uses the same validated campaign event fold as admission.
    """
    root = Path(root)
    rows: List[Dict[str, Any]] = []
    for entry in artifact_locator.iter_campaign_dirs(root):
        record = _read_json(entry / "campaign.json")
        if not record or not isinstance(record.get("campaign_id"), str):
            continue
        try:
            state = artifact_campaign.campaign_state(root, entry / "campaign.json", record).state
            state_error = None
        except artifact_campaign.CampaignError as exc:
            state, state_error = "invalid", exc.code
        if active_only and state != "active":
            continue
        cycles = record.get("cycles")
        rows.append({
            "campaign_id": record["campaign_id"],
            "key": record.get("key"),
            "title": record.get("title"),
            "goal": record.get("goal"),
            "locator": entry.name,
            "state": state,
            **({"state_error": state_error} if state_error else {}),
            "degraded": record.get("degraded") is True,
            "cycle_count": len(cycles) if isinstance(cycles, list) else 0,
            "created_on": str(record.get("created_on") or ""),
        })
    rows.sort(key=lambda row: (row["created_on"], str(row["key"])), reverse=True)
    return rows


def _campaign_degradation(campaign: Mapping[str, Any]) -> Dict[str, Any]:
    if campaign.get("degraded") is True:
        return {"degraded": True, "degraded_reason": campaign.get("degraded_reason", "campaign-unassigned")}
    return {}


def _begin_cycle_record(
    root: Path,
    *,
    route_file: Path,
    capability: str,
    intensity: str,
    node_id: Optional[str] = None,
    campaign_id: Optional[str] = None,
    campaign_key: Optional[str] = None,
    title: Optional[str] = None,
    goal: Optional[str] = None,
    parent_cycle_id: Optional[str] = None,
    workflow_group_id: Optional[str] = None,
    workflow_stage_label: Optional[str] = None,
    require_cycle: bool = False,
    shared_reference_pins: Optional[Sequence[Mapping[str, Any]]] = None,
    allocator: Optional[artifact_identity.IdAllocator] = None,
    now: Optional[float] = None,
    jobs: Optional[Path] = None,
    owner_attempt_id: Optional[str] = None,
    resume_only: bool = False,
) -> Dict[str, Any]:
    dispatch_terminal_commit.require_current_cleanup("producer-begin", jobs=jobs)
    root = Path(root).resolve()
    if capability not in ENTRY_CAPABILITIES + STAGE_CAPABILITIES + INTERNAL_CAPABILITIES:
        raise ProducerError("capability-unknown", capability)
    if intensity not in INTENSITIES:
        raise ProducerError("intensity-unknown", intensity)
    resolved_route_file = resolve_route_argument(root, Path(route_file))
    route = load_route(root, resolved_route_file)
    # A sealed proposal survives dispatch; CLI may confirm, never override it.
    for field, supplied in (("campaign_key", campaign_key), ("parent_cycle_id", parent_cycle_id)):
        if field in route and supplied is not None and supplied != route[field]:
            raise ProducerError("route-campaign-selection-conflict", field)
    campaign_key = route.get("campaign_key", campaign_key)
    parent_cycle_id = route.get("parent_cycle_id", parent_cycle_id)
    if campaign_key is not None and (not isinstance(campaign_key, str) or not _KEY_RE.fullmatch(campaign_key)):
        raise ProducerError("campaign-key-invalid", str(campaign_key))
    if parent_cycle_id is not None and not artifact_identity.is_well_formed(parent_cycle_id, "cycle"):
        raise ProducerError("parent-cycle-invalid", str(parent_cycle_id))
    route_capability = route["capability"]
    if capability in ENTRY_CAPABILITIES + INTERNAL_CAPABILITIES and route_capability != capability:
        raise ProducerError("route-capability-mismatch", f"{route_capability}!={capability}")
    if route["effective_intensity"] != intensity:
        raise ProducerError("route-intensity-mismatch", f"{route['effective_intensity']}!={intensity}")
    node = _route_node(route, node_id)
    if route_is_closed(root, route):
        raise ProducerError("route-already-closed", route["route_id"])
    alloc = allocator or artifact_identity.IdAllocator()
    klass = classify_root(root)
    if klass["state"] == "malformed":
        raise ProducerError("cutover-record-malformed", klass["reason"])
    if klass["state"] == "inactive-empty":
        if resume_only:
            raise ProducerError("producer-binding-required", "route-cycle-absent")
        # D-73: bootstrap-first identity. MUST stay above the admission lock at
        # :547 -- activate() acquires the same lock and would self-deadlock.
        activate(root,
                 repository_id=alloc.allocate("repository"),
                 artifact_root_id=alloc.allocate("artifact_root"),
                 activation_kind="bootstrap-empty-root",
                 adopt_existing_identity=True,
                 now=now)
    elif klass["state"] == "inactive-with-legacy":
        fallback = legacy_fallback_state(root, now=now, classification=klass)
        if _fallback_blocks(fallback):
            raise ProducerError("cutover-inactive-fallback-denied",
                                fallback["override"]["reason"] or "override-absent")
        if require_cycle:
            raise ProducerError("cutover-inactive", "activation required before cycle issuance")
        return {
            "status": "legacy-compat",
            "layout": "legacy",
            "route_id": route["route_id"],
            "reason": "cutover-inactive",
            "env": {"AGENT_ARTIFACT_ROOT": str(root)},
            "legacy_fallback": fallback,
        }
    # active, or just bootstrapped: fall through to the existing cycle path.
    identity = artifact_lifecycle.read_root_identity(root)
    if identity is None:
        raise ProducerError("root-identity-missing")
    # SD-120 A1: only registered owner/worker contexts participate.  The
    # canonical jobs path and attempt identity come from the runtime; callers
    # do not get a route/owner override surface.
    binding_jobs = Path(jobs or os.environ.get("AGENT_DISPATCH_JOBS", ""))
    if not binding_jobs.is_absolute() or not binding_jobs.is_file():
        binding_jobs = None  # type: ignore[assignment]
    binding_owner = owner_attempt_id or os.environ.get("AGENT_DISPATCH_ATTEMPT_ID", "")
    if node_id is not None:
        binding_owner = os.environ.get("AGENT_DISPATCH_PARENT_ATTEMPT_ID", binding_owner)
    owner_begin = node_id is None
    if binding_jobs is not None and binding_owner:
        try:
            owner_binding = dispatch_terminal_commit.validate_owner_route(
                jobs=binding_jobs, route_file=resolved_route_file, owner_attempt_id=binding_owner)
            existing_open = route_cycle_for(root, route)
            binding_path = dispatch_terminal_commit.producer_binding_path(
                root, owner_binding.route_id, binding_owner)
            if binding_path.exists():
                loaded = dispatch_terminal_commit.load_producer_binding(
                    artifact_root=root, route_id=owner_binding.route_id, owner_attempt_id=binding_owner)
                if existing_open is None or loaded.binding.get("cycle_id") != existing_open.get("cycle_id"):
                    raise dispatch_terminal_commit.TerminalCommitError("transaction-conflict", str(binding_path))
            elif not owner_begin and existing_open is None:
                raise dispatch_terminal_commit.TerminalCommitError("producer-binding-required", str(binding_path))
        except dispatch_terminal_commit.TerminalCommitError as exc:
            raise ProducerError(exc.code, exc.detail) from exc
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        # W7G owns a root-wide resplit from R2 through R3.  Check its atomic
        # claim while holding the same admission mutex used for locator and
        # campaign updates, so begin cannot race the gap before its first
        # journal is durable or lose a campaign cycles[] update.
        resplit_lock = producer_dir(root) / "resplit.lock"
        if resplit_lock.exists() or resplit_lock.is_symlink():
            detail = _read_json(resplit_lock)
            raise ProducerError("resplit-in-progress", json.dumps(detail or {}, sort_keys=True))
        resumable = route_cycle_for(root, route)
        if resume_only and resumable is None:
            raise ProducerError("producer-binding-required", "route-cycle-absent")
        campaign: Optional[Dict[str, Any]] = None
        parent = None
        campaign_reopen_event_id = None
        requested_selection = None
        if campaign_id:
            campaign = read_campaign(root, campaign_id)
            if campaign is None:
                raise ProducerError("campaign-unknown", campaign_id)
            requested_selection = {"by": "campaign_id", "value": campaign_id}
        elif campaign_key:
            keyed = _campaigns_by_key(root, campaign_key)
            choice = classify_campaign_key(keyed, campaign_key)
            if choice["mode"] == "blocked":
                raise ProducerError(choice["code"], campaign_key)
            campaign = next((row for row in keyed if row.get("campaign_id") == choice.get("campaign_id")), None)
            requested_selection = {"by": "campaign_key", "value": campaign_key}
        if parent_cycle_id:
            parent = read_cycle_record(root, parent_cycle_id)
            if parent is None or parent.get("state") not in {"open", "sealed"}:
                raise ProducerError("parent-cycle-not-joinable", parent_cycle_id)
            if campaign is not None and parent.get("campaign_id") != campaign["campaign_id"]:
                raise ProducerError("parent-cycle-campaign-mismatch", parent_cycle_id)
            if campaign is None:
                campaign = read_campaign(root, parent["campaign_id"])
                if campaign is None:
                    raise ProducerError("campaign-unknown", parent["campaign_id"])
                requested_selection = {"by": "parent_cycle", "value": parent_cycle_id}
        import artifact_workflow_groups
        # Ambient context is accepted only for the exact selected campaign;
        # the parent-cycle edge alone never selects or implies a group.
        inherited_group_id = (
            os.environ.get("AGENT_ARTIFACT_WORKFLOW_GROUP_ID")
            if campaign is not None and os.environ.get("AGENT_ARTIFACT_CAMPAIGN_ID") == campaign["campaign_id"]
            else None
        )
        selected_group_id = workflow_group_id or inherited_group_id
        if selected_group_id and owner_begin:
            if campaign is None:
                raise ProducerError("workflow-group-campaign-required")
            if workflow_group_id and inherited_group_id and workflow_group_id != inherited_group_id:
                raise ProducerError("workflow-group-context-conflict", workflow_group_id)
            try:
                early_title = (resumable.get("title") if resumable is not None
                               else _route_naming(route, campaign, title=title, goal=goal, root=root)[1])
                early_label = workflow_stage_label or artifact_workflow_groups.stage_label_from_title(early_title)
                artifact_workflow_groups.preflight_join_locked(
                    root, campaign["campaign_id"], selected_group_id, early_label,
                    cycle_id=resumable["cycle_id"] if resumable is not None else None)
            except artifact_workflow_groups.WorkflowGroupError as exc:
                raise ProducerError(exc.code, exc.detail) from exc
        if workflow_stage_label is not None:
            try:
                artifact_workflow_groups._text(workflow_stage_label, 40, "stage-label-invalid")
            except artifact_workflow_groups.WorkflowGroupError as exc:
                raise ProducerError(exc.code, exc.detail) from exc
        if campaign is not None:
            if campaign.get("state") == "satisfied":
                try:
                    reopened = artifact_campaign._reopen_locked(
                        root, _campaign_path(root, campaign["campaign_id"], campaign),
                        route_id=route["route_id"], requested_selection=requested_selection)
                except artifact_campaign.CampaignError as exc:
                    raise ProducerError(exc.code, exc.detail) from exc
                campaign_reopen_event_id = reopened.get("event_id")
                campaign = read_campaign(root, campaign["campaign_id"])
            if campaign is None or campaign.get("state") != "active":
                raise ProducerError("campaign-not-active", campaign_id or parent_cycle_id or campaign_key)
            if campaign_key is not None and campaign.get("key") != campaign_key:
                raise ProducerError("campaign-key-mismatch", campaign_key)
        # Idempotent per route: one open cycle per verified lineage (D-120). A
        # continuation resuming an ancestor's open cycle is the same idempotent
        # path with `rebound=True` and an extended audit record.
        if resumable is not None:
            record = resumable
            admission = cycle_route_admission(root, record, route)
            if not admission.allow:
                raise ProducerError(admission.reason, admission.detail)
            bound_campaign = read_campaign(root, record["campaign_id"])
            if bound_campaign is None or bound_campaign.get("state") != "active":
                raise ProducerError("campaign-not-active", record["campaign_id"])
            if ((campaign is not None and campaign["campaign_id"] != record["campaign_id"])
                    or (campaign_key is not None and campaign_key != bound_campaign.get("key"))
                    or (parent_cycle_id is not None and parent_cycle_id != record.get("parent_cycle_id"))):
                raise ProducerError("cycle-campaign-selection-conflict", record["cycle_id"])
            rebound = record.get("route_id") != route["route_id"]
            # D-120: only an owner `begin --route <continuation>` writes the
            # audit trail. A worker `begin --node` judges the same admission
            # (raised above) but never mutates the record.
            if rebound and owner_begin:
                _bind_cycle_route_locked(root, record, route)
            if selected_group_id and owner_begin:
                stage_label = workflow_stage_label or artifact_workflow_groups.stage_label_from_title(record.get("title"))
                try:
                    artifact_workflow_groups.join_at_begin_locked(
                        root, record["campaign_id"], record["cycle_id"], selected_group_id, stage_label)
                except artifact_workflow_groups.WorkflowGroupError as exc:
                    raise ProducerError(exc.code, exc.detail) from exc
            if binding_jobs is not None and binding_owner:
                try:
                    dispatch_terminal_commit.publish_producer_binding(
                        artifact_root=root, jobs=binding_jobs, route_file=resolved_route_file,
                        owner_attempt_id=binding_owner, cycle_id=record["cycle_id"],
                        owner_begin=owner_begin)
                except dispatch_terminal_commit.TerminalCommitError as exc:
                    raise ProducerError(exc.code, exc.detail) from exc
            title_updated = False
            if owner_begin and title is not None:
                # A repeated owner begin is the existing metadata edit surface.
                # Reread after a continuation bind to retain its route audit.
                current = read_cycle_record(root, record["cycle_id"])
                directory = cycle_dir(root, current["campaign_id"], current["cycle_id"], current)
                if current.get("state") == "open" and not (directory / "manifest.json").exists():
                    display_title = _route_naming(route, bound_campaign, title=title, goal=goal, root=root)[1]
                    if current.get("title") != display_title:
                        record = {**current, "title": display_title}
                        _write_cycle_record(root, record, exclusive=False)
                        title_updated = True
            return {
                "status": "resumed", "layout": "cycle", "campaign_id": record["campaign_id"],
                "cycle_id": record["cycle_id"], "producer_id": record["producer_id"],
                "cycle_dir": str(cycle_dir(root, record["campaign_id"], record["cycle_id"], record)),
                "env": _env_for(root, record, route),
                **({"rebound": True} if rebound else {}),
                **({"title_updated": True} if title_updated else {}),
                **_campaign_degradation(bound_campaign),
            }
        if campaign is not None:
            artifact_locator.prepare_index_update(root, [campaign["campaign_id"]])
        index = artifact_admission.load_index(root)
        if campaign is None and campaign_key is None:
            campaign = find_campaign_by_key(root, UNASSIGNED_KEY)
        campaign_created = False
        slug, display_title, slug_source, slug_truncated = _route_naming(
            route, campaign, title=title, goal=goal, root=root)
        started_on = _rfc3339(now)
        if campaign is None:
            new_campaign_id = alloc.allocate("campaign")
            while new_campaign_id in index.stable_ids:
                new_campaign_id = alloc.allocate("campaign")
            # The campaign is named from the proposed stream key; the route
            # slug names only this cycle (CONVENTIONS "Campaign or cycle").
            campaign_slug, campaign_title, campaign_slug_source, campaign_slug_truncated = (
                _campaign_naming(campaign_key))
            locator, locator_suffix = artifact_locator.allocate_locator(
                root / "campaigns", started_on, campaign_slug)
            campaign = {
                "schema_version": 1,
                "contract": CONTRACT,
                "campaign_id": new_campaign_id,
                "key": campaign_key or UNASSIGNED_KEY,
                **({"degraded": True, "degraded_reason": "campaign-unassigned"} if campaign_key is None else {}),
                "slug": campaign_slug,
                "title": campaign_title,
                "slug_source": campaign_slug_source,
                "slug_truncated": campaign_slug_truncated,
                "locator": locator,
                "locator_suffix": locator_suffix,
                "goal": (goal or f"{route_capability} cycle output") if campaign_key else "Work stream not proposed",
                "completion_criterion": {"statement": artifact_campaign.DEFAULT_COMPLETION_CRITERION},
                "state": "active",
                "created_on": started_on,
                "cycles": [],
            }
            campaign_created = True
            # The readable locator is persisted before children are created.
            _write_campaign(root, campaign, exclusive=True)
        else:
            # Existing W7 records keep their physical path until Cycle B, but
            # missing display fields are filled from this route for hybrid joins.
            campaign = dict(campaign)
            changed = False
            existing_key = campaign.get("key")
            fill_slug, fill_title, fill_source, fill_truncated = _campaign_naming(
                None if existing_key in (None, UNASSIGNED_KEY) else str(existing_key))
            for key, value in (
                ("slug", fill_slug), ("title", fill_title), ("slug_source", fill_source),
                ("slug_truncated", fill_truncated),
            ):
                if key not in campaign:
                    campaign[key] = value
                    changed = True
            # A campaign promoted out of `_unassigned` by the §37 metadata
            # amendment keeps the reserved placeholder title (the amendment
            # writes key/goal only); every later manifest would seal
            # `campaign.title = "_unassigned"`.  The key is the stream name.
            if existing_key not in (None, UNASSIGNED_KEY) and campaign.get("title") == UNASSIGNED_KEY:
                campaign["title"] = fill_title
                changed = True
            if changed:
                _write_campaign(root, campaign, exclusive=False)
        new_cycle_id = alloc.allocate("cycle")
        while new_cycle_id in index.stable_ids or cycle_record_path(root, new_cycle_id).exists():
            new_cycle_id = alloc.allocate("cycle")
        producer_id = alloc.allocate("producer")
        campaign_path = campaign_dir(root, campaign["campaign_id"], campaign)
        cycle_locator, cycle_locator_suffix = artifact_locator.allocate_locator(
            campaign_path, started_on, slug)
        record = {
            "schema_version": 1,
            "contract": CONTRACT,
            "cycle_id": new_cycle_id,
            "campaign_id": campaign["campaign_id"],
            "producer_id": producer_id,
            "parent_cycle_id": parent_cycle_id,
            **({"parent_cycle_state_at_begin": parent["state"]} if parent is not None else {}),
            "capability": capability,
            "route_capability": route_capability,
            "intensity": intensity,
            "route_id": route["route_id"],
            "route_hash": route["route_hash"],
            "route_file": str(resolved_route_file.resolve()),
            "node_id": node["id"] if node else None,
            "state": "open",
            "started_on": started_on,
            "sealed_on": None,
            "manifest_digest": None,
            "slug": slug,
            "title": display_title,
            "slug_source": slug_source,
            "slug_truncated": slug_truncated,
            "locator": cycle_locator,
            "locator_suffix": cycle_locator_suffix,
        }
        if shared_reference_pins is not None:
            record["shared_reference_pins"] = [dict(pin) for pin in shared_reference_pins]
        target = campaign_path / cycle_locator
        if target.exists():
            raise ProducerError("cycle-dir-exists", str(target))
        # Order: durable record first (crash before dir => recover drops the
        # record), then the folder.  Nothing here is visible to the index until
        # finalize's manifest commit point.
        _write_cycle_record(root, record, exclusive=True)
        _ensure_dir(target / "artifacts")
        campaign["cycles"] = list(campaign.get("cycles", [])) + [new_cycle_id]
        _write_campaign(root, campaign, exclusive=False)
        _write_cycle_binding(target, campaign["campaign_id"], new_cycle_id, started_on=started_on)
        if binding_jobs is not None and binding_owner:
            try:
                dispatch_terminal_commit.publish_producer_binding(
                    artifact_root=root, jobs=binding_jobs, route_file=resolved_route_file,
                    owner_attempt_id=binding_owner, cycle_id=new_cycle_id,
                    owner_begin=owner_begin)
            except dispatch_terminal_commit.TerminalCommitError as exc:
                raise ProducerError(exc.code, exc.detail) from exc
        artifact_locator.update_indexes(root, [campaign["campaign_id"]])
        if selected_group_id and owner_begin:
            stage_label = workflow_stage_label or artifact_workflow_groups.stage_label_from_title(display_title)
            try:
                artifact_workflow_groups.join_at_begin_locked(
                    root, campaign["campaign_id"], new_cycle_id, selected_group_id, stage_label)
            except artifact_workflow_groups.WorkflowGroupError as exc:
                # The issued cycle is resumable by this exact route. Its next
                # begin retries the same group append under admission lock.
                raise ProducerError(exc.code, exc.detail) from exc
        return {
            "status": "begun", "layout": "cycle", "campaign_id": campaign["campaign_id"],
            "cycle_id": new_cycle_id, "producer_id": producer_id, "cycle_dir": str(target),
            "campaign_created": campaign_created, "env": _env_for(root, record, route),
            **({"campaign_reopened": True, "campaign_reopen_event_id": campaign_reopen_event_id}
               if campaign_reopen_event_id else {}),
            **_campaign_degradation(campaign),
        }
    finally:
        artifact_admission._release_lock(root, lock_fd)


def begin(root: Path, **kwargs: Any) -> Dict[str, Any]:
    result = _begin_cycle_record(root, **kwargs)
    if result.get("title_updated"):
        # The existing publisher rereads the current open manifest under the
        # admission -> checkpoint lock order. No checkpoint scan or payload
        # rehash is needed for a title-only change.
        artifact_cycle_titles.emit_after_checkpoint(root, {"cycle_id": result["cycle_id"]})
    if (kwargs.get("node_id") is None and result.get("layout") == "cycle"
            and kwargs.get("capability") == "autopilot-spec"):
        # The admission lock has been released. Seed before a review worker can
        # write verdict.json; the later transaction retries the same receipt.
        route = load_route(Path(root).resolve(), resolve_route_argument(Path(root).resolve(), Path(kwargs["route_file"])))
        if route.get("spec_touch"):
            import importlib.util
            module_path = Path(__file__).with_name("spec-transaction.py")
            spec = importlib.util.spec_from_file_location("spec_transaction_preseed", module_path)
            transaction = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(transaction)
            transaction.preseed_owner_cycle(Path(root).resolve(), Path(result["cycle_dir"]))
    return result


# ---------------------------------------------------------------------------
# finalize
# ---------------------------------------------------------------------------


def _media_type(rel: str) -> str:
    return MEDIA_TYPES.get(Path(rel).suffix.lower(), "application/octet-stream")


def _bucket_type(rel: str) -> str:
    first = rel.split("/", 1)[0]
    return BUCKET_TYPES.get(first, "file")


def _unmanifestable_reason(rel: str) -> Optional[str]:
    """Why a relocated legacy file cannot carry a D-6 locator (None when it can)."""
    for part in rel.split("/"):
        if part.startswith("."):
            return "hidden-component"
        if not artifact_manifest._LOCATOR_COMPONENT_RE.match(part):
            return "invalid-component"
    return None


def _enumerate_output(directory: Path, *, exclude_hidden: bool = False,
                      excluded: Optional[List[str]] = None,
                      exclude_symlinks: bool = False,
                      excluded_symlinks: Optional[List[str]] = None) -> Tuple[List[Tuple[str, bytes]], List[str]]:
    """Regular files under `artifacts/`.  With `exclude_hidden`, files whose path
    cannot be a D-6 locator (a dot-component such as `.git/`/`.claude/` runtime
    residue, or a component longer than the locator limit) are left out of the
    manifest and reported through `excluded` instead of failing validation
    (W7E retrospective seal of relocated legacy trees).  With `exclude_symlinks`
    (an abandoned seal, which claims no success), symbolic links are left out
    and reported through `excluded_symlinks`; the link is only lstat-ed, never
    followed or read.  Without it a link is a `symlink-forbidden` violation."""
    paths: List[Tuple[str, Path]] = []
    violations: List[str] = []
    artifacts = directory / "artifacts"
    if not artifacts.is_dir() or artifacts.is_symlink():
        raise ProducerError("artifacts-dir-missing", str(artifacts))
    for entry in _walk_files(directory):
        rel = entry.relative_to(directory).as_posix()
        if rel == artifact_locator.CYCLE_BINDING:
            # Machine-owned locator binding, not user output or manifest data.
            continue
        if os.path.islink(str(entry)):
            if exclude_symlinks:
                if excluded_symlinks is not None:
                    excluded_symlinks.append(rel)
                continue
            violations.append(f"symlink-forbidden:{rel}")
            continue
        if not entry.is_file():
            violations.append(f"non-regular-file:{rel}")
            continue
        if not rel.startswith("artifacts/"):
            violations.append(f"file-outside-artifacts:{rel}")
            continue
        if exclude_hidden and _unmanifestable_reason(rel) is not None:
            if excluded is not None:
                excluded.append(rel)
            continue
        locator = artifact_manifest.validate_locator_path(rel)
        if not locator.ok:
            violations.extend(f"{v.code}:{rel}" for v in locator.violations)
            continue
        paths.append((rel, entry))
    # Validate the whole collection before reading payload bytes. A rejected
    # path must not silently vanish, or surface only after manifest allocation.
    if violations:
        return [], violations
    rows = [(rel, entry.read_bytes()) for rel, entry in paths]
    return rows, violations


def _is_support_locator(rel: str) -> bool:
    """A path through a CORE §3 support name that is not a cycle bucket (`_internal/`, `shards/`)."""
    return any(part in SUPPORT_SEGMENTS for part in rel.split("/")[1:])


def _place_loose_outputs(root: Path, record: Mapping[str, Any], directory: Path) -> List[Dict[str, str]]:
    """Normalize visible payloads under the cycle lock without replacing files."""
    output = directory / "artifacts"
    if output.is_symlink() or not output.is_dir():
        return []
    sources = [p for p in sorted(output.iterdir())
               if p.name not in BUCKET_TYPES and p.name not in SUPPORT_SEGMENTS
               # Cutover snapshots are harness-owned staging, not user output.
               and not (p.name == "shared-input" and
                        (p / "_internal/migration-shared-bases.json").is_file())
               and not p.name.startswith(".") and not p.is_symlink()
               and (p.is_file() or p.is_dir())]
    if not sources:
        return []
    target = output / default_bucket(str(record.get("capability", "")))
    # A foreign file/link at the bucket name is preserved by the existing scanner.
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        return []
    target.mkdir(exist_ok=True)
    if any(os.path.lexists(target / p.name) for p in sources):
        ordinal = 1
        while os.path.lexists(target / f"relocated-{ordinal}"):
            ordinal += 1
        target = target / f"relocated-{ordinal}"
    moved = []
    placement_path = producer_dir(root) / "bucket-placements" / f"{record['cycle_id']}.json"
    placements = {row["from"]: row for row in _output_placements(root, record)}
    for source in sources:
        source_rel = source.relative_to(directory).as_posix()
        prior = placements.get(source_rel)
        destination = directory / prior["to"] if prior else target / source.name
        if os.path.lexists(destination):
            destination = target / source.name
        if os.path.lexists(destination):
            continue
        if any(parent.is_symlink() for parent in destination.parents if parent != directory):
            continue
        row = {"from": source_rel, "to": destination.relative_to(directory).as_posix()}
        placements[source_rel] = row
        # Record the intended path before moving, so a retry can find the same
        # destination after a crash between rename and checkpoint publication.
        _ensure_dir(placement_path.parent)
        _write_atomic(placement_path, _json_bytes({"moves": list(placements.values())}), 0o644)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.rename(destination)
        except OSError as exc:
            placements.pop(source_rel, None)
            _write_atomic(placement_path, _json_bytes({"moves": list(placements.values()),
                          "pending": [{**row, "error": str(exc)}]}), 0o644)
            continue
        moved.append(row)
    if moved:
        print(f"artifact-buckets moved={len(moved)} paths=" + "; ".join(
            f"{row['from']} -> {row['to']}" for row in moved), file=sys.stderr)
    return moved


def _output_placements(root: Path, record: Mapping[str, Any]) -> List[Dict[str, str]]:
    path = producer_dir(root) / "bucket-placements" / f"{record['cycle_id']}.json"
    rows = (_read_json(path) or {}).get("moves", [])
    return [row for row in rows if isinstance(row, dict) and all(
        isinstance(row.get(key), str) and row[key].startswith("artifacts/")
        and not any(part in {"", ".", ".."} for part in row[key].split("/"))
        for key in ("from", "to"))]


def resolve_placed_output(path: Path) -> Path:
    """Follow this cycle's recorded move without changing sealed evidence bytes."""
    path = Path(path)
    if not path.is_absolute() or os.path.lexists(path):
        return path
    for output in path.parents:
        if output.name != "artifacts":
            continue
        directory = output.parent
        binding = _read_json(directory / ".cycle.json") or {}
        if not isinstance(binding, dict):
            continue
        cid = binding.get("cycle_id", "")
        if not artifact_identity.is_well_formed(cid, "cycle"):
            continue
        for root in list(directory.parents)[:4]:
            record = read_cycle_record(root, cid)
            if record is None:
                continue
            if cycle_dir(root, record["campaign_id"], cid, record) != directory:
                continue
            relative = path.relative_to(directory).as_posix()
            placed = _placed_locator(relative, _output_placements(root, record))
            candidate = directory / placed
            if candidate.resolve().is_relative_to(output.resolve()) and candidate.exists():
                return candidate
    return path


def placed_output_proof(path: Path, *, route_id: str, route_hash: str) -> Optional[Dict[str, str]]:
    """Read a route-bound move and current manifest digest for terminal repair.

    The manifest digest describes the file *now*. Neither it nor the placement
    ledger claims to know the file's bytes at the worker's earlier handoff.
    """
    origin = Path(path)
    if not origin.is_absolute() or os.path.lexists(origin):
        return None
    for output in origin.parents:
        if output.name != "artifacts":
            continue
        directory = output.parent
        binding = _read_json(directory / ".cycle.json") or {}
        if not isinstance(binding, dict):
            continue
        cid = binding.get("cycle_id", "")
        if not artifact_identity.is_well_formed(cid, "cycle"):
            continue
        for root in list(directory.parents)[:4]:
            record = read_cycle_record(root, cid)
            if (record is None or record.get("route_id") != route_id
                    or record.get("route_hash") != route_hash
                    or record.get("campaign_id") != binding.get("campaign_id")
                    or cycle_dir(root, record["campaign_id"], cid, record) != directory):
                continue
            placement_path = producer_dir(root) / "bucket-placements" / f"{cid}.json"
            manifest_path = producer_dir(root) / "open-manifests" / f"{cid}.json"
            try:
                placement_bytes = placement_path.read_bytes()
                manifest_bytes = manifest_path.read_bytes()
                manifest = json.loads(manifest_bytes)
            except (OSError, ValueError):
                continue
            if not isinstance(manifest, dict):
                continue
            checkpoint = _read_json(checkpoint_state_path(root, cid)) or {}
            if not isinstance(checkpoint, dict):
                continue
            if (checkpoint.get("manifest_id") != manifest.get("manifest_id")
                    or checkpoint.get("manifest_revision_id") != manifest.get("manifest_revision_id")
                    or checkpoint.get("route_id") != route_id):
                continue
            relative = origin.relative_to(directory).as_posix()
            try:
                moves = _output_placements(root, record)
            except (AttributeError, TypeError):
                continue
            applicable = [row for row in moves if relative == row["from"]
                          or relative.startswith(row["from"] + "/")]
            if len(applicable) != 1:
                continue
            placed = _placed_locator(relative, moves)
            if placed == relative or not placed:
                continue
            target = directory / placed
            try:
                canonical_output = output.resolve()
                canonical_target = target.resolve(strict=True)
                canonical_target.relative_to(canonical_output)
                if canonical_target != target or not target.is_file() or not os.access(target, os.R_OK):
                    continue
                content = target.read_bytes()
            except (OSError, ValueError):
                continue
            digest = "sha256:" + hashlib.sha256(content).hexdigest()
            revisions = [row for row in (manifest.get("artifact_revisions") or [])
                         if isinstance(row, dict)
                         and isinstance(row.get("locator"), dict)
                         and row["locator"].get("path") == placed
                         and row.get("content_digest") == digest
                         and row.get("byte_size") == len(content)
                         and isinstance(row.get("provenance"), dict)
                         and row["provenance"].get("producer_route_id") == route_id]
            routes = [row for row in (manifest.get("routes") or [])
                      if isinstance(row, dict) and row.get("route_id") == route_id
                      and row.get("route_hash") == route_hash]
            cycle = manifest.get("cycle")
            if (not isinstance(cycle, dict)
                    or cycle.get("cycle_id") != cid
                    or cycle.get("campaign_id") != record["campaign_id"]
                    or manifest.get("manifest_id") is None
                    or not manifest.get("manifest_revision_id")
                    or len(revisions) != 1 or len(routes) != 1):
                continue
            return {
                "origin": str(origin), "destination": str(target),
                "cycle_id": cid, "manifest_revision_id": manifest["manifest_revision_id"],
                "artifact_revision_id": revisions[0]["artifact_revision_id"],
                "current_content_digest": digest,
                "placement_sha256": hashlib.sha256(placement_bytes).hexdigest(),
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            }
    return None


def _placed_locator(locator: Optional[str], moved: Sequence[Mapping[str, str]]) -> Optional[str]:
    if not locator:
        return locator
    relative = locator if locator.startswith("artifacts/") else "artifacts/" + locator
    for row in moved:
        if relative == row["from"] or relative.startswith(row["from"] + "/"):
            return row["to"] + relative[len(row["from"]):]
    return locator


def _choose_primary(rows: Sequence[Tuple[str, bytes]], primary: Optional[str],
                    support: Sequence[str] = ()) -> Optional[str]:
    # A `support` row is attached evidence, not this cycle's output, so it is never
    # auto-nominated as the primary artifact -- an explicit `primary` still wins.
    # Support-material paths are skipped the same way while any durable output
    # exists; a cycle holding nothing else keeps its first row so a completed
    # cycle still carries the primary role its outcome criterion requires.
    support_set = set(support)
    names = [rel for rel, _ in rows if rel not in support_set]
    if primary:
        candidate = primary if primary.startswith("artifacts/") else "artifacts/" + primary
        if candidate not in names:
            shown = ", ".join(names[:6]) + (", ..." if len(names) > 6 else "")
            raise ProducerError(
                "primary-artifact-missing",
                f"{primary} (expected a cycle-relative path under artifacts/; cycle outputs: {shown or 'none'})",
            )
        return candidate
    durable = [rel for rel in names if not _is_support_locator(rel)] or names
    for wanted in PRIMARY_CANDIDATES:
        for rel in durable:
            if rel.endswith("/" + wanted) or rel == "artifacts/" + wanted:
                return rel
    documents = [rel for rel in durable if Path(rel).suffix.lower() in {".md", ".html", ".htm"}]
    return next(iter(documents or durable), None)


def _shared_pin_reference_path(root: Path, kind: str, ref_id: str) -> Path:
    return _reference_path(root, kind, ref_id)


def _shared_pin_revision_path(root: Path, kind: str, ref_id: str, rrev_id: str) -> Path:
    return Path(root) / "shared" / kind / ref_id / "revisions" / rrev_id / "revision.json"


def _resolve_shared_pin(
    root: Path, pin: Mapping[str, Any], provenance_fn: Optional[Any] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """D-78-a: resolve one `shared_reference_pins[]` entry from disk.

    Returns (shared_reference_row, shared_reference_revision_row). Raises a
    typed `ProducerError` when the pin is malformed or does not resolve --
    the finalize caller must never silently drop a pin (D-78-a: unresolved or
    digest-mismatched pins hold the seal, they do not fall back to `[]`).
    """
    if not isinstance(pin, Mapping):
        raise ProducerError("shared-reference-pin-invalid", "pin-not-an-object")
    kind = pin.get("kind")
    ref_id = pin.get("shared_reference_id")
    rrev_id = pin.get("shared_reference_revision_id")
    expected_digest = pin.get("content_digest")
    if kind not in SHARED_KINDS:
        raise ProducerError("shared-reference-pin-invalid", f"kind:{kind}")
    if not isinstance(ref_id, str) or not artifact_identity.is_well_formed(ref_id, "shared_reference"):
        raise ProducerError("shared-reference-pin-invalid", f"shared_reference_id:{ref_id}")
    if not isinstance(rrev_id, str) or not artifact_identity.is_well_formed(rrev_id, "shared_reference_revision"):
        raise ProducerError("shared-reference-pin-invalid", f"shared_reference_revision_id:{rrev_id}")
    if expected_digest is not None and not isinstance(expected_digest, str):
        raise ProducerError("shared-reference-pin-invalid", "content_digest")
    reference = _read_json(_shared_pin_reference_path(root, kind, ref_id))
    if reference is None:
        raise ProducerError("shared-reference-pin-unresolved", f"reference:{kind}:{ref_id}")
    revision = _read_json(_shared_pin_revision_path(root, kind, ref_id, rrev_id))
    if revision is None:
        raise ProducerError("shared-reference-pin-unresolved", f"revision:{kind}:{ref_id}:{rrev_id}")
    content_digest = revision.get("content_digest")
    if not isinstance(content_digest, str):
        raise ProducerError("shared-reference-pin-unresolved", f"revision-digest:{kind}:{ref_id}:{rrev_id}")
    if expected_digest is not None and expected_digest != content_digest:
        raise ProducerError("shared-reference-pin-digest-mismatch", f"{ref_id}:{rrev_id}")
    ref_row = {
        "shared_reference_id": ref_id, "kind": reference.get("kind"),
        "title": str(reference.get("title") or ""),
    }
    rev_row: Dict[str, Any] = {
        "shared_reference_revision_id": rrev_id, "shared_reference_id": ref_id,
        "content_digest": content_digest, "updated_at": revision.get("created_on"),
    }
    if provenance_fn is not None:
        rev_row["provenance"] = provenance_fn(content_digest)
    return ref_row, rev_row


def validate_shared_reference_pins(root: Path, pins: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Pure D-78-a validation (no manifest, no provenance) -- R2 calls this
    before finalize so an unresolved pin holds early. Returns a list of
    `{"index", "code", "detail"}` violation rows; empty means every pin
    resolves."""
    root = Path(root).resolve()
    violations: List[Dict[str, Any]] = []
    for i, pin in enumerate(pins):
        try:
            _resolve_shared_pin(root, pin)
        except ProducerError as exc:
            violations.append({"index": i, "code": exc.code, "detail": exc.detail})
    return violations


def _cycle_relative_primary(primary: Optional[str], directory: Path) -> Optional[str]:
    """Map an absolute `--primary` that points inside this cycle's `artifacts/`
    onto the cycle-relative form `_choose_primary` expects. Anything else is
    returned unchanged so the existing `primary-artifact-missing` verdict still
    names what the caller passed (2026-09-16 DX report: an absolute path failed
    with no hint that only cycle-relative locators are accepted)."""
    if not primary or not os.path.isabs(primary):
        return primary
    try:
        rel = Path(primary).resolve().relative_to(Path(directory).resolve())
    except (OSError, ValueError):
        return primary
    rel_posix = rel.as_posix()
    return rel_posix if rel_posix.startswith("artifacts/") else primary


def build_manifest(
    root: Path,
    record: Mapping[str, Any],
    route: Mapping[str, Any],
    rows: Sequence[Tuple[str, bytes]],
    *,
    state: str,
    primary: Optional[str],
    allow_open_route: bool,
    allocator: artifact_identity.IdAllocator,
    now: Optional[float],
    abandon_reason: Optional[str] = None,
    support_locators: Sequence[str] = (),
    reserved: Optional["InterimReservation"] = None,
    interim: bool = False,
    facts: Optional[Sequence[Tuple[str, str, int]]] = None,
) -> Dict[str, Any]:
    """Build the D-6 cycle document.

    `facts` rows are `(locator, content_digest, byte_size)`; when absent they are
    computed from `rows`.  `reserved` carries the IDs an open-cycle checkpoint
    already published, so the sealed document keeps them.  `interim` builds that
    checkpoint document itself: `cycle.state` is `open`, with no cycle or route
    terminal event and no route-closure requirement.
    """
    identity = artifact_lifecycle.read_root_identity(root)
    if identity is None:
        raise ProducerError("root-identity-missing")
    campaign = read_campaign(root, record["campaign_id"])
    if campaign is None:
        raise ProducerError("campaign-unknown", record["campaign_id"])
    if facts is None:
        facts = [(rel, _digest(data), len(data)) for rel, data in rows]
    man_id = (reserved.manifest_id if reserved is not None and reserved.manifest_id
              else allocator.allocate("manifest"))
    mrev_id = allocator.allocate("manifest_revision")
    when = _rfc3339(now)
    # `support_locators` are `artifacts/`-relative locators the caller marks as
    # attached evidence rather than cycle output (W7G D-79 relocates lump-external
    # loose files into a cycle this way). Empty by default, so an ordinary cycle's
    # manifest bytes are unchanged.
    support_rels = {"artifacts/" + rel.lstrip("/") for rel in support_locators}
    primary_rel = _choose_primary([(rel, None) for rel, _d, _s in facts], primary, support=support_rels)

    def provenance(digest: str, recorded_in: Optional[str] = None) -> Dict[str, Any]:
        return {
            "source_manifest_id": man_id, "source_revision_id": recorded_in or mrev_id,
            "producer_route_id": route["route_id"], "algorithm_version": ALGORITHM_VERSION,
            "schema_version": 1, "source_digest": digest,
        }

    artifacts: List[Dict[str, Any]] = []
    revisions: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    for rel, digest, byte_size in facts:
        # A path the open-cycle checkpoint already published keeps its artifact
        # ID; its revision ID is kept only while the content is unchanged.
        kept = reserved.artifacts.get(rel) if reserved is not None else None
        art_id = kept.artifact_id if kept else allocator.allocate("artifact")
        # A revision already reserved for this content (current, or earlier and
        # returned to) keeps its ID, provenance and recording event, so its rows
        # are identical in every interim document and the sealed one.
        same = reserved.revision_for(rel, digest) if reserved is not None else None
        arev_id = same.artifact_revision_id if same else allocator.allocate("artifact_revision")
        revision_provenance = provenance(digest, same.recorded_in if same else None)
        inner = rel[len("artifacts/"):]
        artifacts.append({
            "artifact_id": art_id, "cycle_id": record["cycle_id"],
            "role": "support" if rel in support_rels else ("primary" if rel == primary_rel else "output"),
            "type": _bucket_type(inner), "capability": record["capability"], "title": inner,
        })
        revisions.append({
            "artifact_revision_id": arev_id, "artifact_id": art_id, "revision_sequence": 1,
            "content_digest": digest, "byte_size": byte_size, "media_type": _media_type(rel),
            "locator": {"kind": "cycle-relative", "path": rel}, "provenance": revision_provenance,
        })
        reuse_event = same is not None and same.event_id and same.stream_id and same.recorded_at
        events.append({
            "event_id": same.event_id if reuse_event else allocator.allocate("event"),
            "stream_id": same.stream_id if reuse_event else allocator.allocate("stream"),
            "stream_sequence": 1, "event_type": "artifact.revision.recorded", "target_id": art_id,
            "actor": {"kind": "producer", "id": record["producer_id"]},
            "recorded_at": same.recorded_at if reuse_event else when,
            "provenance": revision_provenance, "evidence_ids": [], "payload": {"locator": rel},
        })
    cycle_digest = _digest(_canonical([[rel, digest] for rel, digest, _size in facts]))
    routes_row = {
        "artifact_root_id": identity.artifact_root_id, "route_id": route["route_id"],
        "route_hash": route["route_hash"], "terminal_marker": "pending",
        "terminal_evidence_id": "",
    }
    closed = False if interim else route_is_closed(root, route)
    if not closed and not allow_open_route and not interim:
        raise ProducerError(
            "route-not-closed",
            f"{route['route_id']}: required order: complete -> close -> finalize -> admit-shared; "
            "complete the terminal node using verified cycle-local evidence, then close the route",
        )
    # D-6: a `completed` cycle must bind a route.terminal.recorded event, which
    # only exists once the route is closed.  Sealing an open route therefore
    # records a provisional `active` cycle (lineage committed, completion not
    # claimed); `abandoned` needs no terminal evidence.
    if interim:
        cycle_state = artifact_manifest.INTERIM_CYCLE_STATE
    elif state == "abandoned":
        cycle_state = "abandoned"
    elif closed:
        cycle_state = "completed"
    else:
        cycle_state = "active"
    if cycle_state not in ("active", artifact_manifest.INTERIM_CYCLE_STATE):
        payload = {"abandon_reason": abandon_reason} if cycle_state == "abandoned" else {}
        events.append({
            "event_id": allocator.allocate("event"), "stream_id": allocator.allocate("stream"),
            "stream_sequence": 1, "event_type": f"cycle.{cycle_state}", "target_id": record["cycle_id"],
            "actor": {"kind": "producer", "id": record["producer_id"]}, "recorded_at": when,
            "provenance": provenance(cycle_digest), "evidence_ids": [], "payload": payload,
        })
    if closed and cycle_state == "completed":
        terminal_event_id = allocator.allocate("event")
        events.append({
            "event_id": terminal_event_id, "stream_id": allocator.allocate("stream"),
            "stream_sequence": 1, "event_type": "route.terminal.recorded", "target_id": record["cycle_id"],
            "actor": {"kind": "system", "id": "capability-route"}, "recorded_at": when,
            "provenance": provenance(cycle_digest), "evidence_ids": [], "payload": {},
        })
        routes_row["terminal_evidence_id"] = terminal_event_id
    # D-78-a: pins are the sole source of shared_references[]/shared_reference_revisions[].
    # No pins => both stay `[]` and the manifest bytes are unchanged from before this feature.
    shared_references: List[Dict[str, Any]] = []
    shared_reference_revisions: List[Dict[str, Any]] = []
    seen_shared_reference_ids: set = set()
    for pin in record.get("shared_reference_pins") or []:
        ref_row, rev_row = _resolve_shared_pin(root, pin, provenance)
        if ref_row["shared_reference_id"] not in seen_shared_reference_ids:
            shared_references.append(ref_row)
            seen_shared_reference_ids.add(ref_row["shared_reference_id"])
        shared_reference_revisions.append(rev_row)
    document = {
        "schema_version": 2, "manifest_kind": "artifact.cycle",
        "manifest_id": man_id, "manifest_revision_id": mrev_id,
        "repository_id": identity.repository_id, "artifact_root_id": identity.artifact_root_id,
        "campaign": {
            "campaign_id": campaign["campaign_id"], "goal": str(campaign.get("goal", "")),
            "completion_criterion": {"statement": str((campaign.get("completion_criterion") or {}).get("statement", ""))},
            "title": str(campaign.get("title", "")), "state": str(campaign.get("state", "active")),
        },
        "cycle": {
            "cycle_id": record["cycle_id"], "campaign_id": campaign["campaign_id"],
            "parent_cycle_id": record.get("parent_cycle_id"),
            "started_on": record["started_on"], "input_digest": _digest(_canonical({
                # D-120 fixed-input boundary: the begin route's identity, not
                # the route sealing the cycle (`route`, which may be a
                # continuation's rebound lineage extension).
                "route_id": record["route_id"], "route_hash": record["route_hash"],
                "capability": record["capability"], "intensity": record["intensity"],
            })),
            "outcome_criterion": {"required_artifact_roles": ["primary"] if facts else [], "decision_required": False},
            "state": cycle_state,
        },
        "artifacts": artifacts, "artifact_revisions": revisions,
        "shared_references": shared_references, "shared_reference_revisions": shared_reference_revisions,
        "routes": [routes_row], "events": events,
        "producer": {
            "producer_id": record["producer_id"], "contract_version": artifact_manifest.CONTRACT_VERSION,
            "source_revision": f"{record['capability']}/{record['intensity']}/{ALGORITHM_VERSION}",
        },
    }
    if closed and cycle_state == "completed":
        # D-120: the route sealing this cycle (`route`, R) may be a
        # continuation the begin record's own `route_file` never names --
        # rebind the terminal evidence from R's own canonical file, not the
        # begin route's.
        binding, sealed_route = artifact_lifecycle.bind_existing_runtime_route(
            root, route_lineage.canonical_route_path(root, route["route_id"]),
            expected_root_id=identity.artifact_root_id
        )
        if sealed_route.get("route_hash") != route["route_hash"]:
            raise ProducerError("route-hash-drift", route["route_id"])
        try:
            document = artifact_lifecycle._derive_terminal_evidence(document, binding, sealed_route)
        except artifact_lifecycle.LifecycleError as exc:
            raise ProducerError(exc.code, exc.detail)
    return document


# ---------------------------------------------------------------------------
# open-cycle checkpoint: the interim manifest
# ---------------------------------------------------------------------------
#
# A consumer that mirrors artifacts (Cairn) can only read a sealed cycle's
# `manifest.json`.  A checkpoint publishes the same closed D-6 document for a
# cycle that is still open -- `cycle.state` is `open` -- under
# `.runtime/artifact-producer/v1/open-manifests/<cyc>.json`.
#
# IDs come from one cumulative reservation ledger,
# `checkpoints/<cyc>.ids.json`: every locator a checkpoint ever published keeps
# its artifact ID there, even while a later checkpoint leaves the file out
# (grown past a size limit, briefly missing), and a revision keeps its ID,
# provenance and recording event while its digest is unchanged.  Every later
# checkpoint and the final `finalize` assign IDs from that ledger, so a
# consumer row survives the seal.  Sealing removes the interim files.
# `checkpoints/<cyc>.json` is bookkeeping only (interval, last result, digest
# cache).

OPEN_MANIFEST_DIR = "open-manifests"
CHECKPOINT_DIR = "checkpoints"
CHECKPOINT_TRIGGERS = ("explicit", "stage-complete", "supervisor-poll", "turn-end")
CHECKPOINT_INTERVAL_ENV = "AGENT_ARTIFACT_CHECKPOINT_MIN_INTERVAL"
CHECKPOINT_MIN_INTERVAL_SECONDS = 900.0
# An automatic trigger never publishes a first interim document for a cycle
# whose files have not changed for this long: that cycle is not live work.
CHECKPOINT_STALE_SECONDS = 24 * 3600.0
CHECKPOINT_FINALIZE_LOCK_SECONDS = 60.0
RESERVATION_SCHEMA_VERSION = 1
# Earlier revisions a locator keeps in the ledger, so content that returns to
# an earlier digest returns to that revision's ID.
RESERVATION_HISTORY_LIMIT = 16
# A route is finalized by the release it was sealed to.  Only a release that
# carries this file keeps interim IDs at the seal, so a cycle whose route is
# sealed to an older release publishes no interim document.
INTERIM_SUPPORT_MARKER = "utilities/artifact_checkpoint_trigger.py"
# Weights, archives, and array dumps are not something a reader opens; they
# stay on disk and are declared only by the sealed manifest.
CHECKPOINT_EXCLUDED_SUFFIXES = frozenset({
    ".pt", ".pth", ".ckpt", ".safetensors", ".bin", ".onnx", ".h5", ".hdf5", ".pkl",
    ".pickle", ".joblib", ".npy", ".npz", ".tflite", ".pb", ".tar", ".zip", ".gz", ".tgz",
    ".xz", ".bz2", ".7z", ".zst", ".tfrecord", ".arrow", ".parquet", ".lmdb", ".mdb",
})
_STALE_TEMP_SECONDS = 3600.0


@dataclass(frozen=True)
class CheckpointLimits:
    max_walk_entries: int = 20000
    max_files: int = 2000
    max_total_bytes: int = 256 * 1024 * 1024
    max_file_bytes: int = 32 * 1024 * 1024

    def to_payload(self) -> Dict[str, int]:
        return {"max_walk_entries": self.max_walk_entries, "max_files": self.max_files,
                "max_total_bytes": self.max_total_bytes, "max_file_bytes": self.max_file_bytes}


@dataclass(frozen=True)
class ReservedRevision:
    artifact_id: str
    artifact_revision_id: str
    content_digest: str
    # The manifest revision that first recorded this artifact revision and its
    # `artifact.revision.recorded` event; reused while the digest is unchanged.
    recorded_in: Optional[str] = None
    event_id: Optional[str] = None
    stream_id: Optional[str] = None
    recorded_at: Optional[str] = None


@dataclass(frozen=True)
class InterimReservation:
    manifest_id: Optional[str]
    artifacts: Dict[str, ReservedRevision]
    dropped: int = 0
    # locator -> content_digest -> an earlier revision of the same artifact
    history: Dict[str, Dict[str, ReservedRevision]] = field(default_factory=dict)

    def revision_for(self, locator: str, digest: str) -> Optional[ReservedRevision]:
        current = self.artifacts.get(locator)
        if current is not None and current.content_digest == digest:
            return current
        return self.history.get(locator, {}).get(digest) if current is not None else None


def _without_event_reuse(reserved: InterimReservation) -> InterimReservation:
    """The same IDs, with every reused provenance/event field dropped."""
    def bare(row: ReservedRevision) -> ReservedRevision:
        return replace(row, recorded_in=None, event_id=None, stream_id=None, recorded_at=None)
    return InterimReservation(
        reserved.manifest_id, {loc: bare(row) for loc, row in reserved.artifacts.items()}, reserved.dropped,
        {loc: {d: bare(row) for d, row in rows.items()} for loc, rows in reserved.history.items()},
    )


def open_manifest_path(root: Path, cycle_id: str) -> Path:
    if not artifact_identity.is_well_formed(cycle_id, "cycle"):
        raise ProducerError("cycle-id-invalid", str(cycle_id))
    return producer_dir(root) / OPEN_MANIFEST_DIR / f"{cycle_id}.json"


def checkpoint_state_path(root: Path, cycle_id: str) -> Path:
    if not artifact_identity.is_well_formed(cycle_id, "cycle"):
        raise ProducerError("cycle-id-invalid", str(cycle_id))
    return producer_dir(root) / CHECKPOINT_DIR / f"{cycle_id}.json"


def reservation_path(root: Path, cycle_id: str) -> Path:
    return checkpoint_state_path(root, cycle_id).with_suffix(".ids.json")


def _checkpoint_lock_path(root: Path, cycle_id: str) -> Path:
    return checkpoint_state_path(root, cycle_id).with_suffix(".lock")


def checkpoint_interval_seconds() -> float:
    raw = os.environ.get(CHECKPOINT_INTERVAL_ENV, "")
    try:
        value = float(raw) if raw else CHECKPOINT_MIN_INTERVAL_SECONDS
    except ValueError:
        return CHECKPOINT_MIN_INTERVAL_SECONDS
    return value if value >= 0 else CHECKPOINT_MIN_INTERVAL_SECONDS


@contextlib.contextmanager
def _checkpoint_lock(root: Path, cycle_id: str, *, timeout: float):
    """Per-cycle lock shared by checkpoint (ID assignment + writes) and finalize
    (ID reuse through interim removal).  Never taken before the admission lock."""
    path = _checkpoint_lock_path(root, cycle_id)
    _ensure_dir(path.parent)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ProducerError("checkpoint-lock-busy", cycle_id)
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def _well_formed_or_none(value: Any, kind: str) -> Optional[str]:
    return value if isinstance(value, str) and artifact_identity.is_well_formed(value, kind) else None


def _reserved_revision(row: Any, artifact_id: str) -> Optional[ReservedRevision]:
    """One revision entry, checked by ID format only.  Reused provenance and
    event fields must also pass the manifest's own value rules; a field that
    does not is dropped (a fresh one is issued), never carried into a seal."""
    if not isinstance(row, Mapping):
        return None
    arev_id = _well_formed_or_none(row.get("artifact_revision_id"), "artifact_revision")
    digest = row.get("content_digest")
    if arev_id is None or not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        return None
    recorded_at = row.get("recorded_at")
    return ReservedRevision(
        artifact_id, arev_id, digest,
        recorded_in=_well_formed_or_none(row.get("recorded_in"), "manifest_revision"),
        event_id=_well_formed_or_none(row.get("event_id"), "event"),
        stream_id=_well_formed_or_none(row.get("stream_id"), "stream"),
        recorded_at=(recorded_at if isinstance(recorded_at, str)
                     and artifact_manifest._RFC3339_RE.match(recorded_at) else None),
    )


def _reserved_row(locator: Any, row: Any) -> Optional[Tuple[ReservedRevision, List[ReservedRevision]]]:
    """One ledger row (current revision + earlier ones), checked by ID format and
    locator grammar only -- a later manifest schema or contract version never
    invalidates a reservation."""
    if not isinstance(locator, str) or not isinstance(row, Mapping):
        return None
    if not artifact_manifest.validate_locator_path(locator).ok:
        return None
    art_id = _well_formed_or_none(row.get("artifact_id"), "artifact")
    current = _reserved_revision(row, art_id) if art_id else None
    if current is None:
        return None
    raw = row.get("history")
    earlier = [_reserved_revision(item, art_id) for item in (raw if isinstance(raw, list) else [])]
    return current, [rev for rev in earlier if rev is not None]


def _reservation_from_rows(manifest_id: Any, rows: Iterable[Tuple[Any, Any]]) -> InterimReservation:
    kept: Dict[str, ReservedRevision] = {}
    history: Dict[str, Dict[str, ReservedRevision]] = {}
    seen_artifacts: Set[str] = set()
    seen_revisions: Set[str] = set()
    seen_events: Set[str] = set()
    seen_streams: Set[str] = set()
    dropped = 0

    def unique_events(rev: ReservedRevision) -> ReservedRevision:
        # A duplicated event or stream ID would make the sealed manifest invalid;
        # keep the revision and let it record a fresh event instead.
        if (rev.event_id in seen_events or rev.stream_id in seen_streams
                or not (rev.event_id and rev.stream_id and rev.recorded_at)):
            return replace(rev, event_id=None, stream_id=None, recorded_at=None)
        seen_events.add(rev.event_id)
        seen_streams.add(rev.stream_id)
        return rev

    for locator, row in rows:
        parsed = _reserved_row(locator, row)
        # A duplicated artifact or revision ID would make the next manifest
        # invalid; drop the row.
        if (parsed is None or locator in kept or parsed[0].artifact_id in seen_artifacts
                or parsed[0].artifact_revision_id in seen_revisions):
            dropped += 1
            continue
        current, earlier = parsed
        kept[locator] = unique_events(current)
        seen_artifacts.add(current.artifact_id)
        seen_revisions.add(current.artifact_revision_id)
        for rev in earlier:
            if (rev.artifact_revision_id in seen_revisions or rev.content_digest == current.content_digest
                    or rev.content_digest in history.get(locator, {})):
                continue
            history.setdefault(locator, {})[rev.content_digest] = unique_events(rev)
            seen_revisions.add(rev.artifact_revision_id)
    return InterimReservation(_well_formed_or_none(manifest_id, "manifest"), kept, dropped, history)


def _reservation_from_document(document: Mapping[str, Any]) -> InterimReservation:
    """Fallback when the ledger is gone: the published interim document's rows."""
    events = {row.get("target_id"): row for row in document.get("events") or []
              if isinstance(row, Mapping) and row.get("event_type") == "artifact.revision.recorded"}
    rows = []
    for revision in document.get("artifact_revisions") or []:
        if not isinstance(revision, Mapping):
            continue
        locator = (revision.get("locator") or {}).get("path") if isinstance(revision.get("locator"), Mapping) else None
        event = events.get(revision.get("artifact_id")) or {}
        provenance = revision.get("provenance") if isinstance(revision.get("provenance"), Mapping) else {}
        rows.append((locator, {
            "artifact_id": revision.get("artifact_id"),
            "artifact_revision_id": revision.get("artifact_revision_id"),
            "content_digest": revision.get("content_digest"),
            "recorded_in": provenance.get("source_revision_id"),
            "event_id": event.get("event_id"), "stream_id": event.get("stream_id"),
            "recorded_at": event.get("recorded_at"),
        }))
    return _reservation_from_rows(document.get("manifest_id"), rows)


def read_interim_reservation(root: Path, record: Mapping[str, Any]) -> Tuple[Optional[InterimReservation], str]:
    """The cycle's ID reservation and a status word: `absent`, `present`,
    `document-fallback` (ledger missing, rebuilt from the interim document),
    `unreadable`, or `identity-mismatch`.  For the last two the returned
    reservation is the document fallback, when one can be read."""
    cid = record["cycle_id"]
    fallback: Optional[InterimReservation] = None
    document = _read_json(open_manifest_path(root, cid))
    if document is not None and isinstance(document.get("cycle"), Mapping) \
            and document["cycle"].get("cycle_id") == cid:
        fallback = _reservation_from_document(document)
    ledger_path = reservation_path(root, cid)
    try:
        ledger_path.lstat()
    except FileNotFoundError:
        return (fallback, "document-fallback") if fallback is not None else (None, "absent")
    except OSError:
        return fallback, "unreadable"
    payload = _read_json(ledger_path)
    if payload is None or not isinstance(payload.get("artifacts"), Mapping):
        return fallback, "unreadable"
    if (payload.get("cycle_id") != cid or payload.get("campaign_id") != record.get("campaign_id")
            or payload.get("route_id") != record.get("route_id")):
        return fallback, "identity-mismatch"
    return _reservation_from_rows(payload.get("manifest_id"), sorted(payload["artifacts"].items())), "present"


def _ledger_row(current: ReservedRevision, earlier: Mapping[str, ReservedRevision]) -> Dict[str, Any]:
    row = {key: value for key, value in asdict(current).items() if value is not None}
    kept = [rev for digest, rev in earlier.items() if digest != current.content_digest]
    if kept:
        row["history"] = [
            {key: value for key, value in asdict(rev).items() if value is not None and key != "artifact_id"}
            for rev in kept[-RESERVATION_HISTORY_LIMIT:]
        ]
    return row


def _reservation_payload(record: Mapping[str, Any], document: Mapping[str, Any],
                         previous: Optional[InterimReservation]) -> Dict[str, Any]:
    """Union of the previous reservation and this document -- never shrinks.  A
    locator whose content changed keeps its earlier revision in `history`."""
    rows: Dict[str, Dict[str, Any]] = {}
    if previous is not None:
        for locator, row in previous.artifacts.items():
            rows[locator] = _ledger_row(row, previous.history.get(locator, {}))
    events = {row["target_id"]: row for row in document["events"]
              if row["event_type"] == "artifact.revision.recorded"}
    for revision in document["artifact_revisions"]:
        locator = revision["locator"]["path"]
        event = events.get(revision["artifact_id"], {})
        current = ReservedRevision(
            revision["artifact_id"], revision["artifact_revision_id"], revision["content_digest"],
            recorded_in=revision["provenance"]["source_revision_id"],
            event_id=event.get("event_id"), stream_id=event.get("stream_id"),
            recorded_at=event.get("recorded_at"),
        )
        earlier: Dict[str, ReservedRevision] = {}
        if previous is not None:
            earlier = dict(previous.history.get(locator, {}))
            before = previous.artifacts.get(locator)
            if before is not None and before.content_digest != current.content_digest:
                earlier.pop(before.content_digest, None)
                earlier[before.content_digest] = before
        rows[locator] = _ledger_row(current, earlier)
    return {
        "schema_version": RESERVATION_SCHEMA_VERSION, "cycle_id": record["cycle_id"],
        "campaign_id": record["campaign_id"], "route_id": record.get("route_id"),
        "manifest_id": document["manifest_id"], "artifacts": dict(sorted(rows.items())),
    }


def remove_interim(root: Path, cycle_id: str) -> None:
    """Drop a cycle's interim document, reservation and bookkeeping (idempotent)."""
    if not artifact_identity.is_well_formed(cycle_id, "cycle"):
        return
    for path in (open_manifest_path(root, cycle_id), reservation_path(root, cycle_id),
                 checkpoint_state_path(root, cycle_id), _checkpoint_lock_path(root, cycle_id)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _sweep_orphan_interims(root: Path, *, now: Optional[float] = None) -> List[str]:
    """Remove interim files of cycles that are no longer open.  A cycle record
    that exists but cannot be read keeps its files (a transient read error must
    not cost a live cycle its IDs); abandoned temporary files are dropped."""
    clock = time.time() if now is None else now
    removed: List[str] = []
    for name in (OPEN_MANIFEST_DIR, CHECKPOINT_DIR):
        directory = producer_dir(root) / name
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if entry.name.startswith(".") and ".tmp-" in entry.name:
                try:
                    if clock - entry.lstat().st_mtime > _STALE_TEMP_SECONDS:
                        entry.unlink()
                except OSError:
                    pass
                continue
            cycle_id = entry.name.split(".", 1)[0]
            if not artifact_identity.is_well_formed(cycle_id, "cycle") or cycle_id in removed:
                continue
            try:
                cycle_record_path(root, cycle_id).lstat()
            except FileNotFoundError:
                record: Optional[Dict[str, Any]] = {"state": "absent"}
            except OSError:
                continue
            else:
                record = read_cycle_record(root, cycle_id)
                if record is None:
                    continue
            if record.get("state") != "open":
                remove_interim(root, cycle_id)
                removed.append(cycle_id)
    return removed


def _route_release_supports_interim(route: Mapping[str, Any]) -> Tuple[bool, str]:
    tuple_ = route.get("launch_compatibility_tuple")
    runtime = tuple_.get("runtime_root") if isinstance(tuple_, Mapping) else None
    path = runtime.get("path") if isinstance(runtime, Mapping) else None
    if not isinstance(path, str) or not path:
        return False, "runtime-root-unsealed"
    return (Path(path) / INTERIM_SUPPORT_MARKER).is_file(), path


def _checkpoint_scan(directory: Path, previous: Mapping[str, Any], limits: CheckpointLimits) -> Dict[str, Any]:
    """Bounded walk of `artifacts/`.  Returns `facts` (locator, digest, size),
    the refreshed digest cache, exclusion counts and the newest file mtime, or a
    `skip` reason as soon as a limit is exceeded (the walk stops there)."""
    artifacts = directory / "artifacts"
    if not artifacts.is_dir() or artifacts.is_symlink():
        return {"skip": "artifacts-dir-missing"}
    excluded = {"hidden": 0, "binary": 0, "oversize": 0, "non-regular": 0, "invalid-locator": 0}
    candidates: List[Tuple[str, str, os.stat_result]] = []
    visited = total = 0
    newest = 0.0
    for current, dirs, files in os.walk(str(artifacts), followlinks=False):
        kept_dirs = []
        for name in sorted(dirs):
            if name.startswith("."):
                excluded["hidden"] += 1
            elif os.path.islink(os.path.join(current, name)):
                excluded["non-regular"] += 1
            else:
                kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(files):
            visited += 1
            if visited > limits.max_walk_entries:
                return {"skip": "walk-limit", "visited": visited}
            path = os.path.join(current, name)
            try:
                info = os.lstat(path)
            except OSError:
                excluded["non-regular"] += 1
                continue
            if not stat.S_ISREG(info.st_mode):
                excluded["non-regular"] += 1
                continue
            newest = max(newest, info.st_mtime)
            if name.startswith("."):
                excluded["hidden"] += 1
                continue
            if os.path.splitext(name)[1].lower() in CHECKPOINT_EXCLUDED_SUFFIXES:
                excluded["binary"] += 1
                continue
            if info.st_size > limits.max_file_bytes:
                excluded["oversize"] += 1
                continue
            rel = Path(path).relative_to(directory).as_posix()
            locator = artifact_manifest.validate_locator_path(rel)
            if not locator.ok:
                excluded["invalid-locator"] += 1
                reason = locator.violations[0].code
                reasons = excluded.setdefault("invalid-locator-reasons", {})
                reasons[reason] = reasons.get(reason, 0) + 1
                continue
            candidates.append((rel, path, info))
            total += info.st_size
            if len(candidates) > limits.max_files:
                return {"skip": "file-count-limit", "files": len(candidates)}
            if total > limits.max_total_bytes:
                return {"skip": "byte-size-limit", "bytes": total}
    facts: List[Tuple[str, str, int]] = []
    stats: Dict[str, List[Any]] = {}
    for rel, path, info in sorted(candidates):
        old = previous.get(rel)
        if (isinstance(old, list) and len(old) == 3 and old[0] == info.st_size
                and old[1] == info.st_mtime_ns and isinstance(old[2], str)):
            digest, size = old[2], info.st_size
        else:
            try:
                data = Path(path).read_bytes()
            except OSError:
                excluded["non-regular"] += 1
                continue
            digest, size = _digest(data), len(data)
        facts.append((rel, digest, size))
        stats[rel] = [size, info.st_mtime_ns, digest]
    return {"facts": facts, "stats": stats, "excluded": excluded, "newest_mtime": newest,
            "total_bytes": sum(size for _rel, _digest_value, size in facts)}


def _checkpoint_target(root: Path, cycle_id: Optional[str], route_file: Optional[Path]) -> Optional[Dict[str, Any]]:
    if cycle_id:
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        return record
    if route_file is None:
        raise ProducerError("checkpoint-target-required", "--cycle or --route")
    route = load_route(root, route_file)
    return route_cycle_for(root, route)


def checkpoint(
    root: Path,
    *,
    cycle_id: Optional[str] = None,
    route_file: Optional[Path] = None,
    trigger: str = "explicit",
    now: Optional[float] = None,
    allocator: Optional[artifact_identity.IdAllocator] = None,
    limits: Optional[CheckpointLimits] = None,
) -> Dict[str, Any]:
    """Publish (or refresh) an open cycle's interim manifest.

    Every outcome that is not an error is a result: `emitted`, `unchanged`, or
    `skipped` with a `reason`.  Only a live cycle is published: the cycle is
    open with no sealed manifest, and its route is readable, not closed, and
    sealed to a release that keeps interim IDs at the seal.  Scans are
    rate-limited per cycle (`checkpoint_interval_seconds`) for every trigger,
    including an explicit one.  The scan runs unlocked; every write -- the
    reservation, the document and the bookkeeping -- happens under the cycle's
    checkpoint lock after the cycle is re-read.
    """
    root = Path(root).resolve()
    if trigger not in CHECKPOINT_TRIGGERS:
        raise ProducerError("checkpoint-trigger-invalid", trigger)
    clock = time.time() if now is None else float(now)
    try:
        record = _checkpoint_target(root, cycle_id, route_file)
    except ProducerError as exc:
        if exc.code == "route-hash-drift":
            return {"status": "skipped", "reason": "route-hash-drift", "trigger": trigger}
        raise
    if record is None:
        return {"status": "skipped", "reason": "no-open-cycle", "trigger": trigger}
    cid = record["cycle_id"]
    base = {"cycle_id": cid, "route_id": record.get("route_id"), "trigger": trigger}

    def skipped(reason: str, **extra: Any) -> Dict[str, Any]:
        return {"status": "skipped", "reason": reason, **base, **extra}

    if record.get("state") != "open":
        return skipped("cycle-not-open", cycle_state=record.get("state"))
    directory = cycle_dir(root, record["campaign_id"], cid, record)
    if (directory / "manifest.json").exists():
        return skipped("sealed-manifest-present")
    try:
        route = load_route(root, Path(record["route_file"]))
    except ProducerError as exc:
        return skipped("route-unreadable", detail=exc.code)
    if route["route_hash"] != record.get("route_hash"):
        return skipped("route-hash-drift")
    if route_is_closed(root, route):
        return skipped("route-closed")
    supported, release = _route_release_supports_interim(route)
    if not supported:
        return skipped("route-release-predates-interim", runtime_root=release)
    state_path = checkpoint_state_path(root, cid)
    state = _read_json(state_path) or {}
    observed_scan = state.get("last_scan_at")
    interval = checkpoint_interval_seconds()
    if (isinstance(observed_scan, (int, float)) and not isinstance(observed_scan, bool)
            and clock - observed_scan < interval):
        return skipped("min-interval", next_eligible_at=_rfc3339(observed_scan + interval))
    limits = limits or CheckpointLimits()
    previous_stats = state.get("stats") if isinstance(state.get("stats"), dict) else {}
    scan = _checkpoint_scan(directory, previous_stats, limits)
    try:
        with _checkpoint_lock(root, cid, timeout=0.0):
            result = _checkpoint_commit(
                root, record, route, directory, scan, observed_scan=observed_scan, clock=clock,
                trigger=trigger, limits=limits, interval=interval, base=base,
                allocator=allocator or artifact_identity.IdAllocator(),
            )
    except ProducerError as exc:
        if exc.code == "checkpoint-lock-busy":
            return skipped("busy")
        raise
    if result["status"] in {"emitted", "unchanged"}:
        artifact_cycle_titles.emit_after_checkpoint(root, record)
    return result


def _checkpoint_commit(
    root: Path, record: Mapping[str, Any], route: Mapping[str, Any], directory: Path,
    scan: Mapping[str, Any], *, observed_scan: Any, clock: float, trigger: str,
    limits: CheckpointLimits, interval: float, base: Mapping[str, Any],
    allocator: artifact_identity.IdAllocator,
) -> Dict[str, Any]:
    cid = record["cycle_id"]
    fresh = read_cycle_record(root, cid)
    if fresh is None:
        return {"status": "skipped", "reason": "cycle-record-unreadable", **base}
    if fresh.get("state") != "open":
        # Sealed or dropped while this checkpoint scanned: whatever interim
        # files remain (the lock this call just re-created, at least) are garbage.
        remove_interim(root, cid)
        return {"status": "skipped", "reason": "cycle-not-open", **base}
    if (directory / "manifest.json").exists():
        # Still open with a manifest present: a seal in flight or a torn one.
        # The reservation stays -- a re-run finalize must find it.
        return {"status": "skipped", "reason": "sealed-manifest-present", **base}
    state_path = checkpoint_state_path(root, cid)
    state = _read_json(state_path) or {}
    if state.get("last_scan_at") != observed_scan:
        # Another checkpoint scanned and settled while this one scanned; its
        # result is newer than nothing and this scan may predate its files.
        return {"status": "skipped", "reason": "superseded", **base}
    interim_path = open_manifest_path(root, cid)
    had_interim = interim_path.exists()
    previous_stats = state.get("stats") if isinstance(state.get("stats"), dict) else {}
    bookkeeping: Dict[str, Any] = {
        "schema_version": 1, "cycle_id": cid, "campaign_id": fresh["campaign_id"],
        "route_id": fresh.get("route_id"), "cycle_path": os.path.relpath(str(directory), str(root)),
        "open_manifest": os.path.relpath(str(interim_path), str(root)),
        "last_scan_at": state.get("last_scan_at"), "last_scan_on": state.get("last_scan_on"),
        "last_trigger": trigger, "last_emitted_at": state.get("last_emitted_at"),
        "output_digest": state.get("output_digest"), "limits": limits.to_payload(),
        "min_interval_seconds": interval, "stats": previous_stats,
    }

    def settle(status: str, reason: Optional[str], *, scanned: bool = True, **extra: Any) -> Dict[str, Any]:
        if scanned:
            bookkeeping.update({"last_scan_at": clock, "last_scan_on": _rfc3339(clock)})
        bookkeeping.update({"last_status": status, "last_reason": reason})
        bookkeeping.update({key: value for key, value in extra.items() if key in {
            "stats", "excluded", "artifact_count", "total_bytes", "output_digest",
            "last_emitted_at", "manifest_id", "manifest_revision_id"}})
        _ensure_dir(state_path.parent)
        _write_atomic(state_path, _json_bytes(bookkeeping), 0o644)
        result = {"status": status, **base, **{k: v for k, v in extra.items() if k != "stats"}}
        if reason:
            result["reason"] = reason
        return result

    moved = _place_loose_outputs(root, fresh, directory)
    if moved:
        scan = _checkpoint_scan(directory, previous_stats, limits)
        base = {**base, "moved_outputs": moved}
    if scan.get("skip"):
        detail = {key: value for key, value in scan.items() if key != "skip"}
        return settle("skipped", scan["skip"], limit_detail=detail)
    facts = scan["facts"]
    common = {"stats": scan["stats"], "excluded": scan["excluded"],
              "artifact_count": len(facts), "total_bytes": scan["total_bytes"]}
    if not had_interim and not facts:
        return settle("skipped", "no-output", scanned=False, **common)
    if (trigger != "explicit" and not had_interim and scan["newest_mtime"]
            and clock - scan["newest_mtime"] > CHECKPOINT_STALE_SECONDS):
        return settle("skipped", "stale-cycle", scanned=False,
                      newest_file_on=_rfc3339(scan["newest_mtime"]), **common)
    reserved, reservation = read_interim_reservation(root, fresh)
    if reservation in ("unreadable", "identity-mismatch"):
        # Never re-issue IDs over a reservation this code cannot read.
        return settle("skipped", f"reservation-{reservation}", **common)
    output_digest = _digest(_canonical([[rel, digest] for rel, digest, _size in facts]))
    reserved_matches = reserved is not None and all(
        rel in reserved.artifacts and reserved.artifacts[rel].content_digest == digest
        for rel, digest, _size in facts)
    if (had_interim and reservation == "present" and reserved_matches
            and state.get("output_digest") == output_digest):
        return settle("unchanged", None, path=str(interim_path), **common)
    try:
        document = build_manifest(
            root, fresh, route, (), state="completed", primary=None, allow_open_route=True,
            allocator=allocator, now=clock, reserved=reserved, interim=True, facts=facts,
        )
    except ProducerError as exc:
        return settle("skipped", exc.code, detail=exc.detail, **common)
    report = artifact_manifest.validate_interim(document)
    if not report.ok:
        return settle("skipped", "interim-invalid",
                      detail=";".join(v.code for v in report.violations), **common)
    # The reservation is written before the document: a crash in between leaves
    # IDs reserved but unpublished, never published but unreserved.
    ledger = reservation_path(root, cid)
    _ensure_dir(ledger.parent)
    _write_atomic(ledger, _json_bytes(_reservation_payload(fresh, document, reserved)), 0o644)
    _ensure_dir(interim_path.parent)
    _write_atomic(interim_path, artifact_manifest.canonical_bytes(document), 0o644)
    return settle(
        "emitted", None, path=str(interim_path), output_digest=output_digest,
        last_emitted_at=clock, manifest_id=document["manifest_id"],
        manifest_revision_id=document["manifest_revision_id"],
        reservation=reservation, **common,
    )


def _remove_empty_cycle(root: Path, record: Mapping[str, Any]) -> None:
    directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
    artifact_locator.prepare_index_update(root, [record["campaign_id"]])
    artifacts = directory / "artifacts"
    binding = directory / artifact_locator.CYCLE_BINDING
    if binding.is_file() and not binding.is_symlink():
        binding.unlink()
    for path in (artifacts, directory):
        try:
            path.rmdir()
        except OSError as exc:
            raise ProducerError("cycle-dir-not-empty", str(path)) from exc
    campaign = read_campaign(root, record["campaign_id"])
    if campaign is not None:
        campaign["cycles"] = [c for c in campaign.get("cycles", []) if c != record["cycle_id"]]
        if not campaign["cycles"] and campaign.get("key", "").endswith(record["route_id"]):
            # Campaign created by this begin and never populated: drop it.
            try:
                campaign_path = campaign_dir(root, campaign["campaign_id"], campaign)
                _campaign_path(root, campaign["campaign_id"], campaign).unlink()
                campaign_path.rmdir()
            except OSError:
                _write_campaign(root, campaign, exclusive=False)
        else:
            _write_campaign(root, campaign, exclusive=False)
    artifact_locator.update_indexes(root, [record["campaign_id"]])


def _review_lease_dir(root: Path, cycle_id: str) -> Path:
    return Path(root) / PRODUCER_REL / REVIEW_LEASE_REL / cycle_id


def _review_lease_path(root: Path, cycle_id: str, attempt_id: str) -> Path:
    return _review_lease_dir(root, cycle_id) / f"{attempt_id}.json"


def _rfc3339_to_epoch(value: str) -> float:
    try:
        return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except ValueError:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _lease_record_is_live(
    record: Optional[Dict[str, Any]], *, root: Optional[Path] = None,
    now: Optional[float] = None,
) -> bool:
    """SD-105/SD-90 evidence hierarchy, cited not redefined (plan.md §6.2):
    exact PID/start/PGID identity, a finite deadline, and judgment-impossible
    inputs (corrupt record, unreadable /proc, clock anomaly, missing field)
    read as *live* -- conservative, so an undecidable lease never lets an
    abandon through (E47-4)."""

    if record is None:
        return False
    if record.get("released_at") is not None:
        return False
    deadline = record.get("deadline")
    if not isinstance(deadline, str):
        return True
    try:
        deadline_ts = _rfc3339_to_epoch(deadline)
    except (ValueError, OverflowError):
        return True
    when = time.time() if now is None else now
    if _is_v2_review_lease(record):
        if record.get("expired") is not False:
            return True
        acquired_at = record.get("acquired_at")
        try:
            acquired_ts = (
                _rfc3339_to_epoch(acquired_at)
                if isinstance(acquired_at, str) else None
            )
        except (ValueError, OverflowError):
            return True
        if root is None or acquired_ts is None or acquired_ts > when or deadline_ts <= acquired_ts:
            return True
        metadata = dict(record)
        metadata["review_cycle_id"] = record.get("cycle_id", "")
        # The record stores the sealed fields while the jobs row mirrors the
        # digest.  Reconstruct that mirror for the closed disposition so a
        # dead exact holder can unblock abandon instead of being treated as
        # malformed forever.
        metadata["review_lease_record_digest"] = review_lease_record_digest(record)
        disposition = review_holder_disposition(record, metadata, root, now=when)
        if disposition.state in {"live", "malformed"}:
            return True
        if disposition.state == "dead":
            return False
        # Valid but unobservable holders remain conservative until the finite
        # stale ceiling, after which time permits recovery but never grants a
        # write.
        return when <= deadline_ts
    if when > deadline_ts:
        return False
    pid = record.get("pid")
    pid_start = record.get("pid_start")
    pgid = record.get("pgid")
    if not isinstance(pid, int) or not isinstance(pid_start, str) or not pid_start or not isinstance(pgid, int):
        return True
    actual_start = process_start_ticks(pid)
    if actual_start is None:
        return False
    if actual_start != pid_start:
        return False
    try:
        actual_pgid = os.getpgid(pid)
    except OSError:
        return False
    return actual_pgid == pgid


def _is_v2_review_lease(record: object) -> bool:
    """Identify the exact-output lease without changing the v1 union seam."""

    return isinstance(record, Mapping) and record.get("schema_version") == 2


def _live_review_lease(
    root: Path, cycle_id: str, *, now: Optional[float] = None
) -> Optional[Path]:
    lease_dir = _review_lease_dir(root, cycle_id)
    if not lease_dir.is_dir():
        return None
    conservative: Optional[Path] = None
    for path in sorted(lease_dir.glob("*.json")):
        record = _read_json(path)
        if record is None:
            # The glob already proved this file exists, so an unparseable
            # read here is corruption, not absence -- conservative live
            # (E47-4), unlike `_lease_record_is_live(None)` below which
            # means "no lease file at this specific path".
            conservative = conservative or path
            continue
        if _lease_record_is_live(record, root=root, now=now):
            # Completed sealing cares about a live exact-report lease wherever
            # it appears in a mixed v1/v2 directory. Preserve the first
            # conservative v1/corrupt candidate for abandon semantics, but let
            # any live v2 report lease win this single union seam.
            if _is_v2_review_lease(record):
                return path
            conservative = conservative or path
    return conservative


def _raise_if_recovery_fenced(root: Path, cycle_id: str, *, now: Optional[float] = None) -> None:
    """Keep recovery from sealing a cycle under a live v2 review lease.

    Recovery is itself a publication path: both a journal roll-forward and
    discovery of an already-published manifest call ``_commit_sealed``.  The
    normal finalize fence therefore has to be repeated immediately before
    that mutation.  Legacy v1 leases remain governed by their existing
    abandon-only policy; only the exact v2 report lease blocks recovery.
    """
    lease_path = _live_review_lease(root, cycle_id, now=now)
    if lease_path is None:
        return
    lease = _read_json(lease_path)
    if _is_v2_review_lease(lease):
        raise ProducerError("cycle-finalize-blocked-live-review", cycle_id)


def _path_entry_present(path: Path) -> bool:
    """Return false only for an absent directory entry.

    Publication admission is fail-closed.  A dangling symlink, directory,
    special node, unreadable regular file, or lookup error is still an entry;
    none may be collapsed into the same state as ENOENT by ``exists`` or
    ``is_file``.
    """

    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _raise_if_review_publication_started(
    root: Path, record: Mapping[str, Any]
) -> None:
    """Refuse a new v2 lease once either publication commit path has begun."""

    cycle_id = str(record.get("cycle_id", ""))
    directory = cycle_dir(
        root, str(record.get("campaign_id", "")), cycle_id, record,
    )
    for entry in (journal_path(root, cycle_id), directory / "manifest.json"):
        if _path_entry_present(entry):
            raise ProducerError(
                "review-lease-admission-after-publication",
                f"{cycle_id}: {entry}",
            )


def prepare_review_output_binding(
    root: Path, *, cycle_id: str, producer_id: str, attempt_id: str,
    review_output: str | Path, capability: str, unit: str,
    worktree: str | Path,
) -> Dict[str, Any]:
    """Validate the immutable cycle/route side before registry mutation."""

    root_input = Path(root)
    canonical_root = root_input.resolve(strict=False)
    worktree_input = Path(worktree)
    canonical_worktree = worktree_input.resolve(strict=False)
    if (
        not root_input.is_absolute() or str(root_input) != str(canonical_root)
        or not worktree_input.is_absolute()
        or str(worktree_input) != str(canonical_worktree)
    ):
        raise ProducerError("review-binding-root-not-canonical", str(root))
    record = read_cycle_record(canonical_root, cycle_id)
    if record is None or record.get("state") != "open":
        raise ProducerError("cycle-not-open", cycle_id)
    if record.get("producer_id") != producer_id:
        raise ProducerError("review-binding-producer-mismatch", producer_id)
    # Fast preclaim refusal.  review_lease_acquire repeats this check while it
    # owns the canonical admission lock, closing publication after prepare.
    _raise_if_review_publication_started(canonical_root, record)
    route = load_route(canonical_root, Path(str(record.get("route_file", ""))))
    expected_capability = str(record.get("capability", ""))
    if (
        capability != expected_capability
        or route.get("capability") != expected_capability
        or route.get("route_id") != record.get("route_id")
        or route.get("route_hash") != record.get("route_hash")
    ):
        raise ProducerError("review-binding-capability-mismatch", capability)
    if unit != "qa/code-review":
        raise ProducerError("review-binding-unit-mismatch", unit)
    if Path(str(route.get("cwd", ""))).resolve(strict=False) != canonical_worktree:
        raise ProducerError("review-binding-worktree-mismatch", str(worktree))
    if Path(str(route.get("artifact_root", ""))).resolve(strict=False) != canonical_root:
        raise ProducerError("review-binding-artifact-root-mismatch", str(root))
    output_input = Path(review_output)
    output = output_input.resolve(strict=False)
    if not output_input.is_absolute() or str(output_input) != str(output):
        raise ProducerError("review-output-path-not-canonical", str(review_output))
    artifacts = (
        cycle_dir(canonical_root, record["campaign_id"], cycle_id, record)
        / "artifacts"
    ).resolve(strict=False)
    try:
        locator = output.relative_to(canonical_root).as_posix()
        cycle_locator = output.relative_to(artifacts)
    except ValueError as exc:
        raise ProducerError("review-output-outside-cycle", str(output)) from exc
    if not cycle_locator.parts or cycle_locator.parts[0] != "plans":
        raise ProducerError("review-output-bucket-forbidden", str(output))
    if output.is_dir() or output.exists() and not output.is_file():
        raise ProducerError("review-output-target-invalid", str(output))
    current = output
    while current != artifacts:
        if current.is_symlink():
            raise ProducerError("review-output-symlink", str(current))
        current = current.parent
    binding: Dict[str, Any] = {
        "schema_version": 2,
        "attempt_id": attempt_id,
        "cycle_id": cycle_id,
        "producer_id": producer_id,
        "worktree": str(canonical_worktree),
        "artifact_root": str(canonical_root),
        "capability": capability,
        "unit": unit,
        "output_path": str(output),
    }
    binding["digest"] = review_output_binding_digest(binding)
    binding["locator_b64"] = encode_review_output_locator(locator)
    return binding


def review_output_write_authorized_from_cycle(
    root: Path, *, jobs: str | Path, attempt_id: str, cycle_id: str,
    review_output: str | Path,
) -> bool:
    """Authorize one report using cycle/route/registry facts, never env axes."""

    root_input = Path(root)
    canonical_root = root_input.resolve(strict=False)
    if not root_input.is_absolute() or str(root_input) != str(canonical_root):
        return False
    try:
        record = read_cycle_record(canonical_root, cycle_id)
        if record is None or record.get("state") != "open":
            return False
        route = load_route(
            canonical_root, Path(str(record.get("route_file", "")))
        )
        producer_id = str(record.get("producer_id", ""))
        capability = str(record.get("capability", ""))
        worktree = str(Path(str(route.get("cwd", ""))).resolve(strict=False))
        binding = prepare_review_output_binding(
            canonical_root, cycle_id=cycle_id, producer_id=producer_id,
            attempt_id=attempt_id, review_output=review_output,
            capability=capability, unit="qa/code-review", worktree=worktree,
        )
        lease_record = _read_json(
            _review_lease_path(canonical_root, cycle_id, attempt_id)
        )
        return review_output_write_authorized(
            jobs, output_path=binding["output_path"], attempt_id=attempt_id,
            cycle_id=cycle_id, producer_id=producer_id,
            capability=capability, unit="qa/code-review", worktree=worktree,
            artifact_root=canonical_root, lease_record=lease_record,
        )
    except (ProducerError, OSError, TypeError, ValueError):
        return False


def review_lease_acquire(
    root: Path, *, cycle_id: str, attempt_id: str, deadline_seconds: float = 900.0,
    now: Optional[float] = None, review_output: Optional[str | Path] = None,
    binding: Optional[Mapping[str, Any]] = None,
    governed_identity: Optional[Mapping[str, Any]] = None,
    jobs: Optional[str | Path] = None,
    watchdog_budget: Optional[FiniteWatchdogBudget] = None,
) -> Dict[str, Any]:
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        path = _review_lease_path(root, cycle_id, attempt_id)
        try:
            path.lstat()
            existing_path = True
        except FileNotFoundError:
            existing_path = False
        existing = _read_json(path)
        when = time.time() if now is None else now
        v2_requested = review_output is not None or binding is not None
        if v2_requested and watchdog_budget is None:
            watchdog_budget = begin_finite_watchdog(
                deadline_seconds, origin_epoch=when
            )
        if v2_requested and not isinstance(watchdog_budget, FiniteWatchdogBudget):
            raise ProducerError("review-lease-budget-invalid", attempt_id)
        if v2_requested and watchdog_budget is not None:
            if remaining_watchdog_seconds(watchdog_budget) <= 0:
                raise ProducerError("review-lease-budget-exhausted", attempt_id)
            if when < watchdog_budget.origin_epoch:
                raise ProducerError("review-lease-budget-contradictory", attempt_id)
        # E47-9: the same (cycle, attempt) re-acquiring its own still-live
        # lease is an idempotent no-op -- zero state change.
        existing_live = existing is not None and _lease_record_is_live(
            existing, root=root, now=when
        )
        schema_version = 1
        exact_output = None
        registry_metadata: Mapping[str, object] = {}
        if v2_requested:
            if review_output is None or binding is None or jobs is None:
                raise ProducerError("review-lease-binding-incomplete", cycle_id)
            if not isinstance(binding, Mapping):
                raise ProducerError("review-lease-binding-invalid", attempt_id)
            admission_record = read_cycle_record(root, cycle_id)
            if admission_record is not None and admission_record.get("state") == "open":
                # This is the authoritative race-closing check: the admission
                # mutex above is still held and publication uses that same
                # mutex.  No recovery or nested lock acquisition occurs here.
                _raise_if_review_publication_started(root, admission_record)
            try:
                canonical_binding = prepare_review_output_binding(
                    root, cycle_id=cycle_id,
                    producer_id=str(binding.get("producer_id", "")),
                    attempt_id=attempt_id, review_output=review_output,
                    capability=str(binding.get("capability", "")),
                    unit=str(binding.get("unit", "")),
                    worktree=str(binding.get("worktree", "")),
                )
                checked_binding = validate_review_output_binding(
                    jobs, attempt_id=attempt_id, output_path=review_output,
                    cycle_id=cycle_id,
                    producer_id=str(canonical_binding["producer_id"]),
                    capability=str(canonical_binding["capability"]),
                    unit=str(canonical_binding["unit"]),
                    worktree=str(canonical_binding["worktree"]),
                    artifact_root=str(canonical_binding["artifact_root"]),
                )
            except Exception as exc:
                if isinstance(exc, (OSError, ValueError, TypeError)):
                    raise ProducerError("review-lease-binding-invalid", attempt_id) from exc
                raise ProducerError(getattr(exc, "reason", "review-lease-binding-invalid"), attempt_id) from exc
            exact_output = Path(checked_binding["output_path"])
            closed_fields = (
                "schema_version", "attempt_id", "cycle_id", "producer_id",
                "worktree", "artifact_root", "capability", "unit",
                "output_path", "digest", "locator_b64",
            )
            if any(binding.get(key) != canonical_binding.get(key) for key in closed_fields):
                raise ProducerError("review-lease-binding-mismatch", attempt_id)
            if checked_binding["digest"] != canonical_binding["digest"]:
                raise ProducerError("review-binding-digest-mismatch", attempt_id)
            identity = dict(governed_identity or {})
            required_identity = ("pid", "pid_start", "pgid", "pid_ns", "pid_observer_ns")
            if not all(identity.get(key) not in (None, "") for key in required_identity):
                raise ProducerError("review-governed-identity-incomplete", cycle_id)
            if not str(identity.get("pid")).isdigit() or not str(identity.get("pgid")).isdigit():
                raise ProducerError("review-governed-identity-invalid", cycle_id)
            registry_metadata = checked_binding.get("_registry_metadata", {})
            if not isinstance(registry_metadata, Mapping):
                raise ProducerError("review-lease-binding-invalid", attempt_id)
            if any(
                str(registry_metadata.get(key, "")) != str(identity.get(key, ""))
                for key in _PROCESS_IDENTITY_METADATA_KEYS
            ):
                raise ProducerError("review-lease-identity-mismatch", attempt_id)
            nonce = identity.get("review_governed_lease_nonce", registry_metadata.get("review_governed_lease_nonce"))
            if not isinstance(nonce, str) or REVIEW_GOVERNED_LEASE_NONCE_RE.fullmatch(nonce) is None:
                raise ProducerError("review-governed-lease-nonce-invalid", attempt_id)
            if (
                registry_metadata.get("review_governed_lease") != REVIEW_GOVERNED_LEASE_KIND
                or registry_metadata.get("review_governed_lease_nonce") != nonce
            ):
                raise ProducerError("review-governed-lease-nonce-mismatch", attempt_id)
            if not review_governed_lease_is_held(root, registry_metadata):
                raise ProducerError("review-governed-lease-not-held", attempt_id)
            schema_version = 2
            binding_digest = str(canonical_binding["digest"])
        else:
            identity = {}
            binding_digest = None
        if existing_live:
            if schema_version == 2:
                expected_existing = {
                    "schema_version": 2, "cycle_id": cycle_id,
                    "attempt_id": attempt_id, "producer_id": canonical_binding["producer_id"],
                    "worktree": canonical_binding["worktree"],
                    "artifact_root": canonical_binding["artifact_root"],
                    "capability": canonical_binding["capability"],
                    "unit": canonical_binding["unit"],
                    "review_output_path": str(exact_output),
                    "review_output_digest": binding_digest,
                    "review_governed_lease": registry_metadata.get("review_governed_lease"),
                    "review_governed_lease_nonce": registry_metadata.get("review_governed_lease_nonce"),
                }
                if any(existing.get(key) != value for key, value in expected_existing.items()):
                    raise ProducerError("review-lease-binding-mismatch", attempt_id)
                for key in _PROCESS_IDENTITY_METADATA_KEYS:
                    if str(existing.get(key, "")) != str(identity.get(key, "")):
                        raise ProducerError("review-lease-identity-mismatch", attempt_id)
                digest = review_lease_record_digest(existing)
                return {
                    "status": "already-held", "cycle_id": cycle_id,
                    "attempt_id": attempt_id,
                    "registry_metadata": {
                        "review_lease_acquired_at": existing.get("acquired_at", ""),
                        "review_lease_deadline": existing.get("deadline", ""),
                        "review_lease_record_digest": digest,
                    },
                }
            return {"status": "already-held", "cycle_id": cycle_id, "attempt_id": attempt_id}
        if schema_version == 2 and existing_path:
            raise ProducerError("review-lease-existing-invalid", attempt_id)
        pid = int(identity.get("pid", os.getpid()))
        pid_start = str(identity.get("pid_start", process_start_ticks(pid) or ""))
        pgid = int(identity.get("pgid", os.getpgid(pid)))
        lease_seconds = (
            watchdog_budget.timeout_seconds
            if schema_version == 2 and watchdog_budget is not None
            else max(1.0, deadline_seconds)
        )
        record = {
            "schema_version": schema_version, "cycle_id": cycle_id, "attempt_id": attempt_id,
            "pid": pid, "pid_start": pid_start,
            "pgid": pgid, "acquired_at": _rfc3339(when),
            # The audit wall deadline belongs to the one launch-origin clock,
            # not to the later lease-acquisition moment.  ``acquired_at``
            # remains the real acquisition observation for future-timestamp
            # rejection and audit coherence.
            "deadline": (_rfc3339_precise(
                watchdog_budget.origin_epoch + watchdog_budget.timeout_seconds
                if schema_version == 2 and watchdog_budget is not None
                else when + lease_seconds
            ) if schema_version == 2 else _rfc3339(when + lease_seconds)),
            "released_at": None, "expired": False,
        }
        if schema_version == 2:
            record.update({
                "review_output_path": str(exact_output),
                "review_output_digest": binding_digest,
                "worktree": canonical_binding["worktree"],
                "artifact_root": canonical_binding["artifact_root"],
                "capability": canonical_binding["capability"],
                "unit": canonical_binding["unit"],
                "producer_id": canonical_binding["producer_id"],
                "review_governed_lease": registry_metadata.get("review_governed_lease"),
                "review_governed_lease_nonce": registry_metadata.get("review_governed_lease_nonce"),
                "watchdog_timeout_seconds": watchdog_budget.timeout_seconds,
                "watchdog_origin_monotonic_ns": watchdog_budget.origin_monotonic_ns,
                "watchdog_deadline_monotonic_ns": watchdog_budget.deadline_monotonic_ns,
                "watchdog_origin_epoch": watchdog_budget.origin_epoch,
                "watchdog_deadline_epoch": watchdog_budget.origin_epoch + watchdog_budget.timeout_seconds,
                "watchdog_budget_digest": watchdog_budget.digest,
            })
            for key in _PROCESS_IDENTITY_METADATA_KEYS:
                if key in identity:
                    record[key] = identity[key]
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(path, _json_bytes(record), 0o600)
        result = {"status": "acquired", "cycle_id": cycle_id, "attempt_id": attempt_id}
        if schema_version == 2:
            result["registry_metadata"] = {
                "review_lease_acquired_at": record["acquired_at"],
                "review_lease_deadline": record["deadline"],
                "review_lease_record_digest": review_lease_record_digest(record),
            }
        return result
    finally:
        artifact_admission._release_lock(root, lock_fd)


def review_lease_release(
    root: Path, *, cycle_id: str, attempt_id: str, now: Optional[float] = None,
) -> Dict[str, Any]:
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        path = _review_lease_path(root, cycle_id, attempt_id)
        existing = _read_json(path)
        if existing is None or existing.get("released_at") is not None:
            # E47-9: releasing an already-released (or never-acquired) lease
            # is an idempotent no-op.
            return {"status": "already-released", "cycle_id": cycle_id, "attempt_id": attempt_id}
        existing["released_at"] = _rfc3339(time.time() if now is None else now)
        _write_atomic(path, _json_bytes(existing), 0o600)
        return {"status": "released", "cycle_id": cycle_id, "attempt_id": attempt_id}
    finally:
        artifact_admission._release_lock(root, lock_fd)


def review_lease_status(root: Path, *, cycle_id: str, attempt_id: Optional[str] = None) -> Dict[str, Any]:
    root = Path(root).resolve()
    if attempt_id is not None:
        record = _read_json(_review_lease_path(root, cycle_id, attempt_id))
        return {
            "cycle_id": cycle_id, "attempt_id": attempt_id,
            "live": _lease_record_is_live(record, root=root),
        }
    live_path = _live_review_lease(root, cycle_id)
    return {"cycle_id": cycle_id, "live": live_path is not None}


_SEALED_CYCLE_STATES = {"active", "completed", "abandoned"}


def _valid_cycle_state(value: Any) -> bool:
    """`value` is a genuine member of the cycle work-state enum only if it is a
    *string* member of it.

    The type check has to live inside this predicate: JSON can put a list or
    dict in this slot, and a bare `value in _SEALED_CYCLE_STATES` raises
    `TypeError: unhashable type` for either -- an exception that is not a
    `ProducerError` and so is not caught by `main()`'s `except ProducerError
    as exc:` arm. Every state comparison, manifest side or record-cache side,
    goes through this one function; nowhere else tests set membership
    directly.
    """
    return isinstance(value, str) and value in _SEALED_CYCLE_STATES


def _record_cycle_manifest_path(root: Path, record: Mapping[str, Any]) -> Path:
    """Resolve this record's manifest without scanning other cycle bindings."""
    root = Path(root).resolve()
    campaign_id = record.get("campaign_id")
    cycle_id = record.get("cycle_id")
    if not artifact_identity.is_well_formed(campaign_id, "campaign") or not artifact_identity.is_well_formed(cycle_id, "cycle"):
        raise ProducerError("sealed-cycle-state-unknown", f"{cycle_id}: record identity invalid")
    try:
        campaigns = artifact_locator.safe_child(root, root, "campaigns")
    except artifact_locator.LocatorError as exc:
        raise ProducerError("sealed-cycle-state-unknown", exc.detail or exc.code) from exc
    matches = []
    if campaigns.is_dir() and not campaigns.is_symlink():
        for candidate in campaigns.iterdir():
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            campaign = _read_json(candidate / "campaign.json")
            if campaign is not None and campaign.get("campaign_id") == campaign_id:
                matches.append(candidate)
    if len(matches) != 1:
        raise ProducerError(
            "sealed-cycle-state-unknown",
            f"{cycle_id}: campaign-locator={'missing' if not matches else 'ambiguous'}",
        )
    parent = matches[0]
    locator = record.get("locator")
    try:
        if locator:
            cycle_path = artifact_locator.safe_child(root, parent, locator)
            binding = artifact_locator.read_cycle_binding(cycle_path)
            if binding is None:
                raise ProducerError(
                    "sealed-cycle-state-unknown",
                    f"{cycle_id}: cycle-binding=missing",
                )
        else:
            cycle_path = artifact_locator.safe_child(
                root, artifact_locator.safe_child(root, parent, "cycles"), cycle_id
            )
            # Legacy ``cycles/<cycle_id>`` directories predate cycle bindings.
            # If one is present it must still agree with the record; absence is
            # allowed only because the stable ID is the legacy path component.
            binding = artifact_locator.read_cycle_binding(cycle_path)
    except artifact_locator.LocatorError as exc:
        detail = exc.code if not exc.detail else f"{exc.code}: {exc.detail}"
        raise ProducerError("sealed-cycle-state-unknown", detail) from exc
    if binding is not None and (
        binding.get("campaign_id") != campaign_id
        or binding.get("cycle_id") != cycle_id
    ):
        raise ProducerError(
            "sealed-cycle-state-unknown",
            f"{cycle_id}: cycle-binding-identity-mismatch",
        )
    return cycle_path / "manifest.json"


def _published_cycle_state(root: Path, record: Mapping[str, Any]) -> str:
    """Return the *work* state of a sealed cycle.

    `record["state"] == "sealed"` is a storage fact -- it means an immutable
    snapshot exists, not that the work is done (D-10: the four completion
    results are independent). The work state is the published manifest's
    `cycle.state` (the D-6 folded state); `record["cycle_state"]` is a cache
    `_commit_sealed` copied from that same document. The cache is only a
    fallback when the canonical source is entirely absent. If the canonical
    source exists but cannot be trusted -- unreadable, wrong shape, naming a
    different cycle, or holding a value outside the enum -- this raises a
    typed refusal instead of ever returning a false success. The only
    exception this function can raise is `ProducerError`.
    """
    cycle_id = str(record.get("cycle_id", "?"))
    cached = record.get("cycle_state")
    record_state = cached if _valid_cycle_state(cached) else None
    try:
        manifest_path = cycle_dir(root, record["campaign_id"], cycle_id, record) / "manifest.json"
    except artifact_locator.LocatorError:
        # The global index can fail because of an unrelated binding. Resolve
        # this record through its own campaign/cycle locator before deciding
        # that the canonical manifest is absent.
        manifest_path = _record_cycle_manifest_path(root, record)
    except (ProducerError, OSError, KeyError, TypeError):
        manifest_path = _record_cycle_manifest_path(root, record)
    if manifest_path is None:
        raise ProducerError("sealed-cycle-state-unreadable", f"{cycle_id}: manifest=path-unresolved")
    try:
        manifest_stat = manifest_path.lstat()
    except FileNotFoundError:
        # Canonical source absent is the *only* case that falls back to the
        # cache (compatibility for W7G/W7I/W7H relocation roots carrying
        # legacy sealed records). A directory, special node, or symlink is
        # present-but-invalid and must never be mistaken for absence.
        if record_state is not None:
            return record_state
        shown = "missing" if cached is None else repr(cached)
        raise ProducerError("sealed-cycle-state-unknown", f"{cycle_id}: manifest=absent record={shown}")
    except OSError as exc:
        raise ProducerError(
            "sealed-cycle-state-unreadable", f"{cycle_id}: manifest={manifest_path} lookup-error={exc.__class__.__name__}:{exc}"
        ) from exc
    if not stat.S_ISREG(manifest_stat.st_mode):
        raise ProducerError(
            "sealed-cycle-state-unreadable", f"{cycle_id}: manifest={manifest_path} entry-kind=non-regular"
        )
    document = _read_json(manifest_path)
    if document is None:  # unparsable JSON, symlink, or encoding error
        raise ProducerError("sealed-cycle-state-unreadable", f"{cycle_id}: manifest={manifest_path} unparsable")
    # Structure checks come before any field access. `_read_json:168` already
    # guarantees a dict, but the invariant is pinned here too so it keeps
    # holding even if `_read_json` is loosened later.
    if not isinstance(document, Mapping):
        raise ProducerError(
            "sealed-cycle-state-unreadable",
            f"{cycle_id}: manifest={manifest_path} document-structure type={type(document).__name__}",
        )
    cycle = document.get("cycle")
    if not isinstance(cycle, Mapping):
        raise ProducerError(
            "sealed-cycle-state-unreadable",
            f"{cycle_id}: manifest={manifest_path} cycle-structure type={type(cycle).__name__}",
        )
    manifest_cycle_id = cycle.get("cycle_id")  # `!=` is safe against any type
    if manifest_cycle_id != record.get("cycle_id"):
        raise ProducerError(
            "sealed-cycle-state-unreadable",
            f"{cycle_id}: manifest={manifest_path} manifest_cycle_id={manifest_cycle_id!r} "
            f"record_cycle_id={record.get('cycle_id')!r}",
        )
    manifest_state = cycle.get("state")
    if not _valid_cycle_state(manifest_state):  # non-string and out-of-enum share one gate
        raise ProducerError(
            "sealed-cycle-state-unreadable", f"{cycle_id}: manifest={manifest_path} cycle_state={manifest_state!r}"
        )
    if record_state is not None and record_state != manifest_state:
        raise ProducerError("sealed-cycle-state-ambiguous", f"{cycle_id}: manifest={manifest_state} record={record_state}")
    # A damaged cache (non-string, out-of-enum, or absent) does not block
    # success once the canonical source is valid -- the canonical source wins.
    return manifest_state


def _authorize_active_cleanup(root: Path, operation: str, target: Path, cycle_id: Optional[str]) -> None:
    try:
        dispatch_terminal_commit.require_current_cleanup(operation, target=target, cycle_id=cycle_id)
    except dispatch_terminal_commit.TerminalCommitError as exc:
        raise ProducerError("cleanup-scope-violation", exc.detail) from exc


def _finalize_route(root: Path, record: Mapping[str, Any]) -> Dict[str, Any]:
    """D-120 finalize: R is the unique T(C) leaf -- the route with no
    material-input-qualifying continuation child. In a closed lineage the
    children that begin another cycle are that cycle's, so R stops before them
    (`closed_lineage_handover`). `--cycle`-only finalize has
    no other way to name R; a completion controller that already knows its
    exact route can seal it directly by checking `cycle_route_admission(...,
    finalize=True)` itself instead of calling this walk.
    """
    begin_route = load_route(root, Path(record["route_file"]))
    if begin_route["route_hash"] != record["route_hash"]:
        raise ProducerError("route-hash-drift", record["cycle_id"])
    current = begin_route
    visited = {current["route_id"]}
    handed_over: Optional[frozenset] = None  # computed once, and only when a child begins another cycle
    begin_ids: Optional[Set[Any]] = None
    while True:
        candidates = [c for c in _lineage_children(root, current["route_id"], current["route_hash"])
                      if c.get("capability") == record.get("capability")
                      and c.get("effective_intensity") == record.get("intensity")]
        if candidates and handed_over is None:
            if begin_ids is None:
                begin_ids = {rec.get("route_id") for rec in list_cycle_records(root)
                             if rec.get("cycle_id") != record.get("cycle_id")}
            if any(c["route_id"] in begin_ids for c in candidates):
                handed_over = _handed_over_routes(root, record)
        if handed_over:
            candidates = [c for c in candidates if c["route_id"] not in handed_over]
        if not candidates:
            return current
        if len(candidates) > 1:
            raise ProducerError(
                "cycle-route-binding-mismatch:lineage-fork",
                f"{current['route_id']}:{','.join(sorted(c['route_id'] for c in candidates))}",
            )
        nxt = candidates[0]
        if nxt["route_id"] in visited:
            raise ProducerError("route-lineage-unverified", f"cycle:{nxt['route_id']}")
        visited.add(nxt["route_id"])
        current = nxt


def finalize(
    root: Path,
    *,
    cycle_id: str,
    state: str = "completed",
    primary: Optional[str] = None,
    publication: str = "not-offered",
    allow_open_route: bool = False,
    allocator: Optional[artifact_identity.IdAllocator] = None,
    now: Optional[float] = None,
    crash_after_manifest: bool = False,
    exclude_hidden: bool = False,
    adopt_root_outputs: Sequence[str] = (),
    abandon_reason: Optional[str] = None,
    force_abandon_ignoring_lease: bool = False,
    support_locators: Sequence[str] = (),
    expected_binding: Optional[Mapping[str, Any]] = None,
    lock_timeout: Optional[float] = None,
    exclude_symlinks: bool = False,
    _admission_lock_fd: Optional[int] = None,
    _recovery_scope: str = "root",
) -> Dict[str, Any]:
    """`lock_timeout` bounds both lock waits (default: the admission and
    checkpoint defaults); a background sweep passes 0 so a held lock defers it.
    `exclude_symlinks` (abandoned only) leaves symbolic links out of the manifest
    and records them as `excluded_symlinks` on the result and the cycle record."""
    root = Path(root).resolve()
    if _recovery_scope != "exact":
        dispatch_terminal_commit.require_current_cleanup("producer-finalize")
    _authorize_active_cleanup(root, "finalize-forward-recovery", root, cycle_id)
    if state not in {"completed", "abandoned"}:
        raise ProducerError("finalize-state-invalid", state)
    if exclude_symlinks and state != "abandoned":
        raise ProducerError("symlink-exclusion-requires-abandoned", state)
    if _admission_lock_fd is not None:
        raise ProducerError("finalize-reentry-forbidden", cycle_id)
    # PRD §13.53.4(3) names the producer admission mutex as the lock that must
    # be released before `finalize()` is entered. The check above only catches
    # a caller that *passes* its fd; a caller holding the lock in its own
    # variable would otherwise block on `flock` for the full admission timeout
    # and surface as "busy". Refuse it here, typed, at the boundary.
    try:
        dispatch_lock_order.assert_not_held("producer-admission", "producer-finalize")
    except dispatch_lock_order.LockOrderError as error:
        raise ProducerError("finalize-reentry-forbidden", error.detail or cycle_id) from error
    alloc = allocator or artifact_identity.IdAllocator()
    lock_fd = _admission_lock_fd
    owns_lock = lock_fd is None
    if owns_lock:
        lock_fd = artifact_admission._acquire_lock(
            root, artifact_admission.LOCK_TIMEOUT_DEFAULT if lock_timeout is None else lock_timeout, now=now)
    interim_guard = contextlib.ExitStack()
    sweep_unresolved: List[Dict[str, Any]] = []

    def _finish(payload: Dict[str, Any]) -> Dict[str, Any]:
        # duplicate-copy: an unrelated campaign the recovery sweep had to
        # isolate (§3.6a) is surfaced on every return, not swallowed.
        if sweep_unresolved:
            payload = dict(payload)
            payload["recovery_unresolved"] = sweep_unresolved
        return payload

    try:
        if (expected_binding and isinstance(expected_binding, Mapping)
                and expected_binding.get("kind") == "inline_producer_binding_v1"):
            # The admission mutex is held. Check the actual route's verified
            # lineage, including valid continuations of the begin route.
            _inline_producer_binding_check(root, cycle_id, expected_binding)
        import inline_finish
        finish = inline_finish.pending_for_cycle(root, cycle_id)
        if finish and finish.get("state") != "finished":
            permitted = (
                isinstance(expected_binding, Mapping)
                and expected_binding.get("kind") == "inline_producer_binding_v1"
                and expected_binding.get("inline_finish_id") == finish.get("inline_finish_id")
                and finish.get("state") == "route-closed"
            )
            if not permitted:
                raise ProducerError("finish-in-progress", cycle_id)
        if _recovery_scope == "root":
            pre = read_cycle_record(root, cycle_id)
            sweep = _recover_locked(root, now=now,
                                    target_campaign_id=pre["campaign_id"] if pre else None)
            sweep_unresolved = sweep.get("unresolved", [])
        elif _recovery_scope == "exact":
            _recover_exact_cycle_locked(root, cycle_id, expected_binding, now=now)
        else:
            raise ProducerError("recovery-scope-invalid", _recovery_scope)
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        if record.get("state") == "sealed":
            # Storage sealing is not task completion: the manifest commit is an
            # immutable snapshot, and the *published* cycle state
            # (`_published_cycle_state`) is the only thing a re-finalize
            # request can be judged against. Any request whose `state` does
            # not match that published state is a conflict -- no flag makes
            # it idempotent, because the storage schema does not persist the
            # request fingerprint needed to prove "same retry" (D-8).
            published = _published_cycle_state(root, record)
            if published != state:
                raise ProducerError(
                    "finalize-state-conflict",
                    f"{cycle_id}: requested={state} published_cycle_state={published} storage_state=sealed",
                )
            # General relocation compatibility may judge an absent manifest
            # from its state cache. Terminal exact callers still require every
            # durable proof; an already-sealed status is never that proof.
            manifest_path = _record_cycle_manifest_path(root, record)
            if expected_binding is not None or _path_entry_present(manifest_path):
                verified = _verify_sealed_cycle_locked(root, record, expected_binding)
                return _finish({**verified, "storage_state": "sealed", "cycle_state": published})
            return _finish({"status": "already-sealed", "cycle_id": cycle_id,
                    "manifest_digest": record.get("manifest_digest"),
                    "storage_state": "sealed", "cycle_state": published})
        if record.get("state") != "open":
            raise ProducerError("cycle-not-open", record.get("state", "?"))
        # The open-cycle checkpoint assigns IDs under this lock; holding it until
        # the interim document is removed keeps a concurrent checkpoint from
        # republishing IDs the sealed manifest did not take.
        interim_guard.enter_context(_checkpoint_lock(
            root, cycle_id, timeout=CHECKPOINT_FINALIZE_LOCK_SECONDS if lock_timeout is None else lock_timeout))
        # A live review lease protects the report's exact write window from
        # both terminal outcomes.  The check remains under the producer
        # admission lock and happens before any terminal mutation.
        live_review = _live_review_lease(root, cycle_id, now=now)
        live_record = _read_json(live_review) if live_review is not None else None
        if state == "abandoned":
            # SD-117 L1 before L3 (plan-check C-2): live-lease enforcement
            # comes first -- a live registered review lease refuses the
            # abandon outright, zero events, zero record-state change
            # (E47-2), before the abandon_reason vocabulary is even
            # consulted.
            if not force_abandon_ignoring_lease and live_review is not None:
                lease = live_record
                reason = ("cycle-finalize-blocked-live-review"
                          if _is_v2_review_lease(lease)
                          else "cycle-abandon-blocked-live-review")
                raise ProducerError(reason, cycle_id)
            if force_abandon_ignoring_lease and abandon_reason not in (None, "operator-override-live-review"):
                raise ProducerError("abandon-reason-required", str(abandon_reason))
            if force_abandon_ignoring_lease:
                abandon_reason = "operator-override-live-review"
            if abandon_reason not in ABANDON_REASONS:
                raise ProducerError("abandon-reason-required", str(abandon_reason))
        elif live_review is not None:
            # SD-120 completed settlement consumes the v1/v2 union. Keep the
            # legacy abandon override above and the v2-only root recovery
            # policy separate from this completed publication boundary.
            raise ProducerError("cycle-finalize-blocked-live-review", cycle_id)
        directory = cycle_dir(root, record["campaign_id"], cycle_id, record)
        # D-120: the route that seals this cycle is the unique T(C) leaf, not
        # necessarily the begin route -- an inherited (rebound) cycle's begin
        # route may long since have a continuation writing it.
        route = _finalize_route(root, record)
        admission = cycle_route_admission(root, record, route, finalize=True)
        if not admission.allow:
            raise ProducerError(admission.reason, admission.detail)
        if _bind_cycle_route_locked(root, record, route)["written"]:
            # `_commit_sealed` below reseals from this local copy; refresh it
            # so the binding write just made is not clobbered back to stale.
            record = read_cycle_record(root, cycle_id)
        artifact_locator.prepare_index_update(root, [record["campaign_id"]])
        adopted_root_outputs: List[str] = []
        if adopt_root_outputs and state != "abandoned":
            raise ProducerError("root-output-adoption-requires-abandoned")
        adoption_moves: List[Tuple[str, Path, Path]] = []
        seen_adoptions: set[str] = set()
        for name in adopt_root_outputs:
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                raise ProducerError("root-output-adoption-name-invalid", name)
            if name in seen_adoptions:
                raise ProducerError("root-output-adoption-duplicate", name)
            seen_adoptions.add(name)
            source = directory / name
            target = directory / "artifacts" / name
            if source.is_symlink() or not source.is_file():
                raise ProducerError("root-output-adoption-source-invalid", name)
            if target.exists() or target.is_symlink():
                raise ProducerError("root-output-adoption-target-exists", name)
            adoption_moves.append((name, source, target))
        for name, source, target in adoption_moves:
            os.replace(source, target)
            adopted_root_outputs.append(name)
        moved_outputs = _place_loose_outputs(root, record, directory)
        placements = _output_placements(root, record)
        primary = _placed_locator(_cycle_relative_primary(primary, directory), placements)
        support_locators = tuple(_placed_locator(value, placements) for value in support_locators)
        excluded_hidden: List[str] = []
        excluded_symlinks: List[str] = []
        rows, violations = _enumerate_output(directory, exclude_hidden=exclude_hidden, excluded=excluded_hidden,
                                             exclude_symlinks=exclude_symlinks,
                                             excluded_symlinks=excluded_symlinks)
        if violations:
            raise ProducerError("output-invalid", ";".join(violations))
        if not rows:
            # D-9: no durable output, no lineage. Unchanged except that an
            # abandoned empty cycle also carries its sealed abandon_reason
            # (E47-5: `_remove_empty_cycle` call and returned `status` stay
            # byte-identical either way).
            _remove_empty_cycle(root, record)
            record = dict(record)
            record["state"] = "abandoned" if state == "abandoned" else "no-lineage"
            record["sealed_on"] = _rfc3339(now)
            if state == "abandoned":
                record["abandon_reason"] = abandon_reason
            _write_cycle_record(root, record, exclusive=False)
            remove_interim(root, cycle_id)
            return _finish({"status": "no-lineage", "cycle_id": cycle_id, "lineage_committed": False})
        # An open predecessor may be linked at begin; the existing manifest
        # index still requires that predecessor to be admitted before its child.
        # Refuse before staging a manifest so the owner can seal the parent and
        # retry without a dangling-parent recovery journal.
        if record.get("parent_cycle_id"):
            parent = read_cycle_record(root, record["parent_cycle_id"])
            if parent is None or parent.get("state") != "sealed":
                raise ProducerError("parent-cycle-not-sealed", record["parent_cycle_id"])
        reserved, reservation = read_interim_reservation(root, record)
        identity = artifact_lifecycle.read_root_identity(root)
        index = artifact_admission.load_index(root)
        # A reservation never blocks a seal: when a reused field makes the
        # document fail, rebuild with fresh events, then with fresh IDs, and say so.
        attempts: List[Tuple[Optional[InterimReservation], Optional[str]]] = [(reserved, None)]
        if reserved is not None:
            attempts += [(_without_event_reuse(reserved), "events-fresh"), (None, "ids-fresh")]
        for candidate, rebuilt in attempts:
            document = build_manifest(
                root, record, route, rows, state=state,
                primary=_cycle_relative_primary(primary, directory),
                allow_open_route=allow_open_route, allocator=alloc, now=now,
                abandon_reason=abandon_reason, support_locators=support_locators,
                reserved=candidate,
            )
            report = artifact_manifest.validate(document)
            if not report.ok:
                failure = ProducerError("manifest-invalid", ";".join(v.code for v in report.violations))
                continue
            digest = artifact_manifest.manifest_digest(document)
            index_report = artifact_index.check(
                index, document, idempotency_key=cycle_id, manifest_digest=digest,
                repository_id=identity.repository_id if identity else None,
            )
            if not index_report.ok:
                failure = ProducerError("index-rejected", ";".join(v.code for v in index_report.violations))
                continue
            reserved, interim_rebuilt, failure = candidate, rebuilt, None
            break
        if failure is not None:
            raise failure
        if state == "completed" and route_is_closed(root, route):
            completion = artifact_lifecycle.evaluate_cycle_completion(
                document, content_root=directory,
                route_file=route_lineage.canonical_route_path(root, route["route_id"]),
                publication=publication, expected_root_id=identity.artifact_root_id if identity else None,
                inline_finish_id=(expected_binding.get("inline_finish_id")
                                  if isinstance(expected_binding, Mapping) else None),
            )
            if not completion.ok:
                raise ProducerError(
                    "completion-rejected",
                    ";".join(f"{v.code}:{v.detail}" for v in completion.reasons),
                )
        manifest_path = directory / "manifest.json"
        if manifest_path.exists():
            raise ProducerError("manifest-already-present", str(manifest_path))
        cycle_path = os.path.relpath(str(directory), str(root))
        if excluded_symlinks:
            # Written before the manifest is published so a crash after the commit
            # point recovers from the on-disk record without losing the exclusion.
            record = dict(record)
            record["excluded_symlinks"] = excluded_symlinks
            _write_cycle_record(root, record, exclusive=False)
        _write_journal(root, cycle_id, state="sealing", manifest_digest=digest, cycle_path=cycle_path)
        # COMMIT POINT: exclusive manifest creation.
        _write_exclusive(manifest_path, artifact_manifest.canonical_bytes(document))
        if crash_after_manifest:  # test hook: simulate a crash after the commit point
            raise artifact_admission.AdmissionRecoveryRequired("simulated crash after manifest publish")
        try:
            _write_journal(root, cycle_id, state="published", manifest_digest=digest, cycle_path=cycle_path)
            _commit_sealed(root, record, document, digest, now=now)
        except BaseException as exc:
            raise artifact_admission.AdmissionRecoveryRequired(
                f"cycle {cycle_id} manifest published but post-publish update failed; run recover"
            ) from exc
        sealed_result = {"excluded_hidden": excluded_hidden, "excluded_symlinks": excluded_symlinks,
            "adopted_root_outputs": adopted_root_outputs,
            "moved_outputs": moved_outputs,
            "status": "sealed", "cycle_id": cycle_id, "campaign_id": record["campaign_id"],
            "manifest_digest": digest, "manifest_path": str(manifest_path),
            "artifact_count": len(rows), "lineage_committed": True, "cycle_state": document["cycle"]["state"],
            "storage_state": "sealed",
        }
        if reservation != "absent":
            # The interim document's IDs were (or, when unusable, were not)
            # carried into the sealed manifest; say which.
            kept = reserved.artifacts if reserved is not None else {}
            sealed_result["interim_ids"] = reservation
            if interim_rebuilt:
                sealed_result["interim_ids_rebuilt"] = interim_rebuilt
            sealed_result["interim_ids_kept"] = sum(
                1 for row in document["artifact_revisions"]
                if row["locator"]["path"] in kept
                and kept[row["locator"]["path"]].artifact_id == row["artifact_id"])
        if document["cycle"]["state"] == "active":
            sealed_result["provisional"] = True
            sealed_result["warning"] = PROVISIONAL_SEAL_WARNING
        return _finish(sealed_result)
    finally:
        interim_guard.close()
        if owns_lock:
            artifact_admission._release_lock(root, lock_fd)


def _recover_exact_cycle_locked(root: Path, cycle_id: str, expected_binding: Optional[Mapping[str, Any]] = None,
                                *, now: Optional[float] = None) -> Dict[str, Any]:
    """Recover only one cycle journal while the admission lock is held."""
    journal_path = producer_dir(root) / "journal" / f"{cycle_id}.json"
    journal = _read_json(journal_path)
    if _path_entry_present(journal_path) and journal is None:
        raise ProducerError("cycle-journal-invalid", cycle_id)
    if journal and journal.get("cycle_id", cycle_id) != cycle_id:
        raise ProducerError("cycle-journal-identity-mismatch", cycle_id)
    if journal and expected_binding and journal.get("manifest_digest") != expected_binding.get("manifest_digest", journal.get("manifest_digest")):
        raise ProducerError("cycle-journal-binding-mismatch", cycle_id)
    if expected_binding:
        record = read_cycle_record(root, cycle_id)
        for key in ("campaign_id", "cycle_id", "producer_id"):
            expected = expected_binding.get(key)
            if expected is not None and (record is None or record.get(key) != expected):
                raise ProducerError("cycle-journal-binding-mismatch", key)
    record = read_cycle_record(root, cycle_id)
    if record is None:
        raise ProducerError("cycle-unknown", cycle_id)
    if _live_review_lease(root, cycle_id, now=now) is not None:
        raise ProducerError("cycle-finalize-blocked-live-review", cycle_id)
    if record.get("state") == "sealed":
        return _verify_sealed_cycle_locked(root, record, expected_binding)
    if record.get("state") != "open":
        raise ProducerError("cycle-not-open", cycle_id)
    directory = cycle_dir(root, record["campaign_id"], cycle_id, record)
    path = directory / "manifest.json"
    if _path_entry_present(path):
        document = _read_json(path)
        if (journal is None or document is None
                or (root / str(journal.get("cycle_path", ""))).resolve() != directory.resolve()
                or artifact_manifest.manifest_digest(document) != journal.get("manifest_digest")):
            raise ProducerError("cycle-journal-manifest-mismatch", cycle_id)
        identity = artifact_lifecycle.read_root_identity(root)
        manifest_route_file, _manifest_route = resolve_cycle_manifest_route(root, record, document)
        completion = artifact_lifecycle.evaluate_cycle_completion(
            document, content_root=directory, route_file=manifest_route_file,
            expected_root_id=identity.artifact_root_id if identity else None,
            inline_finish_id=expected_binding.get("inline_finish_id") if isinstance(expected_binding, Mapping) else None)
        if not completion.ok:
            raise ProducerError("completion-rejected", ";".join(v.code for v in completion.reasons))
        _commit_sealed(root, record, document, journal["manifest_digest"], now=now)
        return _verify_sealed_cycle_locked(root, read_cycle_record(root, cycle_id), expected_binding)
    if journal is not None:
        # Nothing crossed the manifest commit point; the same cycle can
        # re-enter finalize. No other journal or cycle is read or changed.
        _remove_journal(root, cycle_id)
    return {"status": "open", "cycle_id": cycle_id}


def _verify_sealed_cycle_locked(root: Path, record: Mapping[str, Any],
                                expected_binding: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Verify every immutable component before accepting an already-sealed replay."""
    if record.get("state") != "sealed" or not record.get("sealed_on"):
        raise ProducerError("already-sealed-mismatch", "cycle-record")
    if expected_binding is not None:
        for key in ("campaign_id", "cycle_id", "producer_id"):
            expected = expected_binding.get(key)
            if expected is not None and record.get(key) != expected:
                raise ProducerError("already-sealed-mismatch", key)
        if (expected_binding.get("cycle_record_digest")
                and dispatch_terminal_commit.cycle_identity_digest(record) != expected_binding["cycle_record_digest"]):
            raise ProducerError("already-sealed-mismatch", "cycle-identity")
    manifest_path = _record_cycle_manifest_path(root, record)
    directory = manifest_path.parent
    try:
        raw = manifest_path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ProducerError("already-sealed-mismatch", str(manifest_path)) from exc
    canonical = artifact_manifest.canonical_bytes(document)
    digest = artifact_manifest.manifest_digest(document)
    if raw != canonical or digest != record.get("manifest_digest"):
        raise ProducerError("already-sealed-mismatch", "manifest")
    identity = artifact_lifecycle.read_root_identity(root)
    index = artifact_admission.load_index(root)
    row = index.manifests.get(record["cycle_id"]) if hasattr(index, "manifests") else None
    if not isinstance(row, dict) or row.get("manifest_digest") != digest:
        raise ProducerError("already-sealed-mismatch", "index")
    if expected_binding is not None:
        cycle_id = record["cycle_id"]
        cycle = document.get("cycle", {})
        if (identity is None or index.artifact_root_id != identity.artifact_root_id
                or document.get("artifact_root_id") != identity.artifact_root_id
                or cycle.get("cycle_id") != cycle_id
                or cycle.get("campaign_id") != record["campaign_id"]
                or document.get("producer", {}).get("producer_id") != record["producer_id"]):
            raise ProducerError("already-sealed-mismatch", "index-identity")
        # Use the canonical writer's projection, scoped to this exact cycle.
        # Root-wide rebuild/repair could touch unrelated open cycles and is
        # not evidence that this transaction's two index rows were applied.
        expected_index = artifact_index.apply(
            artifact_index.empty(identity.artifact_root_id), document,
            cycle_path=os.path.relpath(str(directory), str(root)),
            manifest_digest=digest, idempotency_key=cycle_id,
        )
        if (row != expected_index.manifests[cycle_id]
                or index.cycles.get(cycle_id) != expected_index.cycles[cycle_id]):
            raise ProducerError("already-sealed-mismatch", "index-projection")
    manifest_route_file, _manifest_route = resolve_cycle_manifest_route(root, record, document)
    if document.get("cycle", {}).get("state") == "completed":
        completion = artifact_lifecycle.evaluate_cycle_completion(
            document, content_root=directory, route_file=manifest_route_file,
            expected_root_id=identity.artifact_root_id if identity else None,
            inline_finish_id=(expected_binding.get("inline_finish_id")
                              if isinstance(expected_binding, Mapping) else None))
        if not completion.ok:
            raise ProducerError("already-sealed-mismatch", "completion-evidence")
    return {"status": "already-sealed", "cycle_id": record["cycle_id"], "manifest_digest": digest}


def finalize_exact_cycle(root: Path, *, cycle_id: str, expected_binding: Mapping[str, Any],
                         state: str = "completed", **kwargs: Any) -> Dict[str, Any]:
    """Finalize one bound cycle under one admission lock, without root recovery."""
    root = Path(root).resolve()
    if expected_binding.get("kind") == "inline_producer_binding_v1":
        # Fast refusal before entering finalize; finalize repeats this *under*
        # its admission lock, so this precheck grants no write authority.
        _inline_producer_binding_check(root, cycle_id, expected_binding)
    _authorize_active_cleanup(root, "finalize-forward-recovery", root, cycle_id)
    # Enter public finalize with no lock held. It owns the one admission
    # boundary encompassing exact recovery, lease check and manifest commit.
    return finalize(root, cycle_id=cycle_id, state=state,
                    expected_binding=expected_binding, _recovery_scope="exact", **kwargs)


def verify_finalized_cycle(root: Path, *, cycle_id: str, expected_binding: Mapping[str, Any]):
    """Read-only proof under admission lock; never repairs an unsealed cycle."""
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT)
    try:
        if expected_binding.get("kind") == "inline_producer_binding_v1":
            _inline_producer_binding_check(root, cycle_id, expected_binding)
        if _live_review_lease(root, cycle_id) is not None:
            raise ProducerError("cycle-finalize-blocked-live-review", cycle_id)
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        return _verify_sealed_cycle_locked(root, record, expected_binding)
    finally:
        artifact_admission._release_lock(root, lock_fd)


def _commit_sealed(
    root: Path, record: Mapping[str, Any], document: Mapping[str, Any], digest: str, *, now: Optional[float]
) -> None:
    directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
    index = artifact_admission.load_index(root)
    if record["cycle_id"] not in index.manifests:
        index = artifact_index.apply(
            index, document, cycle_path=os.path.relpath(str(directory), str(root)),
            manifest_digest=digest, idempotency_key=record["cycle_id"],
        )
        artifact_admission._write_index(root, index)
    sealed = dict(record)
    sealed["state"] = "sealed"
    sealed["sealed_on"] = _rfc3339(now)
    sealed["manifest_digest"] = digest
    sealed["cycle_state"] = document["cycle"]["state"]
    _write_cycle_record(root, sealed, exclusive=False)
    _write_journal(root, record["cycle_id"], state="committed", manifest_digest=digest,
                   cycle_path=os.path.relpath(str(directory), str(root)))
    _remove_journal(root, record["cycle_id"])
    remove_interim(root, record["cycle_id"])
    artifact_locator.update_indexes(root, [record["campaign_id"]])
    artifact_cycle_titles.emit_after_seal_locked(root, sealed, document, directory / "manifest.json")
    try:
        import artifact_workflow_group_review  # lazy: it imports this module
        artifact_workflow_group_review.launch_after_seal(root, sealed)  # groups and metadata, one job
    except Exception:  # noqa: BLE001 -- the review trigger never changes a seal
        pass


# ---------------------------------------------------------------------------
# recover
# ---------------------------------------------------------------------------


def _is_recoverable_locator_defect(exc: BaseException) -> bool:
    """§3.6a's isolation only ever catches a *locator* defect on the record
    being resolved -- never any other `ProducerError` (a live-review fence, a
    digest mismatch, ...), which must keep propagating unconditionally."""

    if isinstance(exc, artifact_locator.LocatorError):
        return True
    return isinstance(exc, ProducerError) and exc.code == "record-locator-invalid"


def _recover_locked(root: Path, *, now: Optional[float] = None,
                    target_campaign_id: Optional[str] = None) -> Dict[str, Any]:
    """Root-scope crash recovery. Visits every open record and journal entry.

    A record whose campaign is not `target_campaign_id` is isolated (§3.6a):
    a locator defect while resolving *that* record (a hand-copied campaign
    folder, a binding conflict) is reported in ``unresolved`` and the record
    is left exactly as found -- no write, still ``open``, journal untouched --
    so the next sweep retries it unchanged. A record whose campaign *is* the
    target still raises, because that is the campaign this operation touches.
    ``target_campaign_id=None`` (bare ``recover()``) isolates every record.
    """

    result: Dict[str, Any] = {
        "rolled_forward": [], "rolled_back": [], "dropped": [], "open": [], "unresolved": [],
    }
    journal_dir = producer_dir(root) / "journal"
    if journal_dir.is_dir():
        for entry in sorted(journal_dir.glob("*.json")):
            journal = _read_json(entry)
            if journal is None:
                entry.unlink()
                continue
            cycle_id = journal.get("cycle_id", entry.stem)
            record = read_cycle_record(root, cycle_id)
            if record is None:
                entry.unlink()
                result["dropped"].append(cycle_id)
                continue
            manifest_path = root / str(journal.get("cycle_path", "")) / "manifest.json"
            document = _read_json(manifest_path)
            if document is not None and artifact_manifest.manifest_digest(document) == journal.get("manifest_digest"):
                _raise_if_recovery_fenced(root, cycle_id, now=now)
                try:
                    artifact_locator.prepare_index_update(root, [record["campaign_id"]])
                    _commit_sealed(root, record, document, journal["manifest_digest"], now=now)
                except (artifact_locator.LocatorError, ProducerError) as exc:
                    if not _is_recoverable_locator_defect(exc) or record["campaign_id"] == target_campaign_id:
                        raise
                    result["unresolved"].append({
                        "cycle_id": cycle_id, "campaign_id": record["campaign_id"],
                        "code": exc.code, "detail": exc.detail, "phase": "journal",
                    })
                    continue
                result["rolled_forward"].append(cycle_id)
            elif document is None:
                # Crash before the commit point: cycle stays open.
                entry.unlink()
                result["rolled_back"].append(cycle_id)
            else:
                raise artifact_admission.AdmissionRecoveryRequired(
                    f"manifest digest mismatch for {cycle_id}; manual inspection required"
                )
    for record in list_cycle_records(root):
        if record.get("state") != "open":
            continue
        try:
            directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
            if not directory.is_dir():
                dropped = dict(record)
                dropped["state"] = "dropped"
                dropped["sealed_on"] = _rfc3339(now)
                _write_cycle_record(root, dropped, exclusive=False)
                result["dropped"].append(record["cycle_id"])
                continue
            if (directory / "manifest.json").is_file():
                document = _read_json(directory / "manifest.json")
                if document is not None:
                    _raise_if_recovery_fenced(root, record["cycle_id"], now=now)
                    artifact_locator.prepare_index_update(root, [record["campaign_id"]])
                    _commit_sealed(root, record, document, artifact_manifest.manifest_digest(document), now=now)
                    result["rolled_forward"].append(record["cycle_id"])
                    continue
        except (artifact_locator.LocatorError, ProducerError) as exc:
            if not _is_recoverable_locator_defect(exc) or record["campaign_id"] == target_campaign_id:
                raise
            result["unresolved"].append({
                "cycle_id": record["cycle_id"], "campaign_id": record["campaign_id"],
                "code": exc.code, "detail": exc.detail, "phase": "open",
            })
            continue
        result["open"].append(record["cycle_id"])
    shared_dir = producer_dir(root) / "shared-journal"
    if shared_dir.is_dir():
        for entry in sorted(shared_dir.glob("*.json")):
            journal = _read_json(entry)
            try:
                _validate_shared_journal(root, entry, journal)
            except ProducerError as exc:
                result["unresolved"].append({"revision_id": entry.stem, "code": exc.code,
                                             "detail": exc.detail, "phase": "shared"})
                continue
            staging = root / journal["staging"]
            target = root / journal["target"]
            if journal.get("state") == "staging" and staging.is_dir():
                reference = _read_json(_reference_path(root, journal["kind"], journal["reference_id"])) or {}
                if target.exists() or journal["expected_previous_revision_id"] != reference.get("latest_revision_id"):
                    result["unresolved"].append({"revision_id": entry.stem, "code": "shared-base-mismatch",
                                                 "detail": "staging ownership or base changed", "phase": "shared"})
                    continue
                shutil.rmtree(str(staging))
                entry.unlink()
                result["rolled_back"].append(journal.get("revision_id", entry.stem))
            elif target.is_dir():
                try:
                    _commit_shared(root, journal)
                except ProducerError as exc:
                    result["unresolved"].append({"revision_id": journal.get("revision_id"),
                                                 "code": exc.code, "detail": exc.detail, "phase": "shared"})
                    continue
                result["rolled_forward"].append(journal.get("revision_id", entry.stem))
            elif journal.get("state") == "published":
                result["unresolved"].append({"revision_id": entry.stem, "code": "shared-journal-mismatch",
                                             "detail": "published target missing", "phase": "shared"})
            else:
                entry.unlink()
                result["rolled_back"].append(journal.get("revision_id", entry.stem))
    removed_interims = _sweep_orphan_interims(root)
    if removed_interims:
        result["interims_removed"] = removed_interims
    return result


def recover(root: Path, *, now: Optional[float] = None) -> Dict[str, Any]:
    dispatch_terminal_commit.require_current_cleanup("root-recover")
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        step1 = artifact_admission._recover_locked(root, now=now)
        producer = _recover_locked(root, now=now)
        locator_index = artifact_locator.verify_indexes(root, repair=True)
        # duplicate-copy: an unrelated campaign's defect stops only itself
        # (locator_index stays "problems", no repair write for it) and the
        # producer sweep's own isolated records (Step 3.9), never this call's
        # exit status -- `recover` keeps reporting success, typed, so a caller
        # retrying its own unrelated seal is not blocked by someone else's
        # hand-copied folder (Step 3.9b).
        status = ("recovered"
                  if not producer.get("unresolved") and locator_index.get("status") in {"current", "rebuilt"}
                  else "recovered-with-problems")
        return {"status": status, "admission": step1, "producer": producer, "locator_index": locator_index}
    finally:
        artifact_admission._release_lock(root, lock_fd)


# ---------------------------------------------------------------------------
# shared admission
# ---------------------------------------------------------------------------


def _reference_path(root: Path, kind: str, ref_id: str) -> Path:
    return Path(root) / "shared" / kind / ref_id / "reference.json"


# Shared kinds a root holds exactly one canonical reference of unless told
# otherwise. `analysis` lineages are per subject (a root legitimately carries
# several); `research` promotions are per promotion.
CANONICAL_SINGLE_REFERENCE_KINDS = ("spec",)


def list_references(root: Path, kind: str) -> List[Dict[str, Any]]:
    base = Path(root) / "shared" / kind
    if not base.is_dir():
        return []
    out = []
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        record = _read_json(entry / "reference.json")
        if record and record.get("shared_reference_id"):
            out.append(record)
    return out


def find_reference_by_key(root: Path, kind: str, key: str) -> Optional[Dict[str, Any]]:
    base = Path(root) / "shared" / kind
    if not base.is_dir():
        return None
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        record = _read_json(entry / "reference.json")
        if record and record.get("key") == key:
            return record
    return None


# --- D-87: component-set preservation -------------------------------------
#
# A shared reference may carry several top-level components (`stage-dispatch/`,
# `agent-fleet-dashboard/`, ...).  `admit_shared` copies only the source tree,
# so a partial admit silently drops every component the source omits and
# `latest_revision_id` then points at the reduced set.  On 2026-09-03 two
# admits one minute apart took `ref_4d540b57...` from 246 files / 3 components
# to 41 files / 1 component.  The contract (a) is a typed refusal, (b) an
# explicit `--drop-component`, and (d) a read-only adjacent-pair check.
# Carry-forward is deliberately NOT implemented: (c) rejects it, because a
# revision must only contain what its admit actually carried.


def component_set(paths: Iterable[str]) -> Set[str]:
    """The component set of a revision or tree: the first path segment of every
    relative path.  A top-level file is its own component -- `rrev_15cf1d9f`
    admitted `prd.md` and friends flat, so defining components as "top-level
    directories" would read that revision as empty.  `revision.json` is
    revision metadata, not content."""
    components: Set[str] = set()
    for path in paths:
        rel = str(path).strip().lstrip("./")
        if not rel or rel == REVISION_RECORD_NAME:
            continue
        components.add(rel.split("/", 1)[0])
    return components


def _scan_component_set(source_path: Path) -> Set[str]:
    """Component set of the incoming tree, read-only.

    Deliberately does not reuse `_copy_tree_files`: contract (a) requires that a
    refusal create no staging directory at all, and that helper writes as it
    walks."""
    if source_path.is_file():
        return component_set([source_path.name])
    return component_set(
        entry.relative_to(source_path).as_posix() for entry in _walk_files(source_path)
    )


def _revision_component_set(
    root: Path, kind: str, ref_id: str, revision_id: str, *, fallback_to_disk: bool = False
) -> Set[str]:
    revision_dir = Path(root) / "shared" / kind / ref_id / "revisions" / revision_id
    record = _read_json(revision_dir / REVISION_RECORD_NAME)
    if record is None:
        # W7-relocated / adopted revisions (`artifact_cutover._adopt_reference`)
        # carry no revision record; the bytes on disk are their only truth.  Use
        # the directory's top-level entries so D-87 still guards them instead of
        # hard-blocking every later admit on the reference.  Only a revision
        # that is absent on disk as well is an error.
        # The read-only `check-components` surface keeps reporting such a
        # revision as unreadable; only the admit guard opts into the scan.
        if fallback_to_disk and revision_dir.is_dir():
            return _scan_component_set(revision_dir)
        raise ProducerError("revision-record-missing", f"{ref_id}/{revision_id}")
    return component_set(
        str(row.get("path", "")) for row in (record.get("files") or []) if isinstance(row, Mapping)
    )


def _latest_component_set(
    root: Path, kind: str, reference: Optional[Mapping[str, Any]]
) -> Optional[Set[str]]:
    """The previous latest revision's component set, or `None` when there is no
    predecessor to regress against (A17-6: the first revision is exempt)."""
    if not reference:
        return None
    latest = reference.get("latest_revision_id")
    if not latest:
        return None
    return _revision_component_set(
        root, kind, reference["shared_reference_id"], str(latest), fallback_to_disk=True
    )


def check_component_sets(
    root: Path,
    kind: str,
    reference_id: str,
    *,
    from_revision: Optional[str] = None,
    to_revision: Optional[str] = None,
) -> Dict[str, Any]:
    """D-87 (d): read-only check of `components(new) >= components(old) - dropped(new)`
    over adjacent revision pairs.  Writes nothing.

    `from_revision`/`to_revision` bound the inspected window.  The bound is not
    cosmetic: run unbounded over `ref_4d540b57...` this reports nine violations
    reaching back to seq 8, because a partial admit was a chronic pattern long
    before the 2026-09-03 incident.  A caller asking about one incident needs to
    ask about its window.
    """
    root = Path(root).resolve()
    reference = _read_json(_reference_path(root, kind, reference_id))
    if reference is None:
        raise ProducerError("reference-unknown", reference_id)
    revisions = [str(value) for value in (reference.get("revisions") or [])]
    if from_revision is not None:
        if from_revision not in revisions:
            raise ProducerError("revision-unknown", from_revision)
        revisions = revisions[revisions.index(from_revision):]
    if to_revision is not None:
        if to_revision not in revisions:
            raise ProducerError("revision-unknown", to_revision)
        revisions = revisions[: revisions.index(to_revision) + 1]
    pairs: List[Dict[str, Any]] = []
    unreadable: List[str] = []
    sets: Dict[str, Optional[Set[str]]] = {}
    for revision_id in revisions:
        try:
            sets[revision_id] = _revision_component_set(root, kind, reference_id, revision_id)
        except ProducerError:
            sets[revision_id] = None
            unreadable.append(revision_id)
    violations = 0
    for older, newer in zip(revisions, revisions[1:]):
        old_set, new_set = sets[older], sets[newer]
        if old_set is None or new_set is None:
            pairs.append({"old": older, "new": newer, "dropped": [], "missing": [],
                          "verdict": "unknown"})
            continue
        record = _read_json(
            Path(root) / "shared" / kind / reference_id / "revisions" / newer / REVISION_RECORD_NAME
        ) or {}
        dropped = sorted(
            str(row.get("name", ""))
            for row in (record.get("dropped_components") or [])
            if isinstance(row, Mapping)
        )
        missing = sorted(old_set - new_set - set(dropped))
        if missing:
            violations += 1
        pairs.append({"old": older, "new": newer, "dropped": dropped, "missing": missing,
                      "verdict": "regressed" if missing else "ok"})
    return {"status": "checked", "kind": kind, "reference_id": reference_id,
            "pairs": pairs, "violations": violations, "unreadable": sorted(unreadable)}


SPEC_BASE_RECEIPT = "_internal/shared-base.json"


def _check_shared_base(expected: Optional[str], actual: Optional[str]) -> None:
    if expected != actual:
        raise ProducerError("shared-base-mismatch",
                            f"base={expected or 'none'} latest={actual or 'none'}; "
                            "merge against latest in a new cycle before admission")


def _sealed_source_files(directory: Path, record: Mapping[str, Any], source_rel: str,
                         source_path: Path) -> List[Tuple[str, str, int]]:
    """Bind both the receipt and payload to the sealed manifest, not mutable disk."""
    document = _read_json(directory / "manifest.json")
    if document is None or artifact_manifest.manifest_digest(document) != record.get("manifest_digest"):
        raise ProducerError("source-manifest-mismatch", source_rel)
    expected = {}
    for row in document.get("artifact_revisions", []):
        path = row.get("locator", {}).get("path", "")
        if path == source_rel or path.startswith(source_rel + "/"):
            rel = path[len(source_rel) + 1:] if path != source_rel else source_path.name
            expected[rel] = (row["content_digest"], row["byte_size"])
    paths = sorted(source_path.rglob("*")) if source_path.is_dir() else [source_path]
    actual = {}
    for path in paths:
        if path.is_symlink():
            raise ProducerError("source-invalid", str(path))
        if path.is_file():
            rel = path.relative_to(source_path).as_posix() if source_path.is_dir() else path.name
            data = path.read_bytes()
            actual[rel] = (_digest(data), len(data))
    if actual != expected:
        raise ProducerError("source-manifest-mismatch", source_rel)
    return [(rel, digest, size) for rel, (digest, size) in sorted(actual.items())]


def _spec_admission_base(root: Path, source_path: Path, reference_id: str,
                         base_revision: Optional[str]) -> Optional[str]:
    receipt_path = source_path / SPEC_BASE_RECEIPT
    receipt = _read_json(receipt_path) if source_path.is_dir() else None
    if receipt is None and os.path.lexists(receipt_path):
        raise ProducerError("shared-base-invalid", SPEC_BASE_RECEIPT)
    if receipt is None:
        if base_revision is None:
            raise ProducerError("shared-base-required", "spec admission needs a seeded receipt or --base-revision")
        base = None if base_revision == "none" else base_revision
    else:
        if receipt.get("schema_version") != 1 or not all(k in receipt for k in ("reference_id", "revision_id", "content_digest")):
            raise ProducerError("shared-base-invalid", SPEC_BASE_RECEIPT)
        base = receipt["revision_id"]
        if base is not None and not artifact_identity.is_well_formed(base, "shared_reference_revision"):
            raise ProducerError("shared-base-invalid", str(base))
        if base is not None:
            if receipt["reference_id"] != reference_id:
                raise ProducerError("shared-base-reference-mismatch", reference_id)
            revision = _read_json(root / "shared/spec" / reference_id / "revisions" / str(base) / REVISION_RECORD_NAME)
            if revision is None or revision.get("content_digest") != receipt["content_digest"]:
                raise ProducerError("shared-base-invalid", str(base))
        elif receipt["reference_id"] is not None or receipt["content_digest"] is not None:
            raise ProducerError("shared-base-invalid", SPEC_BASE_RECEIPT)
        if base_revision is not None:
            _check_shared_base(None if base_revision == "none" else base_revision, base)
    if base is not None and not artifact_identity.is_well_formed(base, "shared_reference_revision"):
        raise ProducerError("shared-base-invalid", str(base))
    return base


def _spec_bytes(directory: Path, *, revision: bool = False) -> Dict[str, bytes]:
    """Read a complete regular-file tree, rejecting links and special files."""
    if directory.is_symlink() or not directory.is_dir():
        raise ProducerError("shared-base-invalid", str(directory))
    result = {}
    for path in _walk_files(directory):
        rel = path.relative_to(directory).as_posix()
        if path.is_symlink() or not path.is_file():
            raise ProducerError("shared-base-invalid", rel)
        if revision and rel == REVISION_RECORD_NAME:
            continue
        result[rel] = path.read_bytes()
    return result


def _spec_inventory(tree: Mapping[str, bytes]) -> List[Dict[str, Any]]:
    return [{"path": p, "sha256": _digest(data), "byte_size": len(data)}
            for p, data in sorted(tree.items())]


def _spec_inventory_digest(rows: Sequence[Mapping[str, Any]]) -> str:
    return _digest(_canonical([[r["path"], r["sha256"], r["byte_size"]] for r in rows]))


def _verified_shared_spec(root: Path, ref_id: str, revision_id: str):
    """Validate each immutable input against its own inventory, including bytes."""
    if not artifact_identity.is_well_formed(revision_id, "shared_reference_revision"):
        raise ProducerError("shared-base-invalid", str(revision_id))
    directory = root / "shared/spec" / ref_id / "revisions" / revision_id
    for parent in (directory, directory.parent, directory.parent.parent):
        if parent.is_symlink():
            raise ProducerError("shared-base-invalid", str(parent))
    record = _read_json(directory / REVISION_RECORD_NAME)
    if (not isinstance(record, dict) or record.get("shared_reference_id") != ref_id
            or record.get("shared_reference_revision_id") != revision_id):
        raise ProducerError("shared-base-invalid", revision_id)
    tree = _spec_bytes(directory, revision=True)
    rows = record.get("files")
    try:
        valid = (isinstance(rows, list)
                 and sorted(rows, key=lambda r: r["path"]) == _spec_inventory(tree)
                 and record.get("content_digest") == _spec_inventory_digest(rows))
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ProducerError("shared-revision-integrity", revision_id)
    return tree, record


def _merge_spec_publication(root: Path, reference: Mapping[str, Any], base_id: str,
                            latest_id: str, source_tree: Mapping[str, bytes], drop_decisions: Sequence[Mapping[str, str]] = ()):
    import spec_merge
    ids = reference.get("revisions", [])
    if (not base_id or not latest_id or base_id not in ids or latest_id not in ids
            or len(ids) != len(set(ids)) or ids.index(base_id) >= ids.index(latest_id)):
        raise ProducerError("shared-base-mismatch", f"unproven ancestry: {base_id} -> {latest_id}")
    ref_id = reference["shared_reference_id"]
    base, base_record = _verified_shared_spec(root, ref_id, base_id)
    latest, latest_record = _verified_shared_spec(root, ref_id, latest_id)
    dropped = {row["name"] for row in drop_decisions}
    missing = component_set(base) - component_set(source_tree) - dropped
    if missing:
        raise ProducerError("component-set-regressed", ",".join(sorted(missing)))
    if dropped - component_set(latest):
        raise ProducerError("drop-component-unknown", ",".join(sorted(dropped - component_set(latest))))
    # A component deletion also conflicts with additions to that component.
    # File-wise merge alone could leave only the newly added files alive.
    for component in component_set(base):
        def subtree(tree):
            return {p: b for p, b in tree.items() if p.split("/", 1)[0] == component}
        b, o, l = subtree(base), subtree(source_tree), subtree(latest)
        if (not o and l and l != b) or (not l and o and o != b):
            raise ProducerError("shared-spec-conflict", f"{component}: component-delete-modify")
    try:
        merged, evidence = spec_merge.merge_trees(base, dict(source_tree), latest)
    except spec_merge.MergeConflict as exc:
        raise ProducerError("shared-spec-conflict", str(exc)) from exc
    source_files = _spec_inventory(source_tree)
    proof = {"schema_version": 1, "base_revision_id": base_id,
             "base_content_digest": base_record["content_digest"],
             "latest_revision_id": latest_id,
             "latest_content_digest": latest_record["content_digest"],
             "source_files": source_files, "source_content_digest": _spec_inventory_digest(source_files),
             "dropped_components": list(drop_decisions),
             "evidence": evidence, "output_content_digest": _spec_inventory_digest(_spec_inventory(merged))}
    return merged, proof


def _verify_spec_publication(root: Path, reference: Mapping[str, Any], revision: Mapping[str, Any]):
    """Recompute a derived publication for recovery and exact retry; never rebase it."""
    proof = revision.get("spec_merge")
    if not isinstance(proof, dict):
        raise ProducerError("shared-merge-proof-invalid", "missing merge proof")
    source = revision.get("source", {})
    record = read_cycle_record(root, source.get("cycle_id", ""))
    if record is None or record.get("state") != "sealed" or record.get("manifest_digest") != source.get("manifest_digest"):
        raise ProducerError("shared-merge-proof-invalid", "source identity")
    directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
    rel = source.get("path", "")
    if not rel.startswith("artifacts/") or ".." in rel.split("/"):
        raise ProducerError("shared-merge-proof-invalid", "source path")
    source_rows = _sealed_source_files(directory, record, rel, directory / rel)
    source_tree = _spec_bytes(directory / rel)
    if source_rows != [(r["path"], r["sha256"], r["byte_size"]) for r in _spec_inventory(source_tree)]:
        raise ProducerError("source-manifest-mismatch", rel)
    recorded_base = _spec_admission_base(root, directory / rel, reference["shared_reference_id"],
                                         proof.get("base_revision_id"))
    merged, expected = _merge_spec_publication(root, reference, recorded_base,
                                              proof.get("latest_revision_id"), source_tree, revision.get("dropped_components", []))
    output, _ = _verified_shared_spec(root, reference["shared_reference_id"],
                                      revision["shared_reference_revision_id"])
    if proof != expected or output != merged:
        raise ProducerError("shared-merge-proof-invalid", revision["shared_reference_revision_id"])


def _verify_exact_spec_publication(root: Path, reference: Mapping[str, Any], revision: Mapping[str, Any]):
    """Absence of a merge proof must mean an exact source copy, not lost proof."""
    ids = reference.get("revisions", [])
    revision_id = revision["shared_reference_revision_id"]
    if revision_id in ids:
        index = ids.index(revision_id)
        base_id = ids[index - 1] if index else None
    else:
        base_id = reference.get("latest_revision_id")
    if "spec_base_revision_id" in revision and revision["spec_base_revision_id"] != base_id:
        raise ProducerError("shared-journal-mismatch", "publication ancestry")
    if base_id and not _legacy_adopted_spec_base(root, reference, base_id):
        _verified_shared_spec(root, reference["shared_reference_id"], base_id)
    source = revision.get("source", {})
    record = read_cycle_record(root, source.get("cycle_id", ""))
    if record is None or record.get("manifest_digest") != source.get("manifest_digest"):
        raise ProducerError("shared-journal-mismatch", "source identity")
    rel = source.get("path", "")
    if not rel.startswith("artifacts/") or ".." in rel.split("/"):
        raise ProducerError("shared-journal-mismatch", "source path")
    directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
    if os.path.lexists(directory / rel / SPEC_BASE_RECEIPT):
        source_base = _spec_admission_base(root, directory / rel, reference["shared_reference_id"], None)
        _check_shared_base(source_base, base_id)
    rows = _sealed_source_files(directory, record, rel, directory / rel)
    output, _ = _verified_shared_spec(root, reference["shared_reference_id"],
                                     revision["shared_reference_revision_id"])
    if rows != [(r["path"], r["sha256"], r["byte_size"]) for r in _spec_inventory(output)]:
        raise ProducerError("shared-merge-proof-invalid", "nonexact publication lacks merge proof")


def _legacy_adopted_spec_base(root: Path, reference: Mapping[str, Any], base_id: str) -> bool:
    # Missing/malformed canonical metadata is corruption, not legacy evidence.
    # Old adoptions without an exact captured roster remain unproven.
    return (reference.get("adopted_from") == "w7-e2-e3-relocation"
            and base_id in reference.get("adopted_revision_ids", [])
            and base_id in reference.get("revisions", [])
            and not os.path.lexists(root / "shared/spec" / reference["shared_reference_id"]
                                   / "revisions" / base_id / REVISION_RECORD_NAME))


def _validate_shared_journal(root: Path, entry: Path, journal) -> None:
    """A malformed recovery record grants no deletion or publication authority."""
    if not isinstance(journal, dict):
        raise ProducerError("shared-journal-mismatch", "unreadable journal")
    kind, ref, rev = (journal.get(k) for k in ("kind", "reference_id", "revision_id"))
    if (not isinstance(kind, str) or kind not in SHARED_KINDS or not artifact_identity.is_well_formed(ref, "shared_reference")
            or not artifact_identity.is_well_formed(rev, "shared_reference_revision") or entry.stem != rev
            or not isinstance(journal.get("state"), str) or journal.get("state") not in {"staging", "published"}
            or "expected_previous_revision_id" not in journal):
        raise ProducerError("shared-journal-mismatch", entry.name)
    prefix = Path("shared") / kind / ref / "revisions"
    staging, target = journal.get("staging"), journal.get("target")
    if (not isinstance(staging, str) or Path(staging).parent != prefix
            or not re.fullmatch(r"\.admitting-[0-9a-f]{16}", Path(staging).name)
            or target != (prefix / rev).as_posix()):
        raise ProducerError("shared-journal-mismatch", "recovery paths")
    for rel in (Path(staging), Path(target)):
        for part in (rel, *rel.parents):
            if (root / part).is_symlink():
                raise ProducerError("shared-journal-mismatch", "symlink recovery path")
    source = journal.get("source_path", "")
    record = read_cycle_record(root, journal.get("cycle_id", ""))
    if (record is None or record.get("manifest_digest") != journal.get("source_manifest_digest")
            or not isinstance(source, str) or not source.startswith("artifacts/")
            or ".." in source.split("/")):
        raise ProducerError("shared-journal-mismatch", "source identity")
    if kind == "spec":
        directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
        _sealed_source_files(directory, record, source, directory / source)


def _commit_shared(root: Path, journal: Mapping[str, Any]) -> None:
    kind = journal["kind"]
    ref_id = journal["reference_id"]
    reference = _read_json(_reference_path(root, kind, ref_id))
    if reference is None:
        reference = {
            "schema_version": 1, "contract": CONTRACT, "shared_reference_id": ref_id,
            "kind": SHARED_KINDS[kind], "key": journal.get("key"), "title": journal.get("title"),
            "created_on": journal.get("created_on"), "latest_revision_id": None, "revisions": [],
        }
    revision = _read_json(root / "shared" / kind / ref_id / "revisions" / journal["revision_id"] / REVISION_RECORD_NAME)
    if (revision is None or revision.get("shared_reference_id") != ref_id
            or revision.get("shared_reference_revision_id") != journal["revision_id"]
            or revision.get("source", {}).get("cycle_id") != journal.get("cycle_id")
            or revision.get("source", {}).get("path") != journal.get("source_path")
            or revision.get("source", {}).get("manifest_digest") != journal.get("source_manifest_digest")):
        raise ProducerError("shared-journal-mismatch", journal["revision_id"])
    if journal["revision_id"] not in reference["revisions"]:
        if "expected_previous_revision_id" not in journal:
            raise ProducerError("shared-base-required", "recovery journal lacks base revision")
        _check_shared_base(journal["expected_previous_revision_id"], reference.get("latest_revision_id"))
    if "spec_merge" in journal or "spec_merge" in revision:
        if journal.get("spec_merge") != revision.get("spec_merge"):
            raise ProducerError("shared-journal-mismatch", "merge proof")
        _verify_spec_publication(root, reference, revision)
        if journal.get("expected_previous_revision_id") != revision["spec_merge"]["latest_revision_id"]:
            raise ProducerError("shared-journal-mismatch", "merge parent")
    elif kind == "spec":
        _verify_exact_spec_publication(root, reference, revision)
        if "spec_base_revision_id" in revision and revision["spec_base_revision_id"] != journal.get("expected_previous_revision_id"):
            raise ProducerError("shared-journal-mismatch", "source base")
    if journal["revision_id"] not in reference["revisions"]:
        if "expected_previous_revision_id" not in journal:
            raise ProducerError("shared-base-required", "recovery journal lacks base revision")
        _check_shared_base(journal["expected_previous_revision_id"], reference.get("latest_revision_id"))
        reference["revisions"] = list(reference["revisions"]) + [journal["revision_id"]]
        reference["latest_revision_id"] = journal["revision_id"]
        reference["updated_on"] = journal.get("created_on")
        path = _reference_path(root, kind, ref_id)
        _ensure_dir(path.parent)
        _write_atomic(path, _json_bytes(reference))
    # An already committed journal is cleanup only: never rewind a newer latest.
    try:
        shared_journal_path(root, journal["revision_id"]).unlink()
    except FileNotFoundError:
        pass


def admit_shared(
    root: Path,
    *,
    cycle_id: str,
    kind: str,
    source: str,
    reference_id: Optional[str] = None,
    key: Optional[str] = None,
    title: Optional[str] = None,
    promote_research: bool = False,
    promotion_evidence: Optional[str] = None,
    allocator: Optional[artifact_identity.IdAllocator] = None,
    drop_components: Sequence[str] = (),
    drop_reason: Optional[str] = None,
    allow_new_reference: bool = False,
    base_revision: Optional[str] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    root = Path(root).resolve()
    if kind not in SHARED_KINDS:
        raise ProducerError("shared-kind-not-admissible", kind)
    if kind == "research":
        if not promote_research:
            raise ProducerError("research-promotion-required",
                                "research is admitted to shared/ only with an explicit promotion")
        if not promotion_evidence:
            raise ProducerError("research-promotion-evidence-required")
    if reference_id and not artifact_identity.is_well_formed(reference_id, "shared_reference"):
        raise ProducerError("reference-id-malformed", reference_id)
    if key and not _KEY_RE.match(key):
        raise ProducerError("reference-key-invalid", key)
    alloc = allocator or artifact_identity.IdAllocator()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        pre = read_cycle_record(root, cycle_id)
        sweep = _recover_locked(root, now=now, target_campaign_id=pre["campaign_id"] if pre else None)
        sweep_unresolved = sweep.get("unresolved", [])
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        if record.get("state") != "sealed":
            raise ProducerError("cycle-not-sealed", record.get("state", "?"))
        directory = cycle_dir(root, record["campaign_id"], cycle_id, record)
        source_rel = source if source.startswith("artifacts/") else "artifacts/" + source
        if ".." in source_rel.split("/"):
            raise ProducerError("source-unsafe", source)
        source_rel = _placed_locator(source_rel, _output_placements(root, record))
        source_path = directory / source_rel
        if os.path.islink(str(source_path)) or not source_path.exists():
            raise ProducerError("source-missing", source_rel)
        evidence_rel: Optional[str] = None
        evidence_digest: Optional[str] = None
        if kind == "research":
            assert promotion_evidence is not None
            evidence_rel = promotion_evidence if promotion_evidence.startswith("artifacts/") else "artifacts/" + promotion_evidence
            evidence_rel = _placed_locator(evidence_rel, _output_placements(root, record))
            evidence_path = directory / evidence_rel
            if os.path.islink(str(evidence_path)) or not evidence_path.is_file():
                raise ProducerError("research-promotion-evidence-missing", evidence_rel)
            evidence_digest = _digest(evidence_path.read_bytes())
        reference: Optional[Dict[str, Any]] = None
        if reference_id:
            reference = _read_json(_reference_path(root, kind, reference_id))
            if reference is None:
                raise ProducerError("reference-unknown", reference_id)
        elif key:
            reference = find_reference_by_key(root, kind, key)
        if reference is None and kind in CANONICAL_SINGLE_REFERENCE_KINDS and not allow_new_reference:
            # Defect K (cairn 2026-09-03): `--key cairn-spec` missed the
            # canonical `spec` reference and silently minted a second one, after
            # which "the latest spec" flipped on every admit. A second reference
            # of a canonical-singular kind is an explicit act, never a miss.
            # The documented flow (`admit-shared --kind spec`, no key) keeps
            # working: with exactly one reference it is the canonical one.
            existing = list_references(root, kind)
            listing = ", ".join(f"{r['shared_reference_id']}(key={r.get('key')})" for r in existing)
            if key is None and len(existing) == 1:
                reference = existing[0]
            elif key is None and len(existing) > 1:
                raise ProducerError("shared-reference-ambiguous",
                                    f"{kind}: {listing}; pass --reference <id> or --key <key>")
            elif key is not None and existing:
                raise ProducerError(
                    "shared-reference-exists",
                    f"{kind}: {listing}; the key {key!r} matches none of them -- pass --reference <id> "
                    "(or --key of an existing one), or --new-reference to add another",
                )
        if reference is None:
            reference_id = alloc.allocate("shared_reference")
            created = True
        else:
            reference_id = reference["shared_reference_id"]
            created = False
        # An unresolved publication intent for this exact source cannot be
        # replaced by a second publication. Preserve it for checked recovery.
        for issue in sweep_unresolved:
            pending_id = issue.get("revision_id")
            if not pending_id:
                continue
            pending = _read_json(shared_journal_path(root, pending_id))
            if (pending is None or not all(pending.get(k) for k in
                    ("reference_id", "cycle_id", "source_path", "source_manifest_digest")) or (pending.get("reference_id") == reference_id
                    and pending.get("cycle_id") == cycle_id and pending.get("source_path") == source_rel
                    and pending.get("source_manifest_digest") == record.get("manifest_digest"))):
                raise ProducerError("shared-publication-unresolved", str(pending_id))
        dropped = sorted({str(name) for name in drop_components if str(name)})
        drop_decisions = [{"name": name, "reason": drop_reason or "unspecified"} for name in dropped]
        source_rows = _sealed_source_files(directory, record, source_rel, source_path) if kind == "spec" else None
        merged_tree = None
        merge_proof = None
        if kind == "spec":
            # Exact publication retry is idempotent, even after another cycle won.
            for prior_id in (reference or {}).get("revisions", []):
                prior_dir = root / "shared" / kind / reference_id / "revisions" / prior_id
                prior = _read_json(prior_dir / REVISION_RECORD_NAME) or {}
                provenance = prior.get("source", {})
                if (provenance.get("cycle_id") == cycle_id and provenance.get("path") == source_rel
                        and provenance.get("manifest_digest") == record.get("manifest_digest")):
                    digest = prior.get("content_digest")
                    comparison = prior.get("spec_merge", {}).get("source_files", prior.get("files", []))
                    if source_rows != sorted((r["path"], r["sha256"], r["byte_size"]) for r in comparison):
                        raise ProducerError("source-manifest-mismatch", source_rel)
                    if "spec_merge" in prior:
                        _verify_spec_publication(root, reference, prior)
                    else:
                        _verify_exact_spec_publication(root, reference, prior)
                    return {"status": "reused", "kind": SHARED_KINDS[kind],
                            "shared_reference_id": reference_id, "shared_reference_revision_id": prior_id,
                            "reference_created": False, "revision_dir": str(prior_dir),
                            "content_digest": digest, "file_count": prior["file_count"], "promotion": prior["promotion"],
                            **({"spec_merge": prior["spec_merge"]} if "spec_merge" in prior else {})}
            # Initial unseeded publications have no predecessor to overwrite.
            expected = (_spec_admission_base(root, source_path, reference_id, base_revision)
                        if reference is not None or base_revision is not None or (source_path / SPEC_BASE_RECEIPT).exists()
                        else None)
            latest_id = (reference or {}).get("latest_revision_id")
            # Canonical revisions with an inventory are verified even on the
            # exact-base path; legacy adopted revisions retain their old path.
            if expected:
                if not _legacy_adopted_spec_base(root, reference or {}, expected):
                    _verified_shared_spec(root, reference_id, expected)
            if expected != latest_id:
                if not expected or not latest_id:
                    _check_shared_base(expected, latest_id)
                if expected not in reference.get("revisions", []) or latest_id not in reference.get("revisions", []):
                    _check_shared_base(expected, latest_id)
                source_tree = _spec_bytes(source_path)
                if source_rows != [(r["path"], r["sha256"], r["byte_size"])
                                   for r in _spec_inventory(source_tree)]:
                    raise ProducerError("source-manifest-mismatch", source_rel)
                base_tree, _ = _verified_shared_spec(root, reference_id, expected)
                # Preserve the original omission guard relative to the actual
                # base. Only additions from latest have carry-forward authority.
                missing_base = component_set(base_tree) - component_set(source_tree) - set(drop_components)
                if missing_base:
                    raise ProducerError("component-set-regressed", ",".join(sorted(missing_base)))
                merged_tree, merge_proof = _merge_spec_publication(root, reference, expected, latest_id, source_tree, drop_decisions)
        # D-87 (a): refuse before anything exists.  This sits above the id
        # allocation, the journal write and the staging directory on purpose --
        # the contract requires a refused admit to leave no revision, no journal
        # and no staging behind.
        previous = _latest_component_set(root, kind, reference)
        if previous is not None:
            unknown = sorted(set(dropped) - previous)
            if unknown:
                raise ProducerError("drop-component-unknown", ",".join(unknown))
            incoming_components = (component_set(merged_tree) if merged_tree is not None
                                   else _scan_component_set(source_path))
            missing = sorted(previous - incoming_components - set(dropped))
            if missing:
                raise ProducerError(
                    "component-set-regressed",
                    "source omits components carried by the previous latest revision: "
                    + ",".join(missing)
                    + "; admit the whole tree or drop them explicitly with --drop-component",
                )
        elif dropped:
            raise ProducerError("drop-component-unknown", ",".join(dropped))
        revision_id = alloc.allocate("shared_reference_revision")
        revisions_dir = Path(root) / "shared" / kind / reference_id / "revisions"
        _ensure_dir(revisions_dir)
        target = revisions_dir / revision_id
        if target.exists():
            raise ProducerError("revision-exists", str(target))
        staging = revisions_dir / f".admitting-{os.urandom(8).hex()}"
        journal = {
            "schema_version": 1, "state": "staging", "kind": kind, "reference_id": reference_id,
            "revision_id": revision_id, "key": key or (reference or {}).get("key"),
            "title": title or (reference or {}).get("title") or source_rel,
            "created_on": _rfc3339(now), "staging": os.path.relpath(str(staging), str(root)),
            "target": os.path.relpath(str(target), str(root)), "cycle_id": cycle_id,
            "expected_previous_revision_id": (reference or {}).get("latest_revision_id"),
            "source_path": source_rel, "source_manifest_digest": record.get("manifest_digest"),
        }
        if merge_proof is not None:
            journal["spec_merge"] = merge_proof
        _ensure_dir(shared_journal_path(root, revision_id).parent)
        _write_exclusive(shared_journal_path(root, revision_id), _json_bytes(journal), 0o600)
        os.makedirs(str(staging))
        try:
            if merged_tree is None:
                rows, violations = _copy_tree_files(source_path, staging)
            else:
                rows, violations = [], []
                for rel, data in sorted(merged_tree.items()):
                    dst = staging / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    _write_exclusive(dst, data)
                    rows.append((rel, _digest(data), len(data)))
                for current, _, _ in os.walk(staging):
                    _fsync_dir(Path(current))
            if violations:
                raise ProducerError("source-invalid", ";".join(violations))
            if not rows:
                raise ProducerError("source-empty", source_rel)
            if source_rows is not None and merged_tree is None and sorted(rows) != source_rows:
                raise ProducerError("source-manifest-mismatch", source_rel)
            content_digest = _digest(_canonical([[rel, digest, size] for rel, digest, size in rows]))
            sequence = len((reference or {}).get("revisions", [])) + 1
            revision = {
                "schema_version": 1, "contract": CONTRACT,
                "shared_reference_revision_id": revision_id, "shared_reference_id": reference_id,
                "kind": SHARED_KINDS[kind], "sequence": sequence, "content_digest": content_digest,
                "file_count": len(rows), "byte_size": sum(size for _, _, size in rows),
                "created_on": journal["created_on"],
                "source": {
                    "campaign_id": record["campaign_id"], "cycle_id": cycle_id,
                    "manifest_digest": record.get("manifest_digest"), "path": source_rel,
                    "capability": record.get("capability"), "route_id": record.get("route_id"),
                },
                "promotion": (
                    {"kind": "explicit", "evidence": evidence_rel, "evidence_digest": evidence_digest}
                    if kind == "research" else {"kind": "canonical-shared-kind"}
                ),
                "files": [{"path": rel, "sha256": digest, "byte_size": size} for rel, digest, size in rows],
            }
            if merge_proof is not None:
                revision["spec_merge"] = merge_proof
            elif kind == "spec":
                revision["spec_base_revision_id"] = expected
            # D-87 (b): an explicit removal is named and reasoned in the record.
            # The key is omitted entirely when nothing was dropped, so an
            # ordinary admit's revision record is byte-identical to before
            # (A17-4). `content_digest` covers `files[]` only, so this key never
            # moves the digest either way.
            if dropped:
                revision["dropped_components"] = drop_decisions
            _write_exclusive(staging / REVISION_RECORD_NAME, _json_bytes(revision))
            _fsync_dir(staging)
        except BaseException:
            shutil.rmtree(str(staging), ignore_errors=True)
            try:
                shared_journal_path(root, revision_id).unlink()
            except FileNotFoundError:
                pass
            raise
        if target.exists():
            shutil.rmtree(str(staging), ignore_errors=True)
            raise ProducerError("revision-exists", str(target))
        # COMMIT POINT: no-replace rename of the staged immutable revision.
        os.rename(str(staging), str(target))
        _fsync_dir(revisions_dir)
        journal["state"] = "published"
        _write_atomic(shared_journal_path(root, revision_id), _json_bytes(journal), 0o600)
        _commit_shared(root, journal)
        result = {
            "status": "admitted", "kind": SHARED_KINDS[kind], "shared_reference_id": reference_id,
            "shared_reference_revision_id": revision_id, "reference_created": created,
            "revision_dir": str(target), "content_digest": content_digest, "file_count": len(rows),
            "promotion": revision["promotion"],
        }
        if merge_proof is not None:
            result["spec_merge"] = merge_proof
        if sweep_unresolved:
            result["recovery_unresolved"] = sweep_unresolved
        return result
    finally:
        artifact_admission._release_lock(root, lock_fd)


# ---------------------------------------------------------------------------
# campaign relationships and supersession side records (D-81)
# ---------------------------------------------------------------------------


def _find_campaign_by_key_any_state(root: Path, key: str) -> Optional[Dict[str, Any]]:
    """Like `find_campaign_by_key`, but not restricted to `state == "active"` --
    a `related[]` row may point at a campaign that is already superseded."""
    rows = _campaigns_by_key(root, key)
    return rows[0] if rows else None


def validate_related(root: Path, related: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """D-81 pure validation of `campaign.json` `related[]` rows -- no write.
    Returns a list of `{"index", "code", "detail"}` violation rows; empty
    means every row resolves."""
    root = Path(root).resolve()
    violations: List[Dict[str, Any]] = []
    for i, row in enumerate(related):
        if not isinstance(row, Mapping):
            violations.append({"index": i, "code": "campaign-related-invalid", "detail": "not-an-object"})
            continue
        kind = row.get("kind")
        if kind not in RELATED_KINDS:
            violations.append({"index": i, "code": "campaign-related-invalid", "detail": f"kind:{kind}"})
            continue
        campaign_id = row.get("campaign_id")
        key = row.get("key")
        if not campaign_id and not key:
            violations.append({"index": i, "code": "campaign-related-invalid",
                               "detail": "missing-campaign_id-and-key"})
            continue
        found = read_campaign(root, campaign_id) if campaign_id else None
        if found is None and key:
            found = _find_campaign_by_key_any_state(root, key)
        if found is None:
            violations.append({"index": i, "code": "campaign-related-unresolved",
                               "detail": str(campaign_id or key)})
    return violations


def set_campaign_related(root: Path, campaign_id: str, *, related: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """D-81: producer-internal API. `campaigns/<camp>/campaign.json` is the
    `campaign-record-machine-managed` write surface -- no general writer, hook,
    or agent may call this."""
    root = Path(root).resolve()
    violations = validate_related(root, related)
    if violations:
        first = violations[0]
        raise ProducerError(first["code"], first["detail"])
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT)
    try:
        campaign = read_campaign(root, campaign_id)
        if campaign is None:
            raise ProducerError("campaign-unknown", campaign_id)
        campaign = dict(campaign)
        campaign["related"] = [dict(row) for row in related]
        _write_campaign(root, campaign, exclusive=False)
        return {"status": "updated", "campaign_id": campaign_id, "related": campaign["related"]}
    finally:
        artifact_admission._release_lock(root, lock_fd)


def mark_cycle_superseded(
    root: Path, cycle_id: str, *, superseded_by: Sequence[str], superseded_event_id: str,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """D-81: mutable side-record supersession marker on a sealed cycle record.
    The sealed manifest's `cycle.state` stays `completed` forever -- this
    writes only the cycle record, never the manifest."""
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        if record.get("state") != "sealed":
            raise ProducerError("cycle-not-sealed", record.get("state", "?"))
        artifact_locator.prepare_index_update(root, [record["campaign_id"]])
        updated = dict(record)
        updated["state"] = "superseded"
        updated["superseded_by"] = list(superseded_by)
        updated["superseded_event_id"] = superseded_event_id
        _write_cycle_record(root, updated, exclusive=False)
        artifact_locator.update_indexes(root, [record["campaign_id"]])
        return {"status": "updated", "cycle_id": cycle_id, "state": "superseded",
                "superseded_by": updated["superseded_by"], "superseded_event_id": superseded_event_id}
    finally:
        artifact_admission._release_lock(root, lock_fd)


def mark_campaign_superseded(root: Path, campaign_id: str, *, now: Optional[float] = None) -> Dict[str, Any]:
    """D-81: a campaign may be marked `superseded` only once every cycle it
    owns already carries the `superseded` side-record state."""
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        campaign = read_campaign(root, campaign_id)
        if campaign is None:
            raise ProducerError("campaign-unknown", campaign_id)
        for cycle_id in campaign.get("cycles", []):
            record = read_cycle_record(root, cycle_id)
            if record is None or record.get("state") != "superseded":
                raise ProducerError("campaign-has-live-cycles", campaign_id)
        artifact_locator.prepare_index_update(root, [campaign_id])
        updated = dict(campaign)
        updated["state"] = "superseded"
        _write_campaign(root, updated, exclusive=False)
        artifact_locator.update_indexes(root, [campaign_id])
        return {"status": "updated", "campaign_id": campaign_id, "state": "superseded"}
    finally:
        artifact_admission._release_lock(root, lock_fd)


# ---------------------------------------------------------------------------
# write policy (used by hooks and writers)
# ---------------------------------------------------------------------------


def _relative(root: Path, target: Path) -> Optional[str]:
    root = Path(root).resolve()
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    # Resolve the deepest existing ancestor so a not-yet-created target still
    # normalizes; the leaf is appended unchanged.
    probe = candidate
    tail: List[str] = []
    while not probe.exists() and probe.parent != probe:
        tail.insert(0, probe.name)
        probe = probe.parent
    resolved = probe.resolve()
    for part in tail:
        resolved = resolved / part
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return None


def _quick_refine_write_gate(root: Path, target: Path, route=None) -> None:
    relative = _relative(Path(root).resolve(), Path(target))
    if relative is None:
        return
    parts = relative.split("/")
    if parts[:1] == ["campaigns"]:
        index = 4 if len(parts) > 2 and parts[2] == "cycles" else 3
        if len(parts) <= index or parts[index] != "artifacts":
            return
        parts = parts[index + 1:]
    if len(parts) < 2 or parts[0] not in {"documents", "research"} or "_internal" in parts:
        return
    if route is None:
        path = os.environ.get("AGENT_ROUTE_FILE") or os.environ.get("AGENT_OWNER_ROUTE_FILE")
        if not path:
            return  # The route/material guard independently requires a binding.
        route = _read_json(Path(path))
        if not isinstance(route, dict):
            raise ProducerError("inline-gate-route-unreadable")
    if route.get("capability") != "autopilot-refine" or route.get("effective_intensity") != "quick":
        return
    node = next((n for n in route.get("nodes", []) if n.get("id") == "one-shot"), {})
    if node.get("inline_human_gates") != ["preview-disposition"]:
        raise ProducerError("inline-gate-binding-missing")
    import workflow_state as WS
    try:
        WS.require_inline_gate_release(route, node, jobs=os.environ.get("AGENT_DISPATCH_JOBS") or None)
    except (WS.WorkflowStateError, OSError, ValueError) as exc:
        raise ProducerError("quick-preview-approval-required", str(exc)) from exc


def require_cycle_output(
    root: Path, target: Path, *, cycle_id: Optional[str] = None, route_id: Optional[str] = None,
) -> Optional[Path]:
    """Bind writes and completion evidence to the producer's issued cycle.

    Route lookup recovers omitted environment context from producer records;
    directory names, recency and a caller-supplied output path are not
    authority. When the sealed route file for `route_id` is readable, lookup
    and write admission both go through the lineage-aware D-120 path
    (`route_cycle_for` + `cycle_route_admission`), so a continuation may write
    the cycle its lineage opened. Only a genuinely missing canonical route
    file permits the legacy exact begin-route match, including an explicitly
    selected legacy cycle. Existing but unreadable or malformed proof refuses.
    """
    record = read_cycle_record(root, cycle_id) if cycle_id else None
    if cycle_id and record is None:
        raise ProducerError("cycle-unknown", cycle_id)
    lineage_checked = False
    route = None
    if route_id:
        if not isinstance(route_id, str) or not _ROUTE_ID_RE.fullmatch(route_id):
            raise ProducerError("route-lineage-unverified", f"route={route_id}")
        route_path = route_lineage.canonical_route_path(root, route_id)
        try:
            route_stat = route_path.lstat()
        except FileNotFoundError:
            route_stat = None
        except OSError as exc:
            raise ProducerError("route-lineage-unverified", f"route-unreadable={route_id}") from exc
        if (route_stat is not None and (not stat.S_ISREG(route_stat.st_mode)
                or route_path.is_symlink() or route_path.resolve() != route_path)):
            raise ProducerError("route-lineage-unverified", f"route-kind={route_id}")
        route = _read_json(route_path) if route_stat is not None else None
        if isinstance(route, dict) and route.get("route_id") == route_id:
            if record is None:
                record = route_cycle_for(root, route)
            if record is not None:
                admission = cycle_route_admission(root, record, route)
                if not admission.allow:
                    raise ProducerError(admission.reason, admission.detail)
                if cycle_id:
                    # An explicit selector must not bypass the shared lookup's
                    # refusal of multiple open cycles in the same lineage.
                    selected = route_cycle_for(root, route)
                    if selected is None or selected.get("cycle_id") != record.get("cycle_id"):
                        raise ProducerError("cycle-route-binding-mismatch", f"cycle={cycle_id} route={route_id}")
                lineage_checked = True
        elif route_stat is not None:
            # Existing but malformed proof is never a legacy missing route.
            raise ProducerError("route-lineage-unverified", f"route={route_id}")
        elif record is None:
            # Legacy records can lack a canonical route. Preserve only their
            # original exact begin-route match; continuation still needs proof.
            candidates = [item for item in list_cycle_records(root) if item.get("route_id") == route_id]
            opened = [item for item in candidates if item.get("state") == "open"]
            candidates = opened or candidates
            if len(candidates) > 1:
                raise ProducerError("route-cycle-binding-ambiguous", route_id)
            record = candidates[0] if candidates else None
    if record is None:
        return None
    if route_id and not lineage_checked and record.get("route_id") != route_id:
        raise ProducerError("cycle-route-binding-mismatch", f"cycle={record['cycle_id']} route={route_id}")
    output = cycle_dir(root, record["campaign_id"], record["cycle_id"], record) / "artifacts"
    try:
        Path(target).resolve().relative_to(output.resolve())
    except ValueError as exc:
        raise ProducerError("artifact-outside-bound-cycle", f"cycle={record['cycle_id']} output_dir={output}") from exc
    return output


def check_write(root: Path, target: Path) -> Dict[str, Any]:
    """Classify one prospective write under the artifact root.

    Returns {verdict: allow|deny, reason, layout, cutover, bucket, cycle_id}.
    """
    root = Path(root).resolve()
    rel = _relative(root, Path(target))
    active = is_active(root)
    base = {"cutover": "active" if active else "inactive", "target": str(target)}
    try:
        _quick_refine_write_gate(root, target)
    except ProducerError as exc:
        return {**base, "verdict": "deny", "reason": exc.code, "detail": exc.detail, "layout": "inline-gate"}
    try:
        _authorize_active_cleanup(root, "partial-report", Path(target), None)
    except ProducerError as exc:
        return {**base, "verdict": "deny", "reason": exc.code, "layout": "cleanup"}
    if rel is None:
        return {**base, "verdict": "allow", "reason": "outside-artifact-root", "layout": None}
    parts = rel.split("/")
    top = parts[0]
    if top.startswith(".") or top == "_scratch":
        return {**base, "verdict": "allow", "reason": "runtime-owned", "layout": "runtime"}
    if top == "shared":
        return {**base, "verdict": "deny", "reason": "shared-revision-immutable", "layout": "shared"}
    if top == "campaigns":
        try:
            locator_mapping, _rows = artifact_locator.scan_index(root)
            target_resolved = Path(target).resolve(strict=False)
            campaign_parts = rel.split("/")
            if len(campaign_parts) >= 3:
                cycle_relative = "/".join(campaign_parts[:3])
                cycle_id = next((identifier for identifier, path in locator_mapping.items()
                                 if path == cycle_relative), None)
                cycle_record = read_cycle_record(root, cycle_id) if cycle_id else None
                if cycle_record:
                    import inline_finish
                    pending = inline_finish.pending_for_cycle(root, cycle_id)
                    if pending and pending.get("state") != "finished":
                        return {**base, "verdict":"deny", "reason":"finish-in-progress",
                                "layout":"cycle", "cycle_id":cycle_id}
        except ProducerError as exc:
            return {**base, "verdict":"deny", "reason":exc.code, "detail":exc.detail, "layout":"cycle"}
        except (OSError, ValueError) as exc:
            return {**base, "verdict":"deny", "reason":"finish-state-unreadable", "detail":str(exc), "layout":"cycle"}
        try:
            require_cycle_output(
                root, target, cycle_id=os.environ.get("AGENT_ARTIFACT_CYCLE_ID"),
                # A node's active route is its write authority. The enclosing
                # owner must not mask a foreign or invalid child route.
                route_id=os.environ.get("AGENT_ROUTE_ID") or os.environ.get("AGENT_OWNER_ROUTE_ID"),
            )
        except ProducerError as exc:
            return {**base, "verdict": "deny", "reason": exc.code, "detail": exc.detail, "layout": "cycle"}
        legacy = len(parts) >= 5 and parts[2] == "cycles"
        readable = len(parts) >= 4 and parts[2] != "cycles"
        if not legacy and not readable:
            return {**base, "verdict": "deny", "reason": "campaign-record-machine-managed", "layout": "cycle"}
        artifacts_index = 4 if legacy else 3
        cycle_path = root.joinpath(*parts[:artifacts_index])
        campaign_path = root / "campaigns" / parts[1]
        campaign = _read_json(campaign_path / "campaign.json") or {}
        campaign_id = campaign.get("campaign_id")
        cycle_id = None
        record = None
        try:
            locator_mapping, _rows = artifact_locator.scan_index(root)
            cycle_relative = cycle_path.resolve().relative_to(root).as_posix()
            cycle_id = next(
                (identifier for identifier, path in locator_mapping.items() if path == cycle_relative),
                None,
            )
        except (artifact_locator.LocatorError, OSError, RuntimeError, ValueError):
            cycle_id = None
        if isinstance(cycle_id, str):
            record = read_cycle_record(root, cycle_id)
        manifest = _read_json(cycle_path / "manifest.json")
        manifest_cycle = manifest.get("cycle") if isinstance(manifest, dict) else None
        if cycle_id is None and isinstance(manifest_cycle, dict):
            cycle_id = manifest_cycle.get("cycle_id")
        if len(parts) <= artifacts_index or parts[artifacts_index] != "artifacts":
            return {**base, "verdict": "deny", "reason": "outside-cycle-artifacts", "layout": "cycle",
                    "cycle_id": cycle_id}
        try:
            observed = os.lstat(target)
            node_kind = ("symlink" if stat.S_ISLNK(observed.st_mode) else
                         "regular" if stat.S_ISREG(observed.st_mode) else
                         "directory" if stat.S_ISDIR(observed.st_mode) else "special")
        except FileNotFoundError:
            node_kind = "missing"
        except OSError:
            node_kind = "special"
        classification = artifact_manifest.classify_artifact_path(
            str(root), campaign_path.relative_to(root).as_posix(),
            cycle_path.relative_to(root).as_posix(), "payload", rel, node_kind,
            prospective=True,
        )
        if not classification.allowed:
            reason = classification.reason or "outside-cycle-artifacts"
            return {**base, "verdict": "deny", "reason": reason,
                    "layout": "cycle", "cycle_id": cycle_id}
        sealed_on_disk = manifest is not None
        if record is None:
            reason = "cycle-sealed" if sealed_on_disk else "cycle-unknown"
            return {**base, "verdict": "deny", "reason": reason, "layout": "cycle", "cycle_id": cycle_id,
                    "hint": CLOSED_CYCLE_HINT}
        if record.get("state") != "open" or record.get("campaign_id") != campaign_id or sealed_on_disk:
            return {**base, "verdict": "deny", "reason": "cycle-not-open", "layout": "cycle", "cycle_id": cycle_id,
                    "hint": CLOSED_CYCLE_HINT}
        bucket = parts[artifacts_index + 1] if len(parts) > artifacts_index + 2 else None
        return {**base, "verdict": "allow", "reason": "open-cycle-artifacts", "layout": "cycle",
                "cycle_id": cycle_id, "campaign_id": campaign_id, "bucket": bucket,
                "output_dir": str(cycle_path / "artifacts")}
    if active:
        denial = {**base, "verdict": "deny", "reason": "legacy-top-level-write-denied", "layout": "legacy",
                  "bucket": top, "hint": LEGACY_WRITE_HINT}
        # Item 7: name where the caller's own cycle actually expects this write,
        # so a stage worker's error names its fix rather than just the refusal.
        # The reason token above stays exactly what it was (D-86: the fleet
        # cutover gate compares it verbatim).
        output_dir = os.environ.get("AGENT_ARTIFACT_OUTPUT_DIR")
        if output_dir:
            denial["expected_output_dir"] = output_dir
        return denial
    klass = classify_root(root)
    if klass["state"] == "malformed":
        # Same reason string as begin(). A damaged cutover record does not
        # slip out through an unmarked legacy allow (D-74: no unmarked allow
        # on any of the three surfaces).
        return {**base, "verdict": "deny", "reason": "cutover-record-malformed",
                "layout": "legacy", "bucket": top, "detail": klass["reason"]}
    fallback = legacy_fallback_state(root, classification=klass)
    if _fallback_blocks(fallback):
        return {**base, "verdict": "deny", "reason": "cutover-inactive-fallback-denied",
                "layout": "legacy", "bucket": top, "legacy_fallback": fallback}
    result = {**base, "verdict": "allow", "reason": "legacy-compat-window", "layout": "legacy", "bucket": top}
    if fallback is not None:
        result["legacy_fallback"] = fallback
    return result


def cycle_bucket(root: Path, target: Path) -> Optional[Tuple[str, str]]:
    """Return (bucket, cycle_id) for a path inside a cycle's artifacts."""
    rel = _relative(Path(root), Path(target))
    if rel is None:
        return None
    parts = rel.split("/")
    if parts[0] != "campaigns":
        return None
    legacy = len(parts) >= 7 and parts[2] == "cycles" and parts[4] == "artifacts"
    readable = len(parts) >= 6 and parts[2] != "cycles" and parts[3] == "artifacts"
    if not legacy and not readable:
        return None
    bucket_index = 5 if legacy else 4
    artifacts_index = 4 if legacy else 3
    cycle_path = Path(root).resolve().joinpath(*parts[:artifacts_index])
    try:
        mapping, _rows = artifact_locator.scan_index(Path(root))
        relative = cycle_path.resolve().relative_to(Path(root).resolve()).as_posix()
    except (artifact_locator.LocatorError, OSError, RuntimeError, ValueError):
        return None
    for cycle_id, path in mapping.items():
        if path != relative:
            continue
        record = read_cycle_record(root, cycle_id)
        if record and record.get("state") == "open":
            return parts[bucket_index], cycle_id
    return None


def resolve_output_dir(root: Path, bucket: str, *, cycle_dir_hint: Optional[str] = None) -> Tuple[Path, str]:
    """Where a writer must place `<bucket>/...` output: cycle layout or legacy."""
    root = Path(root).resolve()
    _authorize_active_cleanup(root, "partial-report", root, os.environ.get("AGENT_ARTIFACT_CYCLE_ID"))
    hint = cycle_dir_hint or os.environ.get("AGENT_ARTIFACT_CYCLE_DIR")
    if hint:
        directory = Path(hint)
        verdict = check_write(root, directory / "artifacts" / bucket / "probe")
        if verdict["verdict"] != "allow":
            raise ProducerError(verdict["reason"], str(directory))
        return directory / "artifacts" / bucket, "cycle"
    if is_active(root):
        raise ProducerError("legacy-top-level-write-denied", f"{bucket}: {LEGACY_WRITE_HINT}")
    klass = classify_root(root)
    if klass["state"] == "malformed":
        raise ProducerError("cutover-record-malformed", klass["reason"] or bucket)
    fallback = legacy_fallback_state(root, classification=klass)
    if _fallback_blocks(fallback):
        raise ProducerError("cutover-inactive-fallback-denied", bucket)
    return root / bucket, "legacy"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _route_autoclose(root: Path, trigger: str, campaign: Optional[str] = None) -> None:
    """Close routes nobody works on before campaign bookkeeping reads them
    (route_autoclose.py).  Bookkeeping only: it never fails the command.
    `campaign` is the one `campaign-close` ends; its members close sooner."""
    try:
        import artifact_cutover
        import route_autoclose
        campaign_id = None
        if campaign is not None:
            record, _raw = artifact_campaign.read_json(root, artifact_campaign.campaign_path(root, campaign))
            campaign_id = record.get("campaign_id")
        route_autoclose.report(route_autoclose.sweep(
            root, api=artifact_cutover._route_module(), trigger=trigger, campaign_id=campaign_id))
    except Exception as exc:  # noqa: BLE001
        print(f"route_autoclose error={type(exc).__name__}", file=sys.stderr)


def _print(payload: Any) -> None:
    print(json.dumps(payload, sort_keys=True))


def _checkpoint_cli(args: argparse.Namespace) -> Dict[str, Any]:
    route_file: Optional[Path] = None
    root_value = args.artifact_root or os.environ.get("AGENT_ARTIFACT_ROOT") or ""
    if args.route:
        route_file = Path(args.route)
        if not root_value:
            raw = _read_json(route_file)
            root_value = str((raw or {}).get("artifact_root") or "")
    cycle_id = args.cycle or (None if args.route else os.environ.get("AGENT_ARTIFACT_CYCLE_ID") or None)
    if not root_value:
        raise ProducerError("checkpoint-target-required", "--artifact-root or $AGENT_ARTIFACT_ROOT")
    root = Path(root_value)
    if route_file is not None:
        route_file = resolve_route_argument(root, route_file)
    return checkpoint(root, cycle_id=cycle_id, route_file=route_file, trigger=args.trigger)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0], allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True,
                                parser_class=functools.partial(argparse.ArgumentParser, allow_abbrev=False))

    p = sub.add_parser("activate")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--repository-id", required=True)
    p.add_argument("--artifact-root-id", required=True)
    p.add_argument("--w7-campaign-id")
    p.add_argument("--w7-cycle-id")
    p.add_argument("--w7-handoff-sha256")
    p.add_argument("--w7-map-sha256")
    p.add_argument("--w7-shared", action="append", default=[], help="kind=ref_id:rrev_id")
    p.add_argument("--approval-receipt-sha256")

    p = sub.add_parser("status")
    p.add_argument("--artifact-root", required=True)

    p = sub.add_parser("campaign-list", help="summarize the root's campaigns (keys, titles, cycle counts)")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--all-states", action="store_true", help="include satisfied/superseded campaigns")

    p = sub.add_parser("cycle-binding-backfill",
                       help="add started_on to .cycle.json bindings written before the field existed")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--apply", action="store_true", help="write the bindings (default: dry run)")

    p = sub.add_parser("cycle-time-recovery",
                       help="recover migrated cycles' start times from the retirement backup's original mtimes")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--backup-store", help="retirement store (default: $XDG_STATE_HOME/hearting/artifact-retirement)")
    p.add_argument("--apply", action="store_true", help="write recovered_started_on into records (default: dry run)")

    p = sub.add_parser("cycle-display-titles-backfill",
                       help="declare cycle display titles for Cairn from sealed cycles (default: dry run)")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--apply", action="store_true", help="write the declaration (default: dry run)")
    p.add_argument("--report", help="write a markdown report to this path")
    p.add_argument("--out", help="write the candidate declaration bytes to this path (outside the artifact root)")
    p.add_argument("--reader-dir", help="Cairn app_dir to gate --apply against (default: ~/.config/cairn-sync/config.json)")
    p.add_argument("--expect-post-digest", help="refuse --apply unless the computed post-digest matches")
    p.add_argument("--restore-journal", help="restore the declaration from a prior --apply's journal")

    for command in ("campaign-status", "campaign-close", "campaign-reopen", "campaign-recover"):
        p = sub.add_parser(command, help="inspect, close, reopen, or recover a campaign")
        p.add_argument("--artifact-root", required=True)
        p.add_argument("--campaign", required=True, help="campaign ID or campaign.json path")
        if command in {"campaign-close", "campaign-reopen"}:
            p.add_argument("--reason")

    p = sub.add_parser("begin")
    p.add_argument("--artifact-root", required=True)
    p.add_argument(
        "--route",
        required=True,
        help="path to the route file, or a bare route id resolved under the artifact root",
    )
    p.add_argument("--node", default=None)
    p.add_argument("--capability", required=True)
    p.add_argument("--intensity", required=True)
    p.add_argument("--campaign", help="existing campaign id to add this cycle to")
    p.add_argument("--campaign-key",
                   help="the work stream this cycle belongs to; reuses the active "
                        "campaign holding that key. Defaults to the sealed route's key. "
                        "With no campaign/key/parent selection, uses the root's "
                        "_unassigned campaign and reports degraded=true. Size: a stream with a "
                        "one-sentence closing condition — not a project name, not a one-cycle task")
    p.add_argument("--title")
    p.add_argument("--goal")
    p.add_argument("--parent-cycle",
                   help="open or sealed predecessor; inherits its campaign and records "
                        "causality, never input or completion approval")
    p.add_argument("--workflow-group-id", help="explicit existing group for this campaign; "
                   "the same-campaign producer context is inherited when omitted")
    p.add_argument("--workflow-stage-label", help="display label for an explicitly grouped cycle")
    p.add_argument("--require-cycle", action="store_true")
    p.add_argument("--shared-reference", action="append", default=[],
                   help="<kind>:<ref>:<rrev>[:<content_digest>], repeatable")
    p.add_argument("--env-file", help="write KEY=VALUE lines for the producer environment")

    p = sub.add_parser("finalize")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--cycle", required=True)
    p.add_argument("--state", default="completed", choices=["completed", "abandoned"])
    p.add_argument("--primary", help="primary artifact as a cycle-relative locator "
                   "(artifacts/<bucket>/<file>); an absolute path inside this cycle's artifacts/ is accepted")
    p.add_argument("--publication", default="not-offered")
    p.add_argument("--allow-open-route", action="store_true")
    p.add_argument("--adopt-root-output", action="append", default=[])
    p.add_argument("--abandon-reason", choices=sorted(ABANDON_REASONS))
    p.add_argument("--force-abandon-ignoring-lease", action="store_true")

    p = sub.add_parser("checkpoint",
                       help="publish an open cycle's interim manifest (the sealed schema with "
                            "cycle.state=open) for readers such as Cairn; rate-limited per cycle")
    p.add_argument("--artifact-root",
                   help="default: $AGENT_ARTIFACT_ROOT, else the route file's artifact_root")
    p.add_argument("--cycle", help="default: $AGENT_ARTIFACT_CYCLE_ID when --route is absent")
    p.add_argument("--route", help="route file or bare route id; selects the route's one open cycle")
    p.add_argument("--trigger", default="explicit", choices=CHECKPOINT_TRIGGERS)

    p = sub.add_parser("review-lease")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--cycle", required=True)
    p.add_argument("--attempt")
    p.add_argument("--deadline-seconds", type=float, default=900.0)
    p.add_argument("operation", choices=["acquire", "release", "status"])

    p = sub.add_parser("admit-shared")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--cycle", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--reference")
    p.add_argument("--key")
    p.add_argument("--title")
    p.add_argument("--promote-research", action="store_true")
    p.add_argument("--promotion-evidence")
    p.add_argument("--drop-component", action="append", default=[], metavar="NAME",
                   help="D-87 (b): drop this top-level component from the reference "
                        "(repeatable); the only way to shrink the component set")
    p.add_argument("--drop-reason", help="reason recorded with --drop-component")
    p.add_argument("--base-revision", help="actual base revision of an unseeded spec, or none for initial publication")
    p.add_argument("--new-reference", action="store_true",
                   help="allow a second reference of a canonical-singular kind (spec) when neither --reference nor --key matches")

    p = sub.add_parser("check-components")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--reference", required=True)
    p.add_argument("--from-revision", help="first revision of the inspected window")
    p.add_argument("--to-revision", help="last revision of the inspected window")

    p = sub.add_parser("recover")
    p.add_argument("--artifact-root", required=True)

    p = sub.add_parser("check-write")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--file", required=True)

    p = sub.add_parser("resolve-output")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--bucket", required=True)
    p.add_argument("--cycle-dir")

    args = parser.parse_args(argv)
    try:
        if args.command not in {"check-write"}:
            dispatch_terminal_commit.require_current_cleanup("producer-" + args.command)
        if args.command == "checkpoint":
            _print(_checkpoint_cli(args))
            return OK
        root = Path(args.artifact_root)
        if args.command == "activate":
            w7: Dict[str, Any] = {}
            if args.w7_campaign_id:
                w7["campaign_id"] = args.w7_campaign_id
            if args.w7_cycle_id:
                w7["cycle_id"] = args.w7_cycle_id
            if args.w7_handoff_sha256:
                w7["handoff_sha256"] = args.w7_handoff_sha256
            if args.w7_map_sha256:
                w7["compatibility_map_sha256"] = args.w7_map_sha256
            shared: Dict[str, Any] = {}
            for row in args.w7_shared:
                kind, _, ids = row.partition("=")
                ref, _, rrev = ids.partition(":")
                shared[kind] = {"shared_reference_id": ref, "shared_reference_revision_id": rrev}
            if shared:
                w7["shared"] = shared
            result = activate(root, repository_id=args.repository_id, artifact_root_id=args.artifact_root_id,
                              w7=w7, approval_receipt_sha256=args.approval_receipt_sha256)
        elif args.command == "status":
            result = status(root)
        elif args.command == "campaign-list":
            rows = list_campaign_summaries(root, active_only=not args.all_states)
            result = {"status": "ok", "artifact_root": str(Path(root).resolve()), "campaigns": rows}
        elif args.command == "cycle-binding-backfill":
            result = backfill_cycle_bindings(root, apply=args.apply)
        elif args.command == "cycle-time-recovery":
            result = recover_cycle_times(root, apply=args.apply,
                                         backup_store=Path(args.backup_store) if args.backup_store else None)
        elif args.command == "cycle-display-titles-backfill":
            if args.restore_journal:
                result = artifact_cycle_titles.restore_backfill(root, Path(args.restore_journal))
            else:
                result = artifact_cycle_titles.backfill(
                    root, apply=args.apply,
                    report_path=Path(args.report) if args.report else None,
                    out_path=Path(args.out) if args.out else None,
                    reader_dir=Path(args.reader_dir) if args.reader_dir else None,
                    expect_post_digest=args.expect_post_digest,
                )
            _print(result)
            return BLOCKED if str(result.get("status", "")).startswith("refused") else OK
        elif args.command == "campaign-status":
            _route_autoclose(root, "campaign-status")
            result = artifact_campaign.status(root, args.campaign)
        elif args.command == "campaign-close":
            _route_autoclose(root, "campaign-close", campaign=args.campaign)
            result = artifact_campaign.close(root, args.campaign, reason=args.reason)
        elif args.command == "campaign-reopen":
            result = artifact_campaign.reopen(root, args.campaign, reason=args.reason)
        elif args.command == "campaign-recover":
            result = artifact_campaign.recover(root, args.campaign)
        elif args.command == "begin":
            pins: List[Dict[str, Any]] = []
            for row in args.shared_reference:
                parts = row.split(":", 3)
                if len(parts) < 3:
                    raise ProducerError("shared-reference-pin-invalid", row)
                kind, ref_id, rrev_id = parts[0], parts[1], parts[2]
                pin: Dict[str, Any] = {
                    "kind": kind, "shared_reference_id": ref_id, "shared_reference_revision_id": rrev_id,
                }
                if len(parts) > 3:
                    pin["content_digest"] = parts[3]
                pins.append(pin)
            result = begin(root, route_file=Path(args.route), capability=args.capability,
                           intensity=args.intensity, node_id=args.node, campaign_id=args.campaign,
                           campaign_key=args.campaign_key, title=args.title, goal=args.goal,
                           parent_cycle_id=args.parent_cycle,
                           workflow_group_id=args.workflow_group_id,
                           workflow_stage_label=args.workflow_stage_label,
                           require_cycle=args.require_cycle,
                           shared_reference_pins=pins or None)
            if args.env_file:
                lines = "".join(f"{k}={v}\n" for k, v in result.get("env", {}).items())
                Path(args.env_file).write_text(lines, encoding="utf-8")
        elif args.command == "finalize":
            result = finalize(root, cycle_id=args.cycle, state=args.state, primary=args.primary,
                              publication=args.publication, allow_open_route=args.allow_open_route,
                              adopt_root_outputs=args.adopt_root_output,
                              abandon_reason=args.abandon_reason,
                              force_abandon_ignoring_lease=args.force_abandon_ignoring_lease)
            if result.get("warning"):
                print(result["warning"], file=sys.stderr)
        elif args.command == "review-lease":
            if args.operation == "acquire":
                if not args.attempt:
                    raise ProducerError("review-lease-attempt-required")
                result = review_lease_acquire(root, cycle_id=args.cycle, attempt_id=args.attempt,
                                              deadline_seconds=args.deadline_seconds)
            elif args.operation == "release":
                if not args.attempt:
                    raise ProducerError("review-lease-attempt-required")
                result = review_lease_release(root, cycle_id=args.cycle, attempt_id=args.attempt)
            else:
                result = review_lease_status(root, cycle_id=args.cycle, attempt_id=args.attempt)
        elif args.command == "admit-shared":
            result = admit_shared(root, cycle_id=args.cycle, kind=args.kind, source=args.source,
                                  reference_id=args.reference, key=args.key, title=args.title,
                                  promote_research=args.promote_research,
                                  promotion_evidence=args.promotion_evidence,
                                  drop_components=args.drop_component,
                                  drop_reason=args.drop_reason,
                                  allow_new_reference=args.new_reference, base_revision=args.base_revision)
        elif args.command == "check-components":
            result = check_component_sets(root, args.kind, args.reference,
                                          from_revision=args.from_revision,
                                          to_revision=args.to_revision)
            _print(result)
            return OK if result["violations"] == 0 else BLOCKED
        elif args.command == "recover":
            result = recover(root)
        elif args.command == "check-write":
            result = check_write(root, Path(args.file))
            _print(result)
            return OK if result["verdict"] == "allow" else BLOCKED
        elif args.command == "resolve-output":
            directory, layout = resolve_output_dir(root, args.bucket, cycle_dir_hint=args.cycle_dir)
            result = {"status": "ok", "output_dir": str(directory), "layout": layout}
            if layout == "legacy":
                fallback = legacy_fallback_state(root)
                if fallback is not None:
                    result["legacy_fallback"] = fallback
        else:  # pragma: no cover
            parser.error("unknown command")
            return USAGE
    except ProducerError as exc:
        _print({"status": "blocked", "reason": exc.code, "detail": exc.detail})
        return BLOCKED
    except artifact_cycle_titles.CycleTitlesError as exc:
        _print({"status": "blocked", "reason": exc.code, "detail": exc.detail})
        return BLOCKED
    except artifact_admission.AdmissionBusy as exc:
        _print({"status": "blocked", "reason": "admission-busy", "detail": str(exc)})
        return BLOCKED
    except artifact_admission.AdmissionRecoveryRequired as exc:
        _print({"status": "blocked", "reason": "recovery-required", "detail": str(exc)})
        return BLOCKED
    except (artifact_lifecycle.LifecycleError, artifact_campaign.CampaignError) as exc:
        _print({"status": "blocked", "reason": exc.code, "detail": exc.detail})
        return BLOCKED
    except (OSError, ValueError) as exc:
        _print({"status": "blocked", "reason": "request-invalid", "detail": str(exc)})
        return BLOCKED
    _print(result)
    return OK


if __name__ == "__main__":
    raise SystemExit(main())
