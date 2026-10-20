"""Pass-local recipient lifetime evidence for peer watch settlement.

The result is intentionally tri-state. This module never serializes evidence and
does not infer lifetime from wall-clock age, activity timestamps, or retries.
"""

from __future__ import annotations

from collections.abc import Callable


ALIVE = "alive"
ENDED = "ended"
UNKNOWN = "unknown"
_STATES = {ALIVE, ENDED, UNKNOWN}


class RecipientCensus:
    """Memoize one bounded evidence probe for each recipient during one pass."""

    __slots__ = ("_probe", "_results", "_host_result")

    def __init__(self, probe: Callable[[str, str], str] | None = None):
        self._probe = probe
        self._results: dict[tuple[str, str], str] = {}
        self._host_result = None

    def state(self, harness: str, session_id: str) -> str:
        key = (harness, session_id)
        if key not in self._results:
            result = UNKNOWN
            if self._probe is None and harness == "claude":
                if self._host_result is None:
                    try:
                        from claude_process_identity import claude_session_census
                        self._host_result = claude_session_census()
                    except Exception:
                        self._host_result = (False, set(), {"census-failed"})
                complete, holders, _reasons = self._host_result
                if session_id in holders:
                    result = ALIVE
                elif complete:
                    result = ENDED
            elif self._probe is not None:
                try:
                    observed = self._probe(harness, session_id)
                    if observed in _STATES:
                        result = observed
                except Exception:
                    result = UNKNOWN
            self._results[key] = result
        return self._results[key]


def recipient_state(*, harness: str, session_id: str,
                    seat_state: str | None = None,
                    lifetime_state: str | None = None,
                    successor_state: str | None = None,
                    census: RecipientCensus | None = None) -> str:
    """Resolve exact positive lifetime evidence without turning uncertainty into end.

    A confirmed successor or live seat keeps the duty alive. A failed or
    conflicting successor lookup is unknown. Existing seat/lifetime rules may
    prove end. A seat-less Claude recipient can use the pass-local complete
    census; seat-less Codex and OpenCode remain unknown absent their existing
    positive lifetime evidence.
    """
    if successor_state == ALIVE or seat_state == ALIVE or lifetime_state == ALIVE:
        return ALIVE
    if successor_state not in (None, ENDED):
        return UNKNOWN
    if seat_state not in (None, ALIVE, ENDED):
        return UNKNOWN
    if lifetime_state == ENDED:
        return ENDED
    if lifetime_state not in (None, ALIVE, ENDED):
        return UNKNOWN
    if seat_state == ENDED:
        return ENDED
    if harness == "claude" and session_id and census is not None:
        return census.state(harness, session_id)
    return UNKNOWN
