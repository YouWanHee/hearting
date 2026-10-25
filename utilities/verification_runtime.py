"""Existing lab HTML verification contracts and their local execution intent.

Intent is derived from sealed node I/O, never a role-name permission exception.
No route, user config or live artifact is rewritten here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re


def lab_browser_node(route: dict, node_id: str | None) -> dict | None:
    node = next((n for n in route.get("nodes", ())
                 if isinstance(n, dict) and n.get("id") == node_id), None)
    if (route.get("capability") != "autopilot-lab" or node is None
            or node.get("completion_gate") != "lab-independent-verify"
            or node.get("kind") != "review-worker" or node.get("unit") != "qa/test"
            or node.get("commit_expected") is not False
            or not {"report/index.html", "report/report_manifest.json"}
                .issubset(node.get("inputs") or ())):
        return None
    return node


def runtime_requirements(route: dict, node_id: str) -> set[str]:
    node = next((n for n in route.get("nodes", ())
                 if isinstance(n, dict) and n.get("id") == node_id), {})
    requirements = set(node.get("runtime_requirements") or ())
    if lab_browser_node(route, node_id) is not None:
        requirements.add("loopback-listen")
    return requirements


def bound_lab_context(args, environ=None) -> dict | None:
    env = os.environ if environ is None else environ
    route_file = getattr(args, "route_file", None) or env.get("AGENT_ROUTE_FILE")
    node_id = getattr(args, "route_node", None) or env.get("AGENT_ROUTE_NODE")
    if not route_file or not node_id:
        return None
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if lab_browser_node(route, node_id) is None:
        return None
    for key, env_key in (("route_id", "AGENT_ROUTE_ID"), ("route_hash", "AGENT_ROUTE_HASH")):
        expected = getattr(args, key, None) or env.get(env_key)
        if expected and expected != route.get(key):
            return None
    worktree = getattr(args, "worktree", None) or env.get("AGENT_DISPATCH_WORKTREE")
    if worktree and Path(worktree).resolve() != Path(route.get("cwd", "")).resolve():
        return None
    return {"route": route, "node_id": node_id,
            "worktree": str(Path(worktree or route["cwd"]).resolve()),
            "artifact_root": str(Path(route["artifact_root"]).resolve())}


def supported_requirements(route: dict, node_id: str, harness: str | None) -> set[str]:
    if lab_browser_node(route, node_id) is None:
        return set()
    if harness == "codex":
        from codex_permission_profile import local_verification_available
        return {"loopback-listen"} if local_verification_available() else set()
    if harness in {"claude", "opencode"}:
        # Their checked verification commands use the shared sandbox runner;
        # do not claim that a broad native network switch enforces this scope.
        from codex_permission_profile import local_verification_available
        return {"loopback-listen"} if local_verification_available() else set()
    return set()


def review_scope(route, node_id, route_file, environ=None, parent_attempt_id=None):
    """Resolve existing producer ownership before deriving writable directories."""
    env = os.environ if environ is None else environ
    from worker_bootstrap import resolve_node_scope
    scope = resolve_node_scope(route, node_id, env,
        parent_attempt_id=parent_attempt_id or env.get("AGENT_DISPATCH_PARENT_ATTEMPT_ID"),
        route_file=route_file)
    if not scope.output_dir:
        raise ValueError("canonical-review-scope-unbound")
    base = Path(scope.output_dir).resolve()
    artifact = Path(route["artifact_root"]).resolve()
    if base != artifact and artifact not in base.parents:
        raise ValueError("canonical-review-scope-outside-artifact-root")

    def path_for(pattern, *, parent=False):
        value = Path(pattern)
        if value.is_absolute() or ".." in value.parts or "\\" in pattern:
            raise ValueError("canonical-review-scope-invalid")
        parts = []
        wildcard = False
        for part in value.parts:
            if any(c in part for c in "*?["):
                wildcard = True
                break
            parts.append(part)
        if parent and not wildcard:
            parts = parts[:-1]
        if not parts:
            raise ValueError("canonical-review-scope-too-broad")
        result = base.joinpath(*parts)
        current = base
        for part in parts:
            current /= part
            if current.is_symlink():
                raise ValueError("canonical-review-scope-symlink")
        if base not in result.resolve().parents:
            raise ValueError("canonical-review-scope-outside-cycle")
        return result

    own = lab_browser_node(route, node_id)
    writes = sorted({path_for(p, parent=True) for p in (*scope.write_scope, *scope.outputs)})
    if not writes:
        raise ValueError("canonical-review-scope-empty")
    group_dirs = set(writes)
    readonly = set()
    for sibling in route.get("nodes", ()):
        if (sibling.get("id") == node_id or not own.get("parallel_group")
                or sibling.get("parallel_group") != own.get("parallel_group")):
            continue
        for pattern in (*sibling.get("write_scope", ()), *sibling.get("outputs", ())):
            directory = path_for(pattern, parent=True)
            group_dirs.add(directory)
            target = path_for(pattern)
            if any(parent in target.parents for parent in writes):
                readonly.add(target)
    return {"output_dir": str(base), "write_roots": [str(p) for p in writes],
            "group_dirs": [str(p) for p in sorted(group_dirs)],
            "read_only_roots": [str(p) for p in sorted(readonly)]}


def prepare_review_directories(scope):
    # Only declared review parents are materialized; existing payloads and
    # verdict bytes are never rewritten by runtime preparation.
    base = Path(scope["output_dir"]).resolve()
    for raw in scope["group_dirs"]:
        path = Path(raw)
        if base not in path.resolve().parents:
            raise ValueError("canonical-review-scope-outside-cycle")
        current = base
        for part in path.relative_to(base).parts:
            current /= part
            if current.is_symlink():
                raise ValueError("canonical-review-scope-symlink")
        path.mkdir(parents=True, exist_ok=True)


def scoped_write_roots(scope, supplied, environ=None):
    """Keep declared review parents and the caller's existing runtime grants."""
    env = os.environ if environ is None else environ
    available = [Path(root).resolve() for root in supplied]
    result = {Path(root) for root in scope["write_roots"]}
    candidates = []
    if env.get("AGENT_RUNTIME_DIR"):
        candidates.append(Path(env["AGENT_RUNTIME_DIR"]).resolve())
    if env.get("AGENT_DISPATCH_JOBS"):
        candidates.append(Path(env["AGENT_DISPATCH_JOBS"]).resolve().parent)
    for candidate in candidates:
        if any(candidate == root or root in candidate.parents for root in available):
            result.add(candidate)
    from harness_state_roots import spec_grounding_dir
    marker = (spec_grounding_dir(None, env).resolve()
              if any(env.get(key) for key in ("HARNESS_STATE_ROOT", "XDG_STATE_HOME", "HOME")) else None)
    result.update(root for root in available
                  if root.name in {".core-grounding", ".spec-grounding"} or root == marker)
    return [str(p) for p in sorted(result)]


def headless_browser_executable(environ=None) -> str | None:
    env = os.environ if environ is None else environ
    configured = env.get("PLAYWRIGHT_BROWSERS_PATH")
    roots = [Path(configured).expanduser()] if configured and configured != "0" else []
    roots.append(Path(env.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "ms-playwright")
    standard = Path.home() / ".cache" / "ms-playwright"
    if standard not in roots:
        roots.append(standard)
    for root in roots:
        candidates = []
        for pattern in ("chromium_headless_shell-*/*/headless_shell",
                        "chromium_headless_shell-*/*/chrome-headless-shell"):
            for path in root.glob(pattern):
                if path.is_file() and os.access(path, os.X_OK):
                    version = tuple(int(x) for x in re.findall(r"\d+", path.parent.parent.name))
                    candidates.append((version, str(path.resolve())))
        if candidates:
            return max(candidates)[1]
    return None


def assignment_hint(route: dict, node_id: str) -> str:
    if lab_browser_node(route, node_id) is None:
        return ""
    browser = headless_browser_executable()
    launch = (f"Use the existing Playwright headless shell at {browser} "
              "(launch executable_path/executablePath) with a temporary profile. "
              if browser else "Use the installed Playwright headless shell with a temporary profile. ")
    return ("Local verification: source is read-only; the declared canonical review output is writable. "
            "Save that verdict locally before any optional artifact sink. "
            "loopback-listen permits localhost only, with outbound denied. "
            "Keep the local server and browser in one verification command. "
            + launch + "Use the existing verification runner for local checks: python3 "
            + str(Path(__file__).with_name("verification-background-lease.py"))
            + " --timeout <seconds> -- <command>. Keep broad Unix-socket access disabled.\n\n")


def scratch_environment(environ=None):
    """Use the already assigned runtime directory for browser scratch files."""
    env = dict(os.environ if environ is None else environ)
    raw = env.get("AGENT_RUNTIME_DIR")
    if raw:
        root = Path(raw).resolve() / "local-verification"
        for key, leaf in (("TMPDIR", "tmp"), ("XDG_CACHE_HOME", "cache"),
                          ("XDG_CONFIG_HOME", "config"), ("XDG_RUNTIME_DIR", "run")):
            path = root / leaf
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            env[key] = str(path)
    return env


def effective_scope(args, supplied, harness, environ):
    """Describe the concrete projection in the existing attempt grant record."""
    bound = bound_lab_context(args, environ)
    if bound is None:
        return None
    scope = review_scope(bound["route"], bound["node_id"], args.route_file, environ)
    prepare_review_directories(scope)
    return {"network": "loopback-only", "route_node": bound["node_id"],
            "route_id": bound["route"].get("route_id"), "route_hash": bound["route"].get("route_hash"),
            "projection": "native-profile" if harness == "codex" else "verification-command",
            "write_roots": scoped_write_roots(scope, supplied, environ),
            "read_roots": [bound["worktree"], bound["artifact_root"], *scope["read_only_roots"]]}


def verification_command(command, environ=None):
    """Reuse the native command sandbox for the existing bounded runner.

    Claude/OpenCode tool permissions are not an OS network boundary. Their
    local verification command uses the same checked sandbox as Codex, without
    a model call, new worker, user config change or Unix-socket escape.
    """
    from types import SimpleNamespace
    env = dict(os.environ if environ is None else environ)
    bound = bound_lab_context(SimpleNamespace(), env)
    if bound is None:
        return command, env
    from execution_access import AccessContext, load_parent_effective_grant
    jobs = Path(env["AGENT_DISPATCH_JOBS"]).resolve()
    context = AccessContext.build(worktree=bound["worktree"], artifact_root=bound["artifact_root"],
        dispatch_state_root=jobs.parent, agent_home=Path(__file__).resolve().parent.parent, environ=env)
    grant = load_parent_effective_grant(jobs=jobs,
        parent_attempt_id=env["AGENT_DISPATCH_ATTEMPT_ID"], context=context)
    scope = grant.verification_scope
    if (not scope or scope.get("route_node") != bound["node_id"]
            or scope.get("route_id") != bound["route"].get("route_id")
            or scope.get("route_hash") != bound["route"].get("route_hash")):
        raise ValueError("loopback-only-unsupported")
    env = scratch_environment(env)
    if (grant.runtime in {"codex-exec", "codex-app-server"}
            and scope.get("projection") == "native-profile"):
        return command, env  # Already inside that exact attempt's native profile.
    from harness_state_roots import spec_grounding_dir
    marker = spec_grounding_dir(None, env).resolve()
    if marker in grant.writable_roots:
        marker.mkdir(parents=True, exist_ok=True)
    from codex_permission_profile import (verification_profile_config, config_arguments,
                                          LOCAL_VERIFICATION_PROFILE_NAME)
    resolved = review_scope(bound["route"], bound["node_id"], env["AGENT_ROUTE_FILE"], env)
    prepare_review_directories(resolved)
    profile = verification_profile_config(bound["worktree"], list(grant.writable_roots),
        env["AGENT_ROUTE_FILE"], bound["node_id"], environ=env)
    if profile is None:
        raise ValueError("loopback-only-unsupported")
    return ["codex", "sandbox", "--include-managed-config", "-C", os.getcwd(),
            "-P", LOCAL_VERIFICATION_PROFILE_NAME, *config_arguments(profile), "--", *command], env
