"""Prepare old row-only fixtures at the collection boundary, before rendering."""
import time

from fleet import projection, render
from fleet.collectors import dispatch, resolve_parent_edges
from fleet.refresh import LiveSnapshot


def observed_fixture(sessions, jobs, resources=None, node_evidence=None):
    evidence = dispatch.collect.last_route_nodes if node_evidence is None else node_evidence
    snapshot = LiveSnapshot(sessions=list(sessions), jobs=list(jobs),
                            resources=list(resources or []), node_evidence=evidence or {})
    resolve_parent_edges(snapshot.sessions, [job for job in snapshot.jobs
                         if (job.parent_managed_dir or getattr(job, "_registry_metadata", None))
                         and not hasattr(job, "_parent_edge_promoted_orphan")])
    if jobs and all(row.work_projection is None for row in snapshot.sessions + snapshot.jobs):
        projection.attach_projections(snapshot.sessions, snapshot.jobs,
                                      node_evidence=snapshot.node_evidence,
                                      degradations=dispatch.collect.last_degradations,
                                      resources=snapshot.resources, now=time.time())
    if not sessions and not jobs:
        snapshot.route_entities = projection.terminal_route_entities(snapshot.node_evidence)
    return snapshot


def build_observed_lines(sessions, jobs, *args, **kwargs):
    snapshot = observed_fixture(sessions, jobs, kwargs.get("resources"), kwargs.get("node_evidence"))
    kwargs.update(node_evidence=snapshot.node_evidence, route_entities=snapshot.route_entities)
    return render._build_lines(sessions, jobs, *args, **kwargs)


def draw_observed(stdscr, sessions, jobs, *args, **kwargs):
    kwargs["snapshot"] = observed_fixture(sessions, jobs, kwargs.get("resources"))
    return render._draw(stdscr, sessions, jobs, *args, **kwargs)
