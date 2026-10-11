"""User vocabulary shared by Fleet's group and process views; no state judgments."""
from .model import project_of


def label(value):
    return str(value) if value else ""


def project(cwd):
    return project_of(cwd) if cwd else "project unknown"


def gpu_owner(label):
    """Execution IDs repeat the run identity; person/session labels add context."""
    return "" if str(label).startswith(("run:", "job:")) else label
