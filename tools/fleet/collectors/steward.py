"""F-100c — steward (depth −1) flag projection, read-only.

The ledger tool (`utilities/peer-message.py`) keeps one marker per session under
`<dispatch-state-root>/peer-steward/<harness>/<sid>.json`. Since 2026-09-06 the flag
is a ROLE: a marker entry is evidence only when its `source` is `explicit`
(`peer-steward.py steward on`), `watch` (`peer-steward.py wait`/`watch` observed a
real target) or `start` (`peer-steward.py start` launched the target). No `record`
path raises it — not a steer/handoff/gate-relay send and not a SendMessage with
`notify_when_idle` (recorded as `kind=watch`) — under the old rule every worker that
handed off to its steward wore the steward tag. This collector joins markers onto live sessions by exact
(harness, session_id) and asks the ledger tool's `steward_evidence_targets` — the
one definition of the rule — which entries count; a marker with none (an old
handoff-only leftover) is treated as absent, so `steward_targets` holds evidence
entries only. Nothing here writes, and a missing or unreadable marker root (or an
unavailable ledger module) leaves every session's default (`steward=False`).
Only targets whose existing session metadata confirms the same repository grant
the displayed role. Foreign and unknown targets remain communication only.
"""
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace


def _peer_message_module():
    from . import peer_messages
    return peer_messages._load_peer_message()


def read_markers():
    """``{(harness, session_id): marker}`` over every ledger root the board reads (the
    F-98d resolver chain plus each installed runtime's own root); empty on any failure."""
    mod = _peer_message_module()
    if mod is None:
        return {}
    try:
        from . import peer_messages as _pm
        roots = _pm._state_roots()
    except Exception:
        roots = None
    try:
        return mod.read_steward_markers(roots or None) or {}
    except Exception:
        return {}


def _session_keys(sess):
    """The shared join-key rule — a marker written before a resume still names the id the
    session used to have, so an exact-only join silently drops the relation."""
    from .. import session_registry
    keys = session_registry.session_join_keys(sess)
    harness = str(getattr(sess, "harness", "") or "").lower()
    for sid in getattr(sess, "_gpu_session_aliases", ()) or ():
        if sid and (harness, sid) not in keys:
            keys.append((harness, sid))
    return keys


def _repository_key(cwd):
    from ..gitinfo import resolve_gitdir
    try:
        return resolve_gitdir(cwd)[1] if cwd else None
    except (OSError, ValueError, TypeError):
        return None


def _registry_sessions():
    """Reuse native session metadata; no process, herdr or role probe."""
    from .. import session_registry
    rows = []
    for harness in ("claude", "codex", "opencode"):
        try:
            entries = os.listdir(session_registry._dir_for(harness))
        except OSError:
            continue
        for entry in entries:
            if not entry.endswith(".json") or not entry[:-5].isdigit():
                continue
            record = session_registry.read(harness, int(entry[:-5]))
            if record and record.get("sessionId") and record.get("cwd"):
                rows.append(SimpleNamespace(harness=harness, session_id=record["sessionId"],
                                            cwd=record["cwd"], runtime_name=record.get("name")))
    return rows


def _projection_sessions():
    """Read the same native/herdr metadata as Fleet, without a process/role probe."""
    from . import herdr
    rows = []
    for agent in herdr.list_agents() or ():
        identity = agent.get("agent_session") or {}
        sid = identity.get("value") if isinstance(identity, dict) else None
        harness = str(agent.get("agent") or "").lower()
        cwd = agent.get("cwd")
        pane = agent.get("pane_id")
        if not sid:
            native = herdr.pane_session_metadata(harness, pane, cwd)
            sid = native.get("sid") if native else None
        if harness and cwd:
            rows.append(SimpleNamespace(harness=harness, session_id=sid, cwd=cwd,
                                        _herdr_name=agent.get("name"),
                                        session_aliases=herdr.pane_session_aliases(harness, sid, pane, cwd),
                                        _gpu_session_aliases=herdr._clear_gpu_session_aliases(harness, sid, pane)))
    current_keys = {key for row in rows for key in _session_keys(row)}
    return rows + [row for row in _registry_sessions()
                   if not current_keys.intersection(_session_keys(row))]


def _native_role_sessions(markers, rows):
    """Exact native directory metadata for evidenced actors/targets absent from the snapshot.

    An OpenCode pane can omit its SID, and old process registry files can be
    absent. Read only named actors and evidenced targets from the native metadata tables, never
    conversation bodies, process state, or an inferred same-cwd identity.
    """
    from . import codex, opencode
    present = {key for row in rows for key in _session_keys(row)}
    wanted = set(markers)
    mod = _peer_message_module()
    evidence = getattr(mod, "steward_evidence_targets", None) if mod is not None else None
    for marker in markers.values():
        try:
            targets = evidence(marker) if evidence is not None else ()
        except Exception:
            continue
        for target in targets:
            harness = str(target.get("harness") or "").lower()
            sid = target.get("session_id")
            if sid:
                wanted.add((harness, sid))
    out = []
    for harness, db, table, column in (
            ("codex", codex._state_db(codex._home()), "threads", "cwd"),
            ("opencode", opencode._db(), "session", "directory")):
        ids = sorted({sid for h, sid in wanted if h == harness and sid
                      and (h, sid) not in present})
        if not ids or not db:
            continue
        try:
            with closing(sqlite3.connect(Path(db).absolute().as_uri() + "?mode=ro",
                                         uri=True, timeout=0.2)) as con:
                for sid in ids:
                    result = con.execute("SELECT %s FROM %s WHERE id=?" % (column, table),
                                         (sid,)).fetchone()
                    if result and isinstance(result[0], str) and result[0]:
                        out.append(SimpleNamespace(harness=harness, session_id=sid, cwd=result[0]))
        except (OSError, sqlite3.Error):
            continue
    return out


def _role_winners(markers, rows, evidence):
    """Newest evidenced same-repo supervisor per canonical target; no marker mutation."""
    from .peer_messages import _parse_ts
    by_key, by_name, repos = {}, {}, {}
    for row in rows:
        repos[id(row)] = _repository_key(getattr(row, "cwd", None))
        for key in _session_keys(row):
            by_key.setdefault(key, []).append(row)
        for name in {getattr(row, "runtime_name", None), getattr(row, "_herdr_name", None)} - {None, ""}:
            by_name.setdefault((row.harness, name), []).append(row)

    def one(candidates):
        if any(not row.session_id for row in candidates):
            return None
        identities = {(row.harness, row.session_id) for row in candidates}
        repositories = {repos[id(row)] for row in candidates}
        if len(identities) != 1 or len(repositories) != 1 or None in repositories:
            return None
        return next(iter(identities)), next(iter(repositories))

    claims = {}
    for parent_key, marker in markers.items():
        parent = one(by_key.get(parent_key, ()))
        if parent is None:
            continue
        try:
            targets = evidence(marker)
        except Exception:
            continue
        for target in targets:
            harness = str(target.get("harness") or "").lower()
            sid = target.get("session_id")
            candidates = by_key.get((harness, sid), ()) if sid else by_name.get((harness, target.get("name")), ())
            child = one(candidates)
            if child is None or child[1] != parent[1]:
                continue
            raw_ts = str(target.get("ts") or "")
            instant = _parse_ts(raw_ts)
            instant = instant if instant is not None else float("-inf")
            rank = (instant, *parent[0])
            owners = claims.setdefault(child[0], {})
            previous = owners.get(parent[0])
            first = (instant, raw_ts)
            order = min(first, previous[3]) if previous else first
            if previous is None or rank > previous[0]:
                owners[parent[0]] = (rank, parent[0], dict(target, session_id=child[0][1]), order)
            else:
                owners[parent[0]] = (*previous[:3], order)
    winners = {child: max(owners.values(), key=lambda claim: claim[0])
               for child, owners in claims.items()}
    # Decide peers after the existing handover: only an actor with a winning
    # same-repository target is a supervisor. Relations between those actors
    # are communication, not another layer of supervision. Keep marker bytes.
    supervisors = {claim[1] for claim in winners.values()}
    return {child: claim for child, claim in winners.items() if child not in supervisors}


def _owner_targets(winners, harness, session_id):
    targets = [claim for claim in winners.values() if claim[1] == (harness, session_id)]
    return [entry for _rank, _parent, entry, _order in sorted(targets, key=lambda claim: claim[3])]


def role_targets(harness, session_id, *, aliases=(), markers=None, cwd=None, sessions=None,
                 _winners=None):
    """One same-repository role lookup for Fleet and herdr, with proven prior ids.

    Current snapshot rows override registry history for their exact join keys.
    A name-only marker can join an unambiguous existing native/herdr name; it
    never guesses a repository from a project basename or a display tag.
    """
    if markers is None:
        markers = read_markers()
    mod = _peer_message_module()
    evidence = getattr(mod, "steward_evidence_targets", None) if mod is not None else None
    if evidence is None:
        return []
    harness = str(harness or "").lower()
    if not any(markers.get((harness, sid)) for sid in [session_id, *aliases] if sid):
        return []
    if _winners is not None:
        return _owner_targets(_winners, harness, session_id)
    rows = _projection_sessions() if sessions is None else list(sessions)
    own_keys = {(harness, sid) for sid in [session_id, *aliases] if sid}
    if cwd is None:
        own_cwds = {row.cwd for row in rows if own_keys.intersection(_session_keys(row))}
        cwd = own_cwds.pop() if len(own_cwds) == 1 else None
    repository = _repository_key(cwd)
    if not repository:
        return []
    own_rows = [row for row in rows if own_keys.intersection(_session_keys(row))]
    own_names = {getattr(row, "_herdr_name", None) for row in own_rows} - {None, ""}
    runtime_names = {getattr(row, "runtime_name", None) for row in own_rows} - {None, ""}
    own = SimpleNamespace(harness=harness, session_id=session_id, cwd=cwd,
                          session_aliases=list(aliases),
                          _herdr_name=next(iter(own_names)) if len(own_names) == 1 else None,
                          runtime_name=next(iter(runtime_names)) if len(runtime_names) == 1 else None)
    rows = [own] + [row for row in rows if not own_keys.intersection(_session_keys(row))]
    if sessions is None:
        rows += _native_role_sessions(markers, rows)
    winners = _role_winners(markers, rows, evidence)
    return _owner_targets(winners, harness, session_id)


def enrich(sessions, markers=None):
    for s in sessions:
        s.steward = False
        s.steward_targets = s.steward_parents = None
    if markers is None:
        markers = read_markers()
    if not markers:
        return
    # Reuse registry history for ended targets, but never overrule current rows.
    current_keys = {key for s in sessions for key in _session_keys(s)}
    context = list(sessions) + [s for s in _registry_sessions()
                                if not current_keys.intersection(_session_keys(s))]
    context += _native_role_sessions(markers, context)
    mod = _peer_message_module()
    evidence = getattr(mod, "steward_evidence_targets", None) if mod is not None else None
    if evidence is None:
        return
    winners = _role_winners(markers, context, evidence)
    # target key → the stewards that named it. Built from the SAME evidence entries the
    # forward projection uses, so "who watches me" can never claim a relation that the
    # steward's own row does not also show.
    parents_by_key = {}
    for s in sessions:
        keys = _session_keys(s)
        targets = role_targets(s.harness, s.session_id, markers=markers,
                               aliases=[sid for _harness, sid in keys[1:]],
                               cwd=getattr(s, "cwd", None), sessions=context, _winners=winners)
        if not targets:
            continue
        s.steward = True
        # `steward_evidence_targets` returns oldest-first — the stable order the
        # renderer's front-preserving +N fold relies on.
        s.steward_targets = list(targets)
        for target in targets:
            target_sid = target.get("session_id")
            if not target_sid:
                continue
            parent = {"harness": str(getattr(s, "harness", "") or "").lower(),
                      "session_id": getattr(s, "session_id", None),
                      "name": getattr(s, "runtime_name", None) or getattr(s, "slug", None),
                      "source": target.get("source"), "ts": target.get("ts")}
            key = (str(target.get("harness") or "").lower(), target_sid)
            parents_by_key.setdefault(key, []).append(parent)
    if not parents_by_key:
        return
    for s in sessions:
        parents = []
        seen = set()
        for key in _session_keys(s):
            for parent in parents_by_key.get(key, ()):
                identity = (parent["harness"], parent["session_id"])
                if identity in seen or identity == (str(getattr(s, "harness", "") or "").lower(),
                                                    getattr(s, "session_id", None)):
                    continue      # a session is never its own supervisor
                seen.add(identity)
                parents.append(parent)
        if parents:
            s.steward_parents = parents
