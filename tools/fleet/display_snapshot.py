"""Display-only observations prepared once for a published Fleet snapshot.

These values label already-collected rows. They do not participate in identity,
selection, liveness, or control decisions.
"""
import os
import re
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class DisplaySnapshot:
    # A mapping proxy keeps this read-only across the basic/detail publication.
    project_by_cwd: dict

    def __deepcopy__(self, memo):
        return self

    def project_for(self, cwd, fallback):
        if isinstance(cwd, str) and cwd in self.project_by_cwd:
            return self.project_by_cwd[cwd]
        return fallback(cwd)


def path_label(cwd):
    """Path-only grouping fallback for injected/legacy rows missed by a snapshot."""
    if not isinstance(cwd, str) or not cwd:
        return "(unknown)"
    normalized = os.path.normpath(cwd)
    parts = [part for part in normalized.split(os.sep) if part]
    if not parts:
        return "(root)"
    for part in parts:
        match = re.match(r"^drill-(.+)-[^-/]+$", part)
        if match:
            return "drill:" + match.group(1)
    for part in parts:
        if len(part) > 3 and part.endswith("-wt"):
            return part[:-3]
    for part in parts:
        if len(part) > len("_worktrees") and part.endswith("_worktrees"):
            return part[:-len("_worktrees")]
    for index, part in enumerate(parts):
        if part == ".worktrees" and index:
            return parts[index - 1]
    base = re.sub(r"\.broken.*$", "", parts[-1])
    return base or "(root)"


def _observed_projects(paths, projects):
    from .model import project_of

    for cwd in paths:
        if not isinstance(cwd, str) or not cwd.startswith("/") or cwd in projects:
            continue
        try:
            projects[cwd] = project_of(cwd)
        except Exception:
            # An unavailable label stays on the pure path fallback; it never
            # changes the backing row or its ownership.
            continue


def build(sessions=(), jobs=(), resources=()):
    """Resolve labels for row and parent directories once per publication."""
    projects = {}
    paths = dict.fromkeys(
        cwd for row in (*sessions, *jobs, *resources)
        for cwd in (getattr(row, "cwd", None), getattr(row, "parent_cwd", None))
        if isinstance(cwd, str) and cwd.startswith("/")
    )
    _observed_projects(paths, projects)
    return DisplaySnapshot(MappingProxyType(projects))


def extend(snapshot, *, memory=None, paths=()):
    """Add detail-owned display paths without replacing basic observations."""
    projects = dict(snapshot.project_by_cwd) if isinstance(snapshot, DisplaySnapshot) else {}
    extra = list(paths)
    item = (memory or {}).get("periodic_curate") if isinstance(memory, dict) else None
    if isinstance(item, dict):
        extra.append(item.get("current_cwd"))
    _observed_projects(extra, projects)
    return DisplaySnapshot(MappingProxyType(projects))


def merge(basic, detail):
    """Merge immutable labels with the newer basic publication taking priority."""
    if not isinstance(basic, DisplaySnapshot):
        return detail
    if not isinstance(detail, DisplaySnapshot):
        return basic
    projects = dict(detail.project_by_cwd)
    projects.update(basic.project_by_cwd)
    return DisplaySnapshot(MappingProxyType(projects))
