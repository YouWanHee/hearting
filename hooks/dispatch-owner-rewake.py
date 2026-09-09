#!/usr/bin/env python3
"""Wake an interactive Claude parent once when its exact headless owner finishes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "utilities"))
from dispatch_contract import (  # noqa: E402
    DispatchContractError,
    resolve_agent_home as _resolve_agent_home,
    runtime_ancestry_binding,
)
from dispatch_completion_join import (  # noqa: E402
    CurrentDeliveryState,
    JoinContractError,
    current_attempt_row,
    current_children,
    current_delivery_state,
    delivery_classification,
    delivery_required_action,
)
import dispatch_pending_delivery as pending_delivery  # noqa: E402
from dispatch_session_sweep import (  # noqa: E402
    HUMAN_GATE_PREFIX,
    _bounded_receipt_text,
    is_human_gate_record,
)
# SD-111 P3 §4.4: carrier 1 must never create a pending-delivery record --
# it only claims one trigger 1/2 already produced. The materializer function
# from dispatch_completion_join is deliberately absent from this import
# block; DispatchOwnerRewakeMaterializeAbsenceTest statically asserts that.


ATTEMPT = re.compile(r"att-[A-Za-z0-9._-]{1,240}\Z")
DEFAULT_INTERVAL_SECONDS = 5  # one readiness probe costs ~0.1s; 20s dominated the wake tail (2026-08-27)
DEFAULT_MAX_SECONDS = 21_600
DEFAULT_ARM_WINDOW_SECONDS = 600
MAXIMUM_CLOCK_SKEW_SECONDS = 60
REGISTRY_OWNER_START = {
    "worker_type": "owner",
    "dispatch_depth": "1",
    "parent_completion_delivery": "claude-parent-runtime",
    "launch_claimed": "1",
    "launch_started": "1",
}
SUCCESS_NOTIFICATION = "\x1b]9;Hearting dispatch completed\x07"
CLAIM_LEASE_SECONDS = 30.0


@dataclass(frozen=True)
class Launch:
    attempt_id: str
    jobs: Path
    session_id: str
    armed: str = "stdout"


def _stdout(response: object) -> str:
    if not isinstance(response, dict):
        return ""
    value = response.get("stdout")
    return value if isinstance(value, str) else ""


def _fields(output: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[a-z][a-z0-9_.-]*", key):
            continue
        result.setdefault(key, []).append(value)
    return result


def _single(fields: dict[str, list[str]], key: str) -> str | None:
    values = fields.get(key, [])
    return values[0] if len(values) == 1 else None


def _payload_session(payload: dict[str, Any]) -> str | None:
    value = payload.get("session_id")
    return value if isinstance(value, str) and value else None


def _validated_jobs(raw: str | None) -> Path | None:
    """Apply the one registry-path security boundary to every arming route."""

    if not raw:
        return None
    jobs = Path(raw)
    if not jobs.is_absolute() or jobs.is_symlink() or not jobs.is_file():
        return None
    return jobs


_START_NAMES = {"dispatch-owner", "dispatch-owner.py", "dispatch-node", "dispatch-node.py"}
_RELEASE_NAMES = {"workflow-supervisor", "workflow-supervisor.py"}


def _shell_segments(command: str) -> list[list[str]]:
    """Split a Bash command line into simple commands, each as shlex tokens.

    Control operators (`;`, `&&`, `||`, `|`, `&`, newline) end a segment only
    outside quotes, backslash escapes, `$( … )` and backticks; `arg;` with no
    whitespace is still a boundary (codex R6, 2026-09-09). `&` that belongs
    to a redirection (`2>&1`, `&>`, `<&`) is not an operator (codex R7 M1),
    and a command substitution's own `;` stays inside its word (R7 M2). A
    segment that does not tokenize is dropped, never merged into a neighbour."""

    segments: list[str] = []
    current: list[str] = []
    in_single = in_double = in_backtick = escaped = False
    subst_depth = 0
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        nxt = command[i + 1] if i + 1 < n else ""
        prev = command[i - 1] if i else ""
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\" and not in_single:
            current.append(ch)
            escaped = True
        elif ch == "'" and not in_double and not in_backtick:
            in_single = not in_single
            current.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
        elif in_single:
            current.append(ch)
        elif ch == "`":
            in_backtick = not in_backtick
            current.append(ch)
        elif ch == "$" and nxt == "(":
            subst_depth += 1
            current.append("$(")
            i += 1
        elif ch == ")" and subst_depth:
            subst_depth -= 1
            current.append(ch)
        elif in_double or in_backtick or subst_depth:
            current.append(ch)
        elif ch in ";|\n" or (ch == "&" and not (nxt == ">" or prev in "<>")):
            segments.append("".join(current))
            current = []
            if ch in "|&" and nxt == ch:
                i += 1
        else:
            current.append(ch)
        i += 1
    segments.append("".join(current))
    tokens: list[list[str]] = []
    for raw in segments:
        try:
            parts = shlex.split(raw)
        except ValueError:
            continue
        if parts:
            tokens.append(parts)
    return tokens


_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_REDIRECTION = re.compile(r"^(?:\d*[<>]{1,2}&?\d*|&>>?)")


def _command_position(parts: list[str]) -> int | None:
    """Index of the simple command's actual command word.

    Leading `NAME=value` assignments, `env` with its assignments, and bare
    redirection words are prefixes; the next word is the command. Anything
    else (echo, printf, ...) is a foreign command and a utility name appearing
    later is one of *its* arguments (codex R7 B1: `echo python3 … --start`
    was recognized as an owner start and armed a prior attempt)."""

    index = 0
    while index < len(parts) and (_ASSIGNMENT.match(parts[index]) or _REDIRECTION.match(parts[index])):
        index += 1
    if index < len(parts) and Path(parts[index]).name == "env":
        index += 1
        while index < len(parts) and (_ASSIGNMENT.match(parts[index]) or parts[index].startswith("-")):
            if parts[index] in {"-u", "--unset", "-C", "--chdir"}:
                index += 1
            index += 1
    return index if index < len(parts) else None


def _launched_by(parts: list[str], index: int) -> bool:
    """The recognized utility must *be* the segment's command, or the first
    argument of a python / preflight.sh command — never a word inside some
    other command's argument list."""

    command_at = _command_position(parts)
    if command_at is None:
        return False
    if index == command_at:
        return True
    if index != command_at + 1:
        return False
    launcher = Path(parts[command_at]).name
    # `preflight.sh dispatch-owner --start` is the adapters' documented
    # launch surface; recognizing only a python launcher left every such
    # owner unarmed (observed 2026-08-27, cairn att-092eb89f/att-5da8bc24).
    return re.fullmatch(r"python(?:3(?:\.\d+)?)?", launcher) is not None or launcher == "preflight.sh"


def _recognized_start(command: str) -> tuple[str, list[str]] | None:
    """`(surface, arguments)` of the first owner-start segment, else None.

    One parser feeds both the surface decision and every option reader, so
    they can never disagree about which segment launched the owner."""

    for parts in _shell_segments(command):
        for index, token in enumerate(parts):
            name = Path(token).name
            if name not in _START_NAMES or not _launched_by(parts, index):
                continue
            arguments = parts[index + 1 :]
            if name in {"dispatch-owner", "dispatch-owner.py"}:
                if "--start" in arguments:
                    return "dispatch-owner", arguments
                continue
            if "--action=start" in arguments or any(
                value == "--action" and arguments[offset + 1] == "start"
                for offset, value in enumerate(arguments[:-1])
            ):
                return "dispatch-node", arguments
    return None


def _recognized_release(command: str) -> list[str] | None:
    """Arguments of the first `workflow-supervisor.py release` /
    `gate … --release` segment, else None."""

    for parts in _shell_segments(command):
        for index, token in enumerate(parts):
            if Path(token).name not in _RELEASE_NAMES or not _launched_by(parts, index):
                continue
            arguments = parts[index + 1 :]
            if arguments and (arguments[0] == "release" or (arguments[0] == "gate" and "--release" in arguments)):
                return arguments
    return None


def _start_surface(command: str) -> str | None:
    """Recognize only the two typed depth-1 owner start command surfaces."""

    recognized = _recognized_start(command)
    return recognized[0] if recognized else None


def _start_segment(command: str) -> str | None:
    """The recognized owner-start segment's arguments, re-quoted for the
    option readers — the same tokens `_start_surface` decided on."""

    recognized = _recognized_start(command)
    return " ".join(shlex.quote(a) for a in recognized[1]) if recognized else None


def _release_segment(command: str) -> str | None:
    arguments = _recognized_release(command)
    return " ".join(shlex.quote(a) for a in arguments) if arguments is not None else None


def _owner_start_command(payload: object) -> tuple[dict[str, Any], str] | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("hook_event_name") != "PostToolUse" or payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str) or _start_surface(command) is None:
        return None
    return payload, command


def parse_launch(payload: object) -> Launch | None:
    """Accept only a successful exact depth-1 owner start from this session."""

    gate = _owner_start_command(payload)
    if gate is None:
        return None
    payload, command = gate
    fields = _fields(_stdout(payload.get("tool_response")))
    required_memberships = {
        "check": "ok",
        "status": "start",
        "dispatch_depth": "1",
        "worker_type": "owner",
        "parent_completion_delivery": "claude-parent-runtime",
        "registered": "1",
        "started": "1",
    }
    if any(expected not in fields.get(key, []) for key, expected in required_memberships.items()):
        return None
    attempt_id = _single(fields, "attempt_id")
    parent_session = _single(fields, "parent_session_id")
    payload_session = _payload_session(payload)
    if (
        payload_session is None
        or parent_session != payload_session
        or attempt_id is None
        or ATTEMPT.fullmatch(attempt_id) is None
    ):
        return None
    jobs = _validated_jobs(_single(fields, "job_registry"))
    if jobs is None:
        return None
    return Launch(attempt_id=attempt_id, jobs=jobs, session_id=payload_session, armed="stdout")


def _command_literal_option(command: str, name: str) -> str | None:
    """Read one `--name value` / `--name=value` literal from a command segment.

    The *last* occurrence wins, as the selector's `_parse` and the wrappers'
    argparse `store` read a repeated flag (codex R5 / OpenCode cross-check,
    2026-09-09); any occurrence carrying `$` (an unexpanded shell variable)
    or an empty value disqualifies the option entirely — never fall back to
    an earlier literal the launch did not honour."""

    return _command_literal_option_last(command, name)


def _command_jobs(command: str) -> str | None:
    """Read the inherited registry path the launch segment itself declared.

    Last occurrence wins (the selector and every wrapper store the last
    `--jobs`); an unexpanded variable or empty value anywhere disqualifies
    the option so the hook never arms from a registry the launch did not use
    (codex R5 B1, 2026-09-09)."""

    return _command_literal_option_last(command, "jobs")


def _registry_metadata(pipe: str) -> dict[str, str]:
    """Tolerantly read the six-column registry pipe in comma or space dual form."""

    def pairs(parts: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for part in parts:
            key, separator, value = part.strip().partition("=")
            if separator and key:
                result[key] = value
        return result

    comma = pairs(pipe.split(","))
    return comma if "attempt_id" in comma else pairs(pipe.replace(",", " ").split())


def _row_age(stamp: str, now: float) -> float | None:
    try:
        moment = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return now - moment.timestamp()


def _canonical_jobs() -> str | None:
    """The installed harness's own registry — the path every wrapper writes by default.

    A launch command that spells `--jobs "$J"` hands this hook an unexpanded shell
    variable, and the hook's own environment carries no `AGENT_DISPATCH_JOBS`; both
    left a real, session-bound owner row unarmed (observed 2026-08-26, two owner
    attempts in a row). The canonical root is deterministic from the agent home, so
    it is the last resort before giving up — the row match itself stays exact."""
    try:
        from dispatch_contract import resolve_dispatch_state_root  # noqa: WPS433

        return str(resolve_dispatch_state_root(_resolve_agent_home(), None) / "jobs.log")
    except Exception:  # noqa: BLE001 — absence beats misattribution
        return None


def _route_evidence_literals(command: str) -> dict[str, str]:
    """`slug`/`worktree` a short route-backed start command implies.

    `dispatch-owner --start --route-evidence <file> --prompt-file <brief>`
    carries no `--slug`/`--worktree` literal: the selector fills both from the
    route (2026-09-09). The route file is the same exact evidence the selector
    read, so its `slug` and `cwd` narrow a wave the way the literals do. Only
    a plain absolute regular-file path counts; anything else narrows nothing."""

    literal = _command_literal_option_last(command, "route-evidence")
    if literal is None:
        return {}
    path = Path(literal)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        return {}
    try:
        route = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(route, dict):
        return {}
    found: dict[str, str] = {}
    slug = route.get("slug")
    if isinstance(slug, str) and slug:
        found["slug"] = slug
    cwd = route.get("cwd")
    if isinstance(cwd, str) and cwd:
        found["worktree"] = cwd
    return found


def _command_option_present(command: str, name: str) -> bool:
    """Whether `--name value` / `--name=value` appears at all, literal or not."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    flag = f"--{name}"
    return any(part == flag or part.startswith(flag + "=") for part in parts)


def _command_literal_option_last(command: str, name: str) -> str | None:
    """Like `_command_literal_option`, but the *last* occurrence wins — the
    selector's `_parse` overwrites `--route-evidence` on repeat, so the hook
    must read the same file it read. Any non-literal occurrence disqualifies."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    flag = f"--{name}"
    found: str | None = None
    for index, part in enumerate(parts):
        value: str | None = None
        if part == flag and index + 1 < len(parts):
            value = parts[index + 1]
        elif part.startswith(flag + "="):
            value = part[len(flag) + 1 :]
        if value is None:
            continue
        if "$" in value or not value:
            return None
        found = value
    return found


def registry_launch(payload: object) -> Launch | None:
    """Arm from the wrapper-written registry when stdout was filtered away.

    A piped `dispatch-owner --start | tail` hands this hook a truncated stdout,
    so the receipt fast path silently fails to arm.  The lock-written registry
    row carries the same exactness — one open depth-1 owner attempt bound to
    this Claude session, started inside the immediately preceding tool window —
    and is harder to forge than tool output.  When several same-session rows
    share the window (a wave of owner starts, 2026-09-01: five fleet cleanup
    owners), the observed launch command's own `--slug`/`--worktree` literals
    narrow them before the exactly-one gate; only an exact single survivor
    arms.  Zero or still-ambiguous candidates stay an unarmed result: absence
    beats misattribution.  (The caller turns that unarmed result into one typed
    `not-armed` notice — see `no_arm_notice` — instead of a silent loss.)
    """

    resolved = _registry_start_candidates(payload)
    if resolved is None:
        return None
    command, session, jobs, candidates = resolved
    if len(candidates) > 1:
        # A wave of same-session owner starts is legitimate; the command this
        # exact hook invocation observed names which one it launched.
        # Narrow by every axis the command states, then by what the route file
        # it names implies for the axes it leaves out — and only then decide.
        # Safety properties (codex R3 + Fable cross-check, 2026-09-09):
        #   1. an explicit option whose value is not a literal (unexpanded
        #      variable, empty) fails closed for the whole narrowing — the
        #      selector honoured that value, so no other axis may pick a row;
        #   2. an explicit literal that matches no candidate fails closed —
        #      the launch this hook observed did not make any of these rows;
        #   3. no early exit: every explicit axis is checked before a single
        #      survivor may arm, so one axis cannot bypass another.
        # A route-implied value that matches nothing narrows nothing (the row
        # may not be visible yet); ambiguity after all axes arms nothing.
        # Last occurrence wins, exactly as the selector's `_parse` and the
        # three wrappers' argparse `store` read a repeated flag (OpenCode
        # cross-check 2026-09-09: first-wins armed the previous attempt).
        segment = _start_segment(command)
        if segment is None:
            return None
        explicit: dict[str, str] = {}
        for option in ("slug", "worktree"):
            if _command_option_present(segment, option):
                value = _command_literal_option_last(segment, option)
                if value is None:
                    return None
                explicit[option] = value
        implied = _route_evidence_literals(segment) if len(explicit) < 2 else {}
        for option, position in (("slug", 2), ("worktree", 1)):
            literal = explicit.get(option, implied.get(option))
            if literal is None:
                continue
            narrowed = [row for row in candidates if row[position] == literal]
            if not narrowed:
                if option in explicit:
                    return None
                continue
            candidates = narrowed
    if len(candidates) != 1:
        return None
    return Launch(
        attempt_id=candidates[0][0], jobs=jobs, session_id=session, armed="registry"
    )


def _registry_start_candidates(
    payload: object,
) -> tuple[str, str, Path, list[tuple[str, str, str]]] | None:
    """Same-session, recent, claimed-and-started owner rows for one observed
    start command: ``(command, session, jobs, [(attempt_id, worktree, slug)])``.

    ``None`` means the payload is not an owner-start Bash call or no registry
    resolves; candidate absence is an empty list, never ``None``."""

    gate = _owner_start_command(payload)
    if gate is None:
        return None
    payload, command = gate
    session = _payload_session(payload)
    if session is None:
        return None
    fields = _fields(_stdout(payload.get("tool_response")))
    jobs = (
        _validated_jobs(_single(fields, "job_registry"))
        or _validated_jobs(_command_jobs(_start_segment(command) or ""))
        or _validated_jobs(os.environ.get("AGENT_DISPATCH_JOBS"))
        or _validated_jobs(_canonical_jobs())
    )
    if jobs is None:
        return None
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    latest: dict[str, tuple[str, str, str, str, dict[str, str]]] = {}
    for line in lines:
        columns = line.split("\t")
        if len(columns) != 6:
            continue
        metadata = _registry_metadata(columns[5])
        attempt_id = metadata.get("attempt_id", "")
        if ATTEMPT.fullmatch(attempt_id) is None:
            continue
        latest[attempt_id] = (columns[0], columns[1], columns[3], columns[4], metadata)
    window = _bounded_number(
        "AGENT_CLAUDE_REWAKE_ARM_WINDOW_SECONDS", DEFAULT_ARM_WINDOW_SECONDS, 30, 86_400
    )
    now = time.time()
    candidates: list[tuple[str, str, str]] = []
    for attempt_id, (stamp, status, worktree, slug, metadata) in latest.items():
        if status != "open" or metadata.get("parent_sid") != session:
            continue
        if any(metadata.get(key) != value for key, value in REGISTRY_OWNER_START.items()):
            continue
        age = _row_age(stamp, now)
        if age is None or age > window or age < -MAXIMUM_CLOCK_SKEW_SECONDS:
            continue
        candidates.append((attempt_id, worktree, slug))
    return command, session, jobs, candidates


RELEASE_OWNER_ROW = dict(REGISTRY_OWNER_START)  # the same row shape the start path arms on


def _release_command(command: str) -> tuple[str, str | None] | None:
    """Recognize `workflow-supervisor.py release …` and `gate … --release`.

    Returns `(gate, route_literal_or_None)`; anything else is None. Only the
    typed supervisor surface counts -- the same discipline `_start_surface`
    applies to owner starts, from the same segment parser."""

    segment = _release_segment(command)
    if segment is None:
        return None
    gate = _command_literal_option(segment, "gate")
    if gate is None:
        return None
    return gate, _command_literal_option(segment, "route")


def release_launch(payload: object) -> Launch | None:
    """SD-129: a successful gate release re-arms the wait on that route's owner.

    The in-wait gate wake (`wait_for_attempt` -> `gate`) spends this hook
    process's one wake. The owner is still alive, waiting on `await-release`,
    and its completion still owes the session a wake -- and the person's
    release is itself a Bash call in this same session. So the release command
    is the second arming event: the release output names the `route_id`, the
    registry names that route's one open depth-1 owner bound to this session,
    and the new hook process waits on it exactly as the start did. A failed
    release (no JSON payload naming the route -- review round 1, M1: the
    `--route` literal is never a fallback for the route id; the one refusal
    that still arms, SD-OPEN-48 (#13), is the supervisor's typed
    `refusal=gate-not-blocked` line -- the gate was already released and the
    owner is running towards a completion that still owes this session a
    wake), a route with no started open owner (the owner
    ended at the gate -- contract (c); or a row that was registered and
    refused at start), or an ambiguous set of owner rows arms nothing and
    says so once (`release_no_arm_notice`); the SD-111 sweep still delivers
    the completion at the next prompt.
    """

    if not isinstance(payload, dict):
        return None
    if payload.get("hook_event_name") != "PostToolUse" or payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return None
    recognized = _release_command(command)
    if recognized is None:
        return None
    gate, route_literal = recognized
    session = _payload_session(payload)
    if session is None:
        return None
    stdout = _stdout(payload.get("tool_response"))
    route_id: str | None = None
    armed = "release"
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rendered = json.loads(line)
        except ValueError:
            continue
        if isinstance(rendered, dict) and rendered.get("gate") == gate:
            value = rendered.get("route_id")
            route_id = value if isinstance(value, str) and value else None
            if rendered.get("refusal") == GATE_NOT_BLOCKED_REFUSAL:
                # SD-OPEN-48 (#13): the supervisor refused this release as
                # already released (on 2026-09-07 by the headless owner itself,
                # cairn W15b rt-bf75754935faf8de) and the owner is RUNNING
                # towards its completion. That completion still owes this
                # session a wake, and the start-armed hook already spent its
                # one wake on the gate, so arming nothing here is exactly the
                # lost wake SD-111 exists to prevent. The route id comes from
                # the supervisor's typed refusal line, never from the prose
                # and never from the `--route` literal; every other refusal
                # prints no JSON and arms nothing.
                armed = "release-refused"
            elif rendered.get("refusal"):
                route_id = None
        break
    if not isinstance(route_id, str) or not route_id:
        return None
    jobs = (
        _validated_jobs(_command_jobs(_release_segment(command) or ""))
        or _validated_jobs(os.environ.get("AGENT_DISPATCH_JOBS"))
        or _validated_jobs(_canonical_jobs())
    )
    if jobs is None:
        return None
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    latest: dict[str, tuple[str, dict[str, str]]] = {}
    for line in lines:
        columns = line.split("\t")
        if len(columns) != 6:
            continue
        metadata = _registry_metadata(columns[5])
        attempt_id = metadata.get("attempt_id", "")
        if ATTEMPT.fullmatch(attempt_id) is None:
            continue
        latest[attempt_id] = (columns[1], metadata)
    candidates = [
        attempt_id
        for attempt_id, (status, metadata) in latest.items()
        if status == "open"
        and metadata.get("parent_sid") == session
        and all(metadata.get(key) == value for key, value in RELEASE_OWNER_ROW.items())
        and route_id in (metadata.get("owner_route_id"), metadata.get("route_id"))
    ]
    if len(candidates) != 1:
        return None
    return Launch(attempt_id=candidates[0], jobs=jobs, session_id=session, armed=armed)


# One definition, shared with `workflow-supervisor.py`'s refusal line: the
# supervisor prints `{"gate", "route_id", "refusal": "gate-not-blocked", ...}`
# on stdout before its prose refusal (review finding 13).
GATE_NOT_BLOCKED_REFUSAL = "gate-not-blocked"


def _refused_release_route_id(payload: dict) -> str | None:
    """The `route_id` of a release the supervisor refused as already released
    (typed `refusal=gate-not-blocked` line), else None."""

    stdout = _stdout(payload.get("tool_response"))
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rendered = json.loads(line)
        except ValueError:
            continue
        if isinstance(rendered, dict) and rendered.get("refusal") == GATE_NOT_BLOCKED_REFUSAL:
            value = rendered.get("route_id")
            return value if isinstance(value, str) and value else None
        return None
    return None


def release_no_arm_notice(payload: object) -> int:
    """One typed exit-0 notice when a proven release armed nothing (review round
    1, M2): the session was told the release re-arms the wait, so a silent
    non-arm would be the lost wake SD-111 exists to prevent. A refused release
    (no JSON payload) stays silent -- its own stderr already told the story --
    except one refused as already released (SD-OPEN-48), whose typed refusal
    line names the route and so was expected to arm."""

    if not isinstance(payload, dict):
        return 0
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or _release_command(command) is None:
        return 0
    stdout = _stdout(payload.get("tool_response"))
    if _refused_release_route_id(payload) is not None:
        # SD-OPEN-48 (#13): refused as already released, but no single started
        # open owner of that route is bound to this session -- say so once.
        message = (
            "[dispatch-owner-rewake] schema=2 state=not-armed surface=release-refused — this gate "
            "was already released (workflow not blocked), and no single started open depth-1 owner "
            "of that route is bound to this session, so the owner's completion will not wake this "
            "session from this command. If the owner is still running, its completion arrives "
            "through the UserPromptSubmit sweep at your next prompt. Do not start Monitor, "
            "dispatch-wait, or a polling loop."
        )
        print(json.dumps({"systemMessage": message}, ensure_ascii=False, separators=(",", ":")))
        return 0
    if not any(line.strip().startswith("{") and '"route_id"' in line for line in stdout.splitlines()):
        return 0
    message = (
        "[dispatch-owner-rewake] schema=2 state=not-armed surface=release — this release recorded, "
        "but no single started open depth-1 owner of that route is bound to this session, so the "
        "owner's completion will not wake this session from this command. If the owner is still "
        "running, its completion arrives through the UserPromptSubmit sweep at your next prompt; "
        "if it ended at the gate, continue the route with a continuation owner. Do not start "
        "Monitor, dispatch-wait, or a polling loop."
    )
    print(json.dumps({"systemMessage": message}, ensure_ascii=False, separators=(",", ":")))
    return 0


def agent_home() -> Path:
    return _resolve_agent_home()


def _bounded_number(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


def wait_for_attempt(
    launch: Launch, readiness: Path, *, gate_probe: Any = None, deadline: float | None = None
) -> tuple[str, str]:
    """Poll the exact attempt to terminal quiescence.

    `gate_probe` (SD-129) is consulted once per interval while the attempt is
    still open: when it reports an open human gate addressed to this attempt,
    the wait ends with `("gate", "human-gate-open")` so the caller can wake the
    session NOW instead of at the owner's terminal. Before this cycle the gate
    rode only the terminal wake, so an owner that stayed alive at its gate --
    the correct behaviour -- reached the person only through the next-prompt
    sweep (measured 2026-09-04, rt-da62cded: 3.5 minutes, and only because the
    user happened to type).
    """
    interval = _bounded_number(
        "AGENT_CLAUDE_REWAKE_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS, 1, 300
    )
    maximum = _bounded_number(
        "AGENT_CLAUDE_REWAKE_MAX_SECONDS", DEFAULT_MAX_SECONDS, interval, 86_400
    )
    # The deadline is the caller's when it re-enters after a gate probe (review
    # round 1, B3): recomputing it here on every re-entry made the bound
    # unreachable.
    if deadline is None:
        deadline = time.monotonic() + maximum
    command = [
        sys.executable,
        str(readiness),
        "--jobs",
        str(launch.jobs),
        "--attempt-id",
        launch.attempt_id,
    ]
    while True:
        try:
            result = subprocess.run(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return "bridge-error", type(exc).__name__
        if result.returncode == 0:
            return "ready", "terminal-quiescent"
        if result.returncode == 3:
            return "attention", "terminal-failure-or-unclosed"
        if result.returncode != 2:
            return "bridge-error", f"readiness-exit-{result.returncode}"
        if time.monotonic() >= deadline:
            return "timeout", f"owner-not-quiescent-after-{maximum}s"
        if gate_probe is not None and gate_probe():
            return "gate", "human-gate-open"
        time.sleep(interval)


def _completion_evidence_current(state: CurrentDeliveryState) -> bool:
    """Consume the marker identity already verified inside the jobs lock."""

    marker = state.marker
    return bool(
        isinstance(marker, dict)
        and re.fullmatch(r"[0-9a-f]{64}", state.marker_digest)
        and marker.get("route_id")
        and marker.get("route_hash")
        and marker.get("node_id")
        and marker.get("attempt_id")
    )


def classified_receipt(
    launch: Launch, state: str, reason: str, root: Path
) -> tuple[str, str]:
    row = None
    delivery = None
    transaction_error = ""
    try:
        if state in {"ready", "attention"}:
            try:
                delivery = current_delivery_state(
                    launch.jobs,
                    launch.attempt_id,
                    parent_attempt_id=launch.attempt_id,
                )
            except (DispatchContractError, JoinContractError, OSError) as exc:
                transaction_error = (
                    exc.reason
                    if isinstance(exc, DispatchContractError)
                    else str(exc) or type(exc).__name__
                )
        # Rendering the sealed launch-home path is deliberately separate from
        # classification. The transaction above is the only current-row
        # decision authority.
        row = current_attempt_row(launch.jobs, launch.attempt_id)
    except (JoinContractError, OSError):
        row = None
    # The row's sealed launch_home (SD-49) is the checkout the attempt actually
    # ran under; `root` (agent_home(), preferring env AGENT_HOME) may point at a
    # mutable primary checkout instead when this hook inherits that env var --
    # a harvest command built from `root` can then be rejected by a parent
    # guard that expects the sealed path. Prefer the sealed value and fall back
    # to `root` only when it is missing or no longer names a real checkout.
    sealed = row.metadata.get("launch_home") if row is not None else None
    home = Path(sealed) if sealed else root
    if not (home / "adapters" / "codex" / "bin" / "preflight.sh").is_file():
        home = root
    harvest = home / "adapters" / "codex" / "bin" / "preflight.sh"
    jobs_argument = shlex.quote(str(launch.jobs))
    status = delivery.status if delivery is not None else ""
    row_revision = delivery.row_revision if delivery is not None else "unavailable"
    marker_current = bool(delivery and _completion_evidence_current(delivery))
    if delivery is not None:
        owned_children = delivery.owned_children
    elif transaction_error:
        try:
            owned_children = sum(
                child.status in {"open", "running"}
                and child.metadata.get("registered_worker") == "1"
                and child.metadata.get("execution_surface") == "registered-headless"
                for child in current_children(launch.jobs, launch.attempt_id)
            )
        except (JoinContractError, OSError):
            owned_children = 0
    else:
        owned_children = 0
    quiescent = bool(delivery and delivery.quiescent)
    advanced = bool(delivery and delivery.advanced)
    row_digest = delivery.row_digest if delivery is not None else "unavailable"
    if (
        state in {"ready", "attention"}
        and delivery is not None
        and delivery.status in {"open", "running", "done"}
    ):
        required_action = delivery_required_action(delivery)
        snapshot_state = state
        state = delivery_classification(delivery)
        if state == "success":
            reason = (
                "row-advanced"
                if delivery.advanced or snapshot_state == "attention"
                else "terminal-complete"
            )
        else:
            reason = "terminal-failure-or-unclosed"
        if state == "success":
            instruction = "No harvest command is required; the registered owner completed."
        elif required_action == "complete-open":
            instruction = (
                "Use only the exact checked harvest command: "
                f"{shlex.quote(str(harvest))} harvest --jobs {jobs_argument} "
                f"--attempt-id {shlex.quote(launch.attempt_id)} --status open --mark-done."
            )
        elif required_action == "inspect-done-failure":
            instruction = (
                "Use only the exact checked harvest command: "
                f"{shlex.quote(str(harvest))} harvest --jobs {jobs_argument} "
                f"--attempt-id {shlex.quote(launch.attempt_id)} --status done --failure-detail."
            )
        elif required_action == "advance-completed":
            instruction = "No harvest command is required; advance or finish the route."
        elif required_action.startswith(HUMAN_GATE_PREFIX):
            instruction = (
                "A human gate is open; it is answered, not harvested. Read the artifact named "
                "in the gate record, then record the answer with "
                "workflow-supervisor.py release --route <route file> --gate "
                f"{shlex.quote(required_action[len(HUMAN_GATE_PREFIX):] or '-')} "
                "--decision proceed|revise|stop."
            )
        else:
            instruction = (
                "Inspect the exact current row and completion marker with: "
                f"{shlex.quote(str(harvest))} harvest --jobs {jobs_argument} "
                f"--attempt-id {shlex.quote(launch.attempt_id)} --status done "
                "--failure-detail."
            )
    elif transaction_error:
        state = "attention"
        reason = f"delivery-transaction-failed-{transaction_error}"
        required_action = "complete-open" if owned_children else "inspect-bridge"
        instruction = (
            "A real owned child remains open; inspect only the sealed registry."
            if owned_children
            else "The delivery transaction needs attention; no open child was observed, so do not block this Stop."
        )
    else:
        required_action = "inspect-bridge"
        instruction = "Inspect the typed bridge state; do not harvest or re-arm it."
    title = (
        "Hearting dispatch completed"
        if state == "success"
        else "Hearting dispatch requires attention"
    )
    message = (
        f"{title}. Runtime owner completion receipt "
        f"schema=2 state={state} attempt_id={launch.attempt_id} armed={launch.armed} "
        f"status={status or '-'} row_revision={row_revision} "
        f"row_digest={row_digest} marker_current={int(marker_current)} "
        f"quiescent={int(quiescent)} owned_children={owned_children} "
        f"advanced={int(advanced)} "
        f"reason={reason} required_action={required_action}. "
        "Do not start or re-arm Background Bash, Monitor, liveness, or dispatch-wait. "
        f"{instruction} Do not emit a periodic progress recap."
    )
    return state, message


def receipt(launch: Launch, state: str, reason: str, root: Path) -> str:
    """Compatibility text view used by tests and non-hook callers."""

    return classified_receipt(launch, state, reason, root)[1]


TERMINAL_STATES = frozenset({"success", "attention"})


def emit_receipt(state: str, message: str, *, block: bool | None = None) -> int:
    """Render the receipt and choose the exit code that actually wakes Claude.

    Claude Code delivers an `asyncRewake` hook's exit-0 output only "on the
    next conversation turn" -- an idle session stays asleep until the user
    types -- and wakes the session immediately only on exit code 2, showing
    stderr (or stdout when stderr is empty) as a system reminder
    (code.claude.com/docs/en/hooks, "Run hooks in the background"). Until
    2026-08-29 a completed owner exited 0 here, so every successful
    completion waited for the next user prompt and looked like a lost wake
    (five observed "gap 4" incidents). Every terminal receipt -- success or
    attention -- now exits 2 with the receipt on stderr; the structured
    stdout payload is kept for success and non-blocking attention so a
    transcript reader sees the same notice as before. Non-terminal bridge
    states (timeout, bridge-error) keep exit 0: there is nothing to wake for.
    """

    if block is None:
        block = state != "success"
    if state == "success" or not block:
        payload = {"systemMessage": message}
        if state == "success":
            payload["terminalSequence"] = SUCCESS_NOTIFICATION
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        if state in TERMINAL_STATES:
            print(message, file=sys.stderr)
            return 2
        return 0
    print(message, file=sys.stderr)
    return 2


def _attention_has_open_child(message: str) -> bool:
    match = re.search(r"\bowned_children=([0-9]+)\b", message)
    return bool(match and int(match.group(1)) > 0)


def _incarnation_binding_matches(metadata: dict[str, str]) -> bool:
    """SD-111 P2 round 2 C-3: the recorded launch-time triple must match this
    hook process's own walk exactly on all three fields. Absence, a partial
    recording, or any mismatch is a fail-closed no (§3.2.1's fork)."""

    recorded = (
        metadata.get("parent_runtime_pid", ""),
        metadata.get("parent_runtime_pid_start", ""),
        metadata.get("parent_runtime_ns", ""),
    )
    if not all(recorded):
        return False
    observed = runtime_ancestry_binding(os.getpid())
    if observed is None:
        return False
    return recorded == observed


@dataclass(frozen=True)
class ClaimWin:
    claim_owner: str
    recipient_key: str
    delivery_id: str
    root: Path


def _delivery_owing_row(launch: Launch) -> dict[str, str] | None:
    """Return the row's metadata only when it is a genuine delivery-owing
    terminal completion (`done` + `delivery_intent` stamped, §4.3.1) -- the
    claim gate governs exactly this population. A still-open/running row
    (e.g. `wait_for_attempt` timed out or hit a bridge error) has taken no
    terminal edge yet and carries no intent; gating *that* notice behind
    claim would silence a live diagnostic forever, which is the opposite of
    what SD-111 exists to prevent, so it is deliberately left ungated."""

    try:
        row = current_attempt_row(launch.jobs, launch.attempt_id)
    except (JoinContractError, OSError):
        return None
    if row is None or row.status != "done" or row.metadata.get("delivery_intent") != "1":
        return None
    return row.metadata


def _carrier_one_claim(launch: Launch, metadata: dict[str, str]) -> ClaimWin | None:
    """P3 claim gate. Never creates a record (§4.4 -- carrier 1 only claims
    what trigger 1/2 already materialized); returns the winning claim on
    success, or ``None`` on any refusal -- claim lost, record already
    acked/claimed, record not yet materialized, or the incarnation binding
    does not match. Every ``None`` path is a silent, zero-notice exit-0 per
    SD-97 (no re-delivery)."""

    if not _incarnation_binding_matches(metadata):
        return None
    delivery_id = metadata.get("delivery_id", "")
    recipient_key = metadata.get("parent_sid", "")
    if not delivery_id or not recipient_key:
        return None
    root = launch.jobs.resolve(strict=False).parent
    claim_owner = f"claude-async-rewake:{os.getpid()}:{time.monotonic_ns()}"
    try:
        pending_delivery.claim(
            root,
            recipient_key,
            delivery_id,
            claim_owner=claim_owner,
            lease_seconds=CLAIM_LEASE_SECONDS,
            require_generation_proof=False,
        )
    except pending_delivery.PendingDeliveryError:
        return None
    return ClaimWin(claim_owner, recipient_key, delivery_id, root)


_RECIPIENT_KEY_CACHE: dict[tuple[str, str], str] = {}


def _recipient_key(launch: Launch) -> str:
    """The owner row's `parent_sid`, read from the registry once per hook
    process: the row's recipient never changes, and re-reading `jobs.log` every
    interval for the owner's whole life was the per-interval cost review round
    1 (minor 9) measured."""

    key = (str(launch.jobs), launch.attempt_id)
    cached = _RECIPIENT_KEY_CACHE.get(key)
    if cached:
        return cached
    try:
        row = current_attempt_row(launch.jobs, launch.attempt_id)
    except (JoinContractError, OSError):
        return ""
    recipient_key = row.metadata.get("parent_sid", "") if row is not None else ""
    if recipient_key:
        _RECIPIENT_KEY_CACHE[key] = recipient_key
    return recipient_key


def _recipient_gate_records(launch: Launch) -> list[tuple[Path, str, str, dict]]:
    """Every open gate record addressed to this launch's depth-0 session:
    `(root, recipient_key, delivery_id, record)`; empty on any refusal."""

    recipient_key = _recipient_key(launch)
    if not recipient_key:
        return []
    root = launch.jobs.resolve(strict=False).parent
    try:
        directory = pending_delivery.record_directory(root, recipient_key)
        entries = sorted(p for p in directory.glob("delivery-*.json") if p.is_file())
    except (pending_delivery.PendingDeliveryError, OSError):
        return []
    found: list[tuple[Path, str, str, dict]] = []
    for entry in entries:
        delivery_id = entry.stem
        try:
            record = pending_delivery.read(root, recipient_key, delivery_id)
        except pending_delivery.PendingDeliveryError:
            continue
        if record is None or record.get("state") not in pending_delivery.OPEN_STATES:
            continue
        if not is_human_gate_record(record):
            continue
        found.append((root, recipient_key, delivery_id, record))
    return found


_PROBE_DIRECTORY_MTIME: dict[str, float] = {}
_PROBE_NEXT_DEADLINE_NS: dict[str, int] = {}


def _open_gate_pending(launch: Launch) -> bool:
    """SD-129 probe for `wait_for_attempt`: is a gate record for THIS attempt
    waiting for a carrier? `pending`, or a lease another carrier let expire,
    and still claimable (`attempts` below `RECLAIM_LIMIT`; a record whose
    reclaim budget is spent is left to the release-time retirement, so the
    hook never spins on something it can never claim -- review round 1, B3).
    A live claim by the sweep is left alone -- it acks within its own turn.

    The recipient directory is only scanned when its mtime moved since the
    last probe -- a record being created, claimed or acked rewrites a file in
    it -- or when the earliest live lease seen at the last scan has now expired
    (a lease expiring is a clock event with no write; review round 2, N1).
    """

    recipient_key = _recipient_key(launch)
    if not recipient_key:
        return False
    root = launch.jobs.resolve(strict=False).parent
    try:
        directory = pending_delivery.record_directory(root, recipient_key)
        mtime = directory.stat().st_mtime
    except (pending_delivery.PendingDeliveryError, OSError):
        return False
    key = str(directory)
    now = time.monotonic_ns()
    next_deadline = _PROBE_NEXT_DEADLINE_NS.get(key)
    if _PROBE_DIRECTORY_MTIME.get(key) == mtime and (next_deadline is None or now < next_deadline):
        return False
    _PROBE_DIRECTORY_MTIME[key] = mtime
    _PROBE_NEXT_DEADLINE_NS.pop(key, None)
    for _root, _key, _delivery_id, record in _recipient_gate_records(launch):
        if launch.attempt_id not in (record.get("attempt_ids") or []):
            continue
        if (record.get("attempts") or 0) >= pending_delivery.RECLAIM_LIMIT:
            continue
        state = record.get("state")
        if state == "pending":
            return True
        if state in {"claimed", "sent-ambiguous"}:
            deadline = int(record.get("claim_deadline_ns") or 0)
            if deadline < now:
                return True
            _PROBE_NEXT_DEADLINE_NS[key] = min(_PROBE_NEXT_DEADLINE_NS.get(key, deadline), deadline)
    return False


def _gate_notices(launch: Launch, *, attempt_only: bool = False, settle: str = "ack") -> list[str]:
    """SD-123 (8)(b) carrier 1: fold every open gate record for this recipient
    into the wake this hook is about to emit.

    Two call sites since SD-129. The terminal wake still folds in every open
    gate for the recipient (contract (c): a gate outlives its owner). The
    in-wait wake (`wait_for_attempt` returned `gate`) passes `attempt_only` so
    it announces only the gate this owner raised -- a parallel owner's gate is
    that owner's hook's wake, and spending this process's single wake on it
    would lose this attempt's completion notice.

    `settle` is how the announced record is left. The terminal wake acks
    (A59-3: a gate folded into a terminal receipt is not re-announced). The
    in-wait wake passes `sent-ambiguous` (review round 1, M6): whether an
    exit-2 wake reaches the session is unmeasured (SD-OPEN-29/32), and an
    acked record would have spent the sweep fallback the contract still
    requires. The cost is bounded at-least-once: if the person has not
    released by the next prompt the sweep shows the same gate once more.

    Each record is claimed and then acked -- not left `sent-ambiguous`. A gate
    record is a pointer; the gate itself is durable in the workflow ledger, so
    the usual at-least-once argument does not apply, and re-delivery would put
    the same gate in front of the user on every prompt (A59-3 forbids that).
    """

    notices: list[str] = []
    for root, recipient_key, delivery_id, record in _recipient_gate_records(launch):
        if attempt_only and launch.attempt_id not in (record.get("attempt_ids") or []):
            continue
        claim_owner = f"claude-async-rewake-gate:{os.getpid()}:{time.monotonic_ns()}"
        try:
            if record.get("state") in {"claimed", "sent-ambiguous"}:
                # `now_ns` is keyword-only and required. Omitting it raised
                # TypeError, which the `except PendingDeliveryError` below does
                # NOT catch — so the whole rewake hook died and the parent lost
                # its completion receipt entirely, not just the gate. Reachable
                # whenever a sweep claimed the record first, an ack failed, two
                # parallel-group rewakes raced, or after `mark_sent_ambiguous`.
                # The sibling call in `dispatch_session_sweep.sweep_deliver`
                # always passed it; this one never did, and no test covered it.
                # `monotonic_ns`, not `time_ns`: `reclaim` compares `now_ns`
                # against `claim_deadline_ns`, which `claim` stamps from
                # `time.monotonic_ns()`. An epoch-clock value is astronomically
                # larger, so every deadline would look passed and a live lease
                # would be reclaimed out from under its holder. The sweep sibling
                # passes `time.monotonic_ns()` for the same reason.
                pending_delivery.reclaim(
                    root, recipient_key, delivery_id, now_ns=time.monotonic_ns()
                )
            pending_delivery.claim(
                root, recipient_key, delivery_id, claim_owner=claim_owner,
                lease_seconds=CLAIM_LEASE_SECONDS, require_generation_proof=False,
            )
        except pending_delivery.PendingDeliveryError:
            continue
        notices.append(_bounded_receipt_text(record))
        try:
            if settle == "sent-ambiguous":
                pending_delivery.mark_sent_ambiguous(
                    root, recipient_key, delivery_id, claim_owner=claim_owner
                )
            else:
                pending_delivery.ack(
                    root, recipient_key, delivery_id, acked_by=f"async-rewake:{launch.session_id}"
                )
        except pending_delivery.PendingDeliveryError:
            pass
    return notices


def gate_wake_message(launch: Launch, notices: list[str]) -> str:
    """The in-wait gate wake: bounded, typed, and explicit that the owner is
    still alive and waiting -- so the session answers the gate instead of
    harvesting the attempt."""

    return (
        "Hearting human gate awaiting your decision (SD-123/129). Runtime gate receipt "
        f"schema=2 state=attention attempt_id={launch.attempt_id} armed={launch.armed} "
        "owner=alive-waiting required_action=human-gate. "
        "The owner is waiting on `workflow-supervisor.py await-release`; it is answered, not "
        "harvested. Read the artifact named below (an interview file or frame summary), put "
        "the [방향 확인] card -- and every interview question, one topic at a time, in plain "
        "words -- to the user through AskUserQuestion, then record the answer with "
        "`workflow-supervisor.py release --route <route file> --gate <name> --decision "
        "proceed|revise|stop [--answers <answers.json>]`. That release command re-arms this "
        "hook for the owner's completion. Do not start or re-arm Background Bash, Monitor, "
        "liveness, or dispatch-wait, and do not emit a periodic progress recap. "
        + " ".join(notices)
    )


def no_arm_notice(payload: object) -> int:
    """One typed notice when a successful start armed neither bridge path.

    A grep-filtered `dispatch-owner --start` stdout plus an ambiguous
    registry window loses the wake silently: the owner completes, nothing
    wakes the parent, and the durable SD-111 record may also be refused for a
    route-less owner (2026-09-01: five fleet cleanup owners, four unwatched
    terminals).  Arming stays fail-closed — absence beats misattribution —
    but the *loss* must be loud (fix candidate ③ of the 2026-08-24 quick-gap
    record): tell the launching session immediately that completion will not
    wake it, so it relaunches with unfiltered stdout or uses the explicit
    poll-fallback.  A start whose own stdout already reports failure keeps
    telling that story itself; this notice never fires for it."""

    gate = _owner_start_command(payload)
    if gate is None:
        return 0
    inner, _command = gate
    raw_stdout = _stdout(inner.get("tool_response"))
    fields = _fields(raw_stdout)
    started = "start" in fields.get("status", []) and "1" in fields.get("started", [])
    if raw_stdout.strip() and not started:
        return 0
    if not raw_stdout.strip():
        resolved = _registry_start_candidates(payload)
        if resolved is None or not resolved[3]:
            return 0
    attempt_id = _single(fields, "attempt_id") or "unknown"
    message = (
        f"[dispatch-owner-rewake] schema=2 state=not-armed attempt_id={attempt_id} "
        "— this owner start reported started=1 but the asyncRewake bridge did NOT arm "
        "(filtered stdout or an ambiguous recent-candidate window), so its completion "
        "will not wake this session and no bridge is watching it. Relaunch future "
        "starts with unfiltered stdout, or watch this exact attempt via the explicit "
        "poll-fallback (dispatch-wait --attempt-id <id>); do not wait for a wake that "
        "cannot arrive."
    )
    print(json.dumps({"systemMessage": message}, ensure_ascii=False, separators=(",", ":")))
    print(message, file=sys.stderr)
    return 2


def main() -> int:
    try:
        payload: Any = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError):
        return 0
    launch = parse_launch(payload) or registry_launch(payload) or release_launch(payload)
    if launch is None:
        return no_arm_notice(payload) or release_no_arm_notice(payload)
    root = agent_home()
    readiness = root / "utilities" / "dispatch-attempt-ready.py"
    if not readiness.is_file():
        state, message = classified_receipt(
            launch, "bridge-error", "readiness-helper-missing", root
        )
    else:
        interval = _bounded_number(
            "AGENT_CLAUDE_REWAKE_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS, 1, 300
        )
        maximum = _bounded_number(
            "AGENT_CLAUDE_REWAKE_MAX_SECONDS", DEFAULT_MAX_SECONDS, interval, 86_400
        )
        deadline = time.monotonic() + maximum
        while True:
            wait_state, wait_reason = wait_for_attempt(
                launch, readiness, gate_probe=lambda: _open_gate_pending(launch),
                deadline=deadline,
            )
            if wait_state != "gate":
                break
            # SD-129: the owner is alive at its gate. Wake the person now with
            # this process's one wake; the release command re-arms the wait
            # (`release_launch`). A probe that another carrier beat to the
            # record (nothing left to announce) sleeps one interval and resumes
            # waiting -- never a tight loop (review round 1, B3).
            notices = _gate_notices(launch, attempt_only=True, settle="sent-ambiguous")
            if notices:
                return emit_receipt("attention", gate_wake_message(launch, notices), block=False)
            time.sleep(interval)
        state, message = classified_receipt(launch, wait_state, wait_reason, root)
    # SD-123 (8)(b): an open gate rides this same wake. It also forces the
    # attention state -- a route whose next step is a human decision has not
    # succeeded, however cleanly the owner attempt ended.
    gates = _gate_notices(launch)
    if gates:
        state = "attention"
        message = (
            message
            + " A human gate is open and awaiting your decision; it is answered, not harvested. "
            + " ".join(gates)
        )
    block = _attention_has_open_child(message)
    if block:
        # A live owned child is still open -- no delivery-owing terminal
        # transition has happened yet (§4.3.1 stamps intent only at
        # open|running -> done), so there is nothing to claim. This keeps
        # Claude from stopping prematurely; SD-111's claim gate does not
        # apply to it.
        return emit_receipt(state, message, block=True)
    owing = _delivery_owing_row(launch)
    if owing is None:
        # Not (yet) a delivery-owing terminal completion -- still open/
        # running (timeout, bridge error) or a non-SD-111 row. Emit exactly
        # as before the claim gate existed; only a genuine delivery-owing
        # terminal notice is claim-gated.
        return emit_receipt(state, message, block=False)
    win = _carrier_one_claim(launch, owing)
    if win is None:
        return 0
    exit_code = emit_receipt(state, message, block=False)
    try:
        pending_delivery.mark_sent_ambiguous(
            win.root, win.recipient_key, win.delivery_id, claim_owner=win.claim_owner,
        )
    except pending_delivery.PendingDeliveryError:
        pass
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
