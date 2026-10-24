"""Runtime seam between collected Fleet data and the pure explorer renderer.

The initial implementation deliberately preserves the existing view model and
input behavior. Collection, ordering, and persistence policy can be supplied by
the live adapter without moving I/O into the drawing code.
"""
from __future__ import annotations

from collections.abc import Mapping
import time


class ExplorerRuntime:
    """Behaviour-preserving runtime hooks for the explorer."""

    def prepare(self, snapshot, detail, *, vm, generation, now):
        """Return the already-built model and collection readiness for this generation."""
        model = vm
        readiness = getattr(model, "readiness", None)
        if readiness is None:
            observations = getattr(snapshot, "observations", None)
            if observations is None and isinstance(snapshot, Mapping):
                observations = snapshot.get("observations")
            observations = observations if isinstance(observations, Mapping) else {}
            source_states = {}
            for name, item in observations.items():
                state = item.get("state") if isinstance(item, Mapping) else getattr(item, "state", None)
                reason = item.get("reason") if isinstance(item, Mapping) else getattr(item, "reason", None)
                error = item.get("error") if isinstance(item, Mapping) else getattr(item, "error", None)
                source_states[str(name)] = {"state": state or "unknown", "reason": reason, "error": error}
            pending = tuple(sorted(name for name, item in source_states.items()
                                   if item["state"] == "pending"))
            # A missing/disabled source is complete unknown; only an explicit
            # pending observation delays the first complete frame.
            readiness = {"ready": not pending, "generation": generation,
                         "pending": pending, "sources": source_states,
                         "observed_at": now if now is not None else time.time()}
        return model, readiness

    def apply_effects(self, effects, vm):
        """Apply runtime-owned effects after reduction; the initial seam is inert."""
        return None

    def loading_frame(self, cols, rows):
        """Return a small terminal-safe loading frame while a source is pending."""
        from .frame import CellFrame
        from .grid import CellGrid
        grid = CellGrid(cols, rows)
        if rows > 0:
            label = "불러오는 중"
            grid.put(max(0, (cols - len(label)) // 2), max(0, rows // 2), label, "dim")
        return CellFrame(grid.snapshot(), tuple(), int(cols), int(rows), "OVERVIEW", "loading")

    def collection_options(self, first_complete):
        """Collect full route/detail facts before publishing the first complete frame."""
        return {"fast_first": not bool(first_complete)}


def tag_cycle(ordering):
    """Read a supplied ordering cycle without imposing an ordering policy."""
    if isinstance(ordering, Mapping):
        value = ordering.get("tag_cycle", ())
    else:
        value = getattr(ordering, "tag_cycle", ())
    if callable(value):
        value = value()
    return tuple(value or ())
