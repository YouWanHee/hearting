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
        self._registry_views = {}
        self._handover_snapshots = None
        self._handover_ledgers = {}
        self._recipient_censuses = {}
        self._transfer_roots = None

    def transfer_roots(self, loader):
        """Reuse candidate ledger roots for exact per-message transfer reads.

        The root list only narrows where each transfer record is read. It does
        not cache transfer contents or any delivery/recipient authority.
        """
        if self._transfer_roots is None:
            roots, _source = loader()
            self._transfer_roots = tuple(Path(root) for root in roots)
        return self._transfer_roots

    def recipient_census(self, endpoint, loader):
        """Reuse one endpoint's recipient listing within a single read pass."""
        key = str(endpoint or "default")
        if key not in self._recipient_censuses:
            self._recipient_censuses[key] = loader()
        return self._recipient_censuses[key]

    def current_attempt_row(self, jobs: Path, attempt_id: str):
        """Return the latest exact attempt row from this scan's registry view."""
        from dispatch_completion_join import ChildRow, JoinContractError
        from dispatch_registry_cache import registry_view

        jobs = Path(jobs)
        key = str(jobs.resolve(strict=False))
        if key not in self._registry_views:
            try:
                self._registry_views[key] = registry_view(jobs)
            except FileNotFoundError:
                self._registry_views[key] = None
            except OSError as exc:
                raise JoinContractError("registry-unreadable") from exc
        view = self._registry_views[key]
        if view is None:
            return None
        record, error = view.completion_attempt(attempt_id)
        if error:
            raise JoinContractError(error)
        if record is None:
            return None
        return ChildRow(record.order, record.status, record.slug, attempt_id,
                        record.raw, record.completion_metadata())

    def handover_snapshots(self, loader):
        if self._handover_snapshots is None:
            self._handover_snapshots = tuple(loader())
        return self._handover_snapshots

    def handover_rows(self, seat, loader):
        key = (str(getattr(seat, "kind", "")), str(getattr(seat, "key", "")))
        if key not in self._handover_ledgers:
            self._handover_ledgers[key] = tuple(loader())
        return self._handover_ledgers[key]
