"""Read one attempt's process and result evidence through the shared policy.

Registry projections, recovery and launch consumers translate this observation;
they do not infer liveness from an open registry word or from a launch claim.
This reader owns no storage and never grants a launch or overwrites a result.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from dispatch_attempt_policy import AttemptDecision, OPEN_STATES, TERMINAL_STATES, decide_attempt


@dataclass(frozen=True)
class AttemptEvidence:
    process: object
    result_state: str
    terminal: dict
    decision: AttemptDecision

    @property
    def exact_dead(self) -> bool:
        return self.process.state == "quiescent"

    @property
    def death_note(self) -> str:
        return ("dead-parent-terminated" if self.process.reason.startswith("parent-terminal-") else
                "dead-namespace-absent" if self.process.reason == "namespace-extinct" else "dead-exact-pid")


def observe_attempt(
    status: str, metadata: Mapping[str, str], *,
    worktree: str | Path | None = None,
    repo: str | Path | None = None,
    artifact_root: str | Path | None = None,
    process: object | None = None,
    terminal: dict | None = None,
    registry_rows: Iterable[str] | None = None,
) -> AttemptEvidence:
    """Observe existing evidence; callers re-read it at their existing lock.

The optional observations let an already-bound consumer reuse its exact process
proof or terminal inspection. Imports stay lazy because the process observer and
terminal writer also consume this policy. File absence and readable incomplete
logs have no terminal result; inaccessible evidence remains unknown.
"""
    if terminal is None:
        from codex_dispatch_terminal import inspect_terminal_attempt
        terminal = inspect_terminal_attempt(
            metadata.get("log_file"),
            worktree=worktree or metadata.get("worktree"),
            artifact_root_metadata=artifact_root or metadata.get("artifact_root"),
            worker_type=metadata.get("worker_type"),
        )
    if process is None:
        from dispatch_contract import attempt_process_quiescence
        from codex_dispatch_terminal import EXACT_BOUNDARY_SOURCES
        # An actual final boundary already has the wrapper's existing durable
        # receipt obligation. Missing/partial logs and a launch claim do not.
        boundary = terminal.get("source") in set().union(*EXACT_BOUNDARY_SOURCES.values())
        process = attempt_process_quiescence(
            dict(metadata), terminal_receipt=status in TERMINAL_STATES or boundary,
        )
    if process.state == "unverifiable" and registry_rows is not None:
        from dispatch_contract import ProcessQuiescence, parse_registry_metadata, resolve_parent_extinction
        rows = []
        for raw in registry_rows:
            fields = raw.split("\t")
            if len(fields) == 6:
                rows.append((fields, parse_registry_metadata(fields[5])))
        bound = dict(metadata)
        if repo is not None:
            bound["repo"] = str(repo)
        if worktree is not None:
            bound["worktree"] = str(worktree)
        parent = resolve_parent_extinction(bound, rows)
        if parent.state == "proven":
            # The existing foreground lifetime contract supplies exact proof
            # when that namespace is no longer observable. Positive child
            # process evidence was handled above and always wins.
            process = ProcessQuiescence("quiescent", parent.reason)
    if status in TERMINAL_STATES:
        result_state = "settled"
    elif status not in OPEN_STATES:
        result_state = "unverifiable"
    else:
        result_state = {
            "valid": "settleable", "absent": "none", "invalid": "invalid",
        }.get(terminal.get("state"), "unverifiable")
    decision = decide_attempt(
        status, metadata, process_state=process.state, process_reason=process.reason,
        result_state=result_state,
    )
    return AttemptEvidence(process, result_state, terminal, decision)
