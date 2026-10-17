#!/usr/bin/env python3
"""Per-scan reuse of immutable registry and handover read inputs.

Instances are short lived and owned by one peer-message scan. They cache no
delivery state, claim, acknowledgement, or authority across a state-changing
operation.
"""

from __future__ import annotations

from pathlib import Path


class PeerReadContext:
    def __init__(self):
        self._attempt_indexes = {}
        self._handover_snapshots = None
        self._handover_ledgers = {}

    def current_attempt_row(self, jobs: Path, attempt_id: str):
        """Return the latest exact attempt row from this scan's registry view."""
        from dispatch_completion_join import ChildRow, JoinContractError, _metadata
        from dispatch_registry_cache import registry_lines

        jobs = Path(jobs)
        key = str(jobs.resolve(strict=False))
        if key not in self._attempt_indexes:
            try:
                lines = registry_lines(jobs)
            except FileNotFoundError:
                self._attempt_indexes[key] = (None, {})
            except OSError as exc:
                raise JoinContractError("registry-unreadable") from exc
            else:
                latest = {}
                errors = {}
                for order, line in enumerate(lines):
                    if "attempt_id=" not in line:
                        continue
                    fields = line.split("\t")
                    if len(fields) != 6:
                        continue
                    metadata = _metadata(fields[5])
                    row_attempt = metadata.get("attempt_id")
                    if not row_attempt:
                        continue
                    if metadata.get("attempt_schema_version") != "2":
                        errors.setdefault(row_attempt, JoinContractError(
                            "attempt-row-schema-invalid"))
                        continue
                    latest[row_attempt] = ChildRow(
                        order, fields[1], fields[4], row_attempt, line, metadata
                    )
                self._attempt_indexes[key] = (latest, errors)

        latest, errors = self._attempt_indexes[key]
        if attempt_id in errors:
            raise errors[attempt_id]
        return latest.get(attempt_id) if latest is not None else None

    def handover_snapshots(self, loader):
        if self._handover_snapshots is None:
            self._handover_snapshots = tuple(loader())
        return self._handover_snapshots

    def handover_rows(self, seat, loader):
        key = (str(getattr(seat, "kind", "")), str(getattr(seat, "key", "")))
        if key not in self._handover_ledgers:
            self._handover_ledgers[key] = tuple(loader())
        return self._handover_ledgers[key]
