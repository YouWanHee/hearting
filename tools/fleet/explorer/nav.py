"""Pure navigation projection for Fleet explorer snapshots."""

from collections import OrderedDict
from collections.abc import Mapping

from .types import Health, NavRow


def _get(value, *names, default=None):
    for name in names:
        candidate = value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)
        if candidate is not None and candidate != "":
            return candidate
    return default


def _items(value):
    return value or ()


def _role(value):
    """Use only the existing user-authored title/summary as role evidence."""
    title = _get(value, "title", "summary", "name", default="")
    return str(title).strip() or "역할 미확인"


def _section(identity, label):
    return NavRow(identity=f"section:{identity}", kind="section", depth=0,
                  label=label, target_ref=None)


def _health(value):
    state = _get(value, "liveness", "state", "status", default="unknown")
    return Health(state=str(state))


def build_nav(snapshot, focus_result=None, state=None):
    """Build the four stable nav sections without reading external state.

    Session and worker targets retain their original objects for the adapter's
    existing target constructors. Other rows are informational and untargetable.
    """
    sessions = list(_items(_get(snapshot, "sessions", default=())))
    jobs = list(_items(_get(snapshot, "jobs", default=())))
    rows = [_section("sessions", "세션")]

    projects = OrderedDict()
    for session in sessions:
        project = str(_get(session, "cwd", "project", default="프로젝트 미확인"))
        projects.setdefault(project, []).append(session)
    for project, project_sessions in projects.items():
        rows.append(NavRow(f"project:{project}", "project", 1, project,
                           target_ref=None))
        for session in project_sessions:
            sid = str(_get(session, "session_id", "sid", "id", default="unknown"))
            harness = str(_get(session, "harness", default="unknown"))
            label = _role(session)
            rows.append(NavRow(f"session:{harness}:{sid}", "session", 2,
                               label, (("harness", harness), ("sid", sid)),
                               _health(session), session))
            children = [job for job in jobs if
                        _get(job, "_parent_edge_sid", "parent_sid", "caller_sid") == sid]
            for job in children:
                jid = str(_get(job, "job_id", "attempt_id", "slug", "id", default="unknown"))
                rows.append(NavRow(f"worker:{harness}:{sid}:{jid}", "worker", 3,
                                   _role(job), (("sid", sid), ("job_id", jid)),
                                   _health(job), job))
            previous = _get(session, "previous_generation", "previous_sid")
            if previous is not None:
                previous_sid = (_get(previous, "session_id", "sid", "id")
                                if not isinstance(previous, str) else previous)
                rows.append(NavRow(f"previous-generation:{harness}:{sid}",
                                   "previous-generation", 3,
                                   str(previous_sid or "이전 세대 미확인"),
                                   (("sid", str(previous_sid or "")),), target_ref=None))

    tabs = tuple(_items(_get(focus_result, "tabs", default=())))
    rows.append(_section("todo", "남은 일"))
    for tab in tabs:
        identity = str(_get(tab, "identity", "cycle_id", "route_id", default="unknown"))
        rows.append(NavRow(f"request:{identity}", "request", 1,
                           str(_get(tab, "label", default="요청 미확인")),
                           (("route_id", _get(tab, "route_id")),
                            ("cycle_id", _get(tab, "cycle_id"))), target_ref=None))
        for ledger in _items(_get(focus_result, "ledger", default=())):
            route_node = str(_get(ledger, "route_node", default=""))
            if _get(ledger, "request_id", "cycle_id") not in (None, identity):
                continue
            if route_node:
                rows.append(NavRow(f"stage:{identity}:{route_node}", "stage", 2,
                                   route_node, target_ref=None))

    rows.append(_section("relations", "관계"))
    relations = _items(_get(snapshot, "relations", "relationship_rows", default=()))
    for index, relation in enumerate(relations):
        identity = str(_get(relation, "identity", "id", "ref", default=index))
        label = str(_get(relation, "label", "text", "kind", default="관계 근거 미확인"))
        rows.append(NavRow(f"relation:{identity}", "relation", 1, label,
                           target_ref=None))

    rows.append(_section("system", "시스템"))
    for resource in _items(_get(snapshot, "resources", default=())):
        identity = str(_get(resource, "identity", "id", "host", "name", default="unknown"))
        label = str(_get(resource, "label", "name", "host", default="시스템 자원 미확인"))
        rows.append(NavRow(f"system:{identity}", "system", 1, label,
                           health=_health(resource), target_ref=None))
    return rows
