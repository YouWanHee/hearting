"""Runtime seam between collected Fleet data and the pure explorer renderer.

The initial implementation deliberately preserves the existing view model and
input behavior. Collection, ordering, and persistence policy can be supplied by
the live adapter without moving I/O into the drawing code.
"""
from __future__ import annotations

from collections.abc import Mapping


class ExplorerRuntime:
    """Behaviour-preserving runtime hooks for the explorer."""

    def prepare(self, snapshot, detail, *, vm, generation, now):
        """Return the already-built model and its current readiness facts."""
        model = vm
        readiness = getattr(model, "readiness", None)
        if readiness is None:
            readiness = {"ready": True, "generation": generation}
        return model, readiness

    def apply_effects(self, effects, vm):
        """Apply runtime-owned effects after reduction; the initial seam is inert."""
        return None

    def loading_frame(self, cols, rows):
        """Return a loading frame only while readiness is explicitly pending."""
        # The renderer checks readiness before calling this hook. Keeping the
        # default inert preserves the first-generation screen until the adapter
        # supplies an explicit loading presentation.
        return None

    def collection_options(self, first_complete):
        """Keep the live collector's current fast-first behavior."""
        return {"fast_first": True}


def tag_cycle(ordering):
    """Read a supplied ordering cycle without imposing an ordering policy."""
    if isinstance(ordering, Mapping):
        value = ordering.get("tag_cycle", ())
    else:
        value = getattr(ordering, "tag_cycle", ())
    if callable(value):
        value = value()
    return tuple(value or ())
