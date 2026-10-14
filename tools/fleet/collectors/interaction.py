"""Neutral Fleet interaction-wait enrichment (read-only, fail open)."""

from pathlib import Path

from .. import interaction


def enrich(sess, now=None):
    session_id = getattr(sess, "session_id", None)
    harness = getattr(sess, "harness", None)
    if not session_id or not harness:
        return
    record = interaction.pending_wait(
        session_id,
        harness,
        session_start=getattr(sess, "started_at", None),
        activity_since=getattr(sess, "_interaction_activity", None),
        now=now,
    )
    if not record:
        return
    candidate = {
        "kind": record["kind"],
        "source": record["source"],
        "waiting_since": record["waiting_since"],
    }
    existing = getattr(sess, "interaction_state", None)
    priorities = {"codex-appserver": 2, "codex-rollout": 1}
    if isinstance(existing, dict):
        existing_priority = priorities.get(existing.get("source"), 0)
        candidate_priority = priorities.get(candidate.get("source"), 0)
        if existing_priority > candidate_priority:
            return
        if (
            existing_priority == candidate_priority
            and isinstance(existing.get("waiting_since"), (int, float))
            and existing["waiting_since"] > candidate["waiting_since"]
        ):
            return
    sess.interaction_state = candidate


def enrich_human_gates(sessions, *, jobs_path=None, now=None):
    """Attach exact session-ledger observations without replacing native evidence."""
    import sys
    from pathlib import Path
    from .. import route, route_chain
    utilities = str(Path(__file__).resolve().parents[3] / "utilities")
    if utilities not in sys.path:
        sys.path.insert(0, utilities)
    import human_gate_observation
    from . import dispatch

    registries = dispatch.human_gate_registry_paths(jobs_path)
    for sess in sessions:
        sess.interaction_observations = []
        if getattr(sess, "is_child", False) or getattr(sess, "app_server", False):
            continue
        try:
            from .. import session_registry
            join_keys = set(session_registry.session_join_keys(sess))
            lines = route_chain.session_lines(sess, full_history=True)
        except (ImportError, OSError, ValueError):
            continue
        by_gate = {}
        for line in lines:
            if (line.get("event") not in route_chain.COMPOSING_EVENTS
                    or (line.get("harness"), line.get("session_id")) not in join_keys):
                continue
            record = route.load(line.get("route_file"), line.get("route_hash"), line.get("route_id"))
            if record is None:
                continue
            matches = []
            for registry in registries:
                waits = human_gate_observation.pending_human_waits(
                    record, jobs=registry, outcome=route.load_outcome(
                        line.get("route_file"), line.get("route_id"), line.get("route_hash")))
                if waits:
                    matches.append((registry, waits))
            # Split registries are not inferred from a cwd or duplicate route. Multiple
            # exact journals for this route are ambiguous and stay out of the card.
            if len(matches) != 1:
                continue
            _registry, waits = matches[0]
            for wait in waits:
                key = (wait["route_id"], wait["gate_id"])
                current = by_gate.get(key)
                observation = {name: wait[name] for name in ("kind", "source", "waiting_since")}
                if current is None or observation["waiting_since"] < current["waiting_since"]:
                    by_gate[key] = observation
        sess.interaction_observations = sorted(by_gate.values(), key=lambda row: row["waiting_since"])
