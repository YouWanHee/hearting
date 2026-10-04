"""SD-162 read-only observations, never launch or permission authority."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import gpu_execution_sandbox as GPU_SANDBOX

RECEIPT_KEY = "owner_write_advisory="
CODE = "codex-owner-write-constraints"


def worktree_mutating_scope(scope):
    """Shared route/guard predicate; artifact outputs alone are not mutations."""
    if scope in ("target-artifact", "source-scoped"):
        return True
    root = scope[:-3] if str(scope).endswith("/**") else scope
    return root == "source"


def git_topology(cwd):
    if not cwd:
        return "unknown"
    try:
        root = Path(cwd).resolve()
        values = []
        for flag in ("--git-dir", "--git-common-dir"):
            result = subprocess.run(["git", "-C", str(root), "rev-parse", flag],
                                    text=True, capture_output=True, check=True, timeout=2)
            value = Path(result.stdout.strip())
            values.append(value.resolve() if value.is_absolute() else (root / value).resolve())
        return "primary" if values[0] == values[1] else "linked"
    except (OSError, ValueError, subprocess.SubprocessError):
        return "unknown"


def advisories(route, *, owner_harness=None, sandbox=None, git_writable_roots=(),
               explicit_writable_roots=(), gpu_selection=None):
    """Prospective selection or actual wrapper facts, outside sealed route bytes."""
    if route.get("owner_dispatch_depth") == 0 or route.get("effective_intensity") == "direct":
        return []
    gpu_rows = GPU_SANDBOX.advisory(route, owner_harness=owner_harness,
                                   selection=gpu_selection, applied=sandbox is not None)
    applied = sandbox is not None
    if gpu_rows and sandbox is None:
        sandbox = gpu_rows[0]["sandbox"]
    if not explicit_writable_roots and not any(
            worktree_mutating_scope(scope) for node in route.get("nodes", [])
            for scope in (node.get("write_scope") or [])):
        return gpu_rows
    owner = owner_harness or (route.get("work_request") or {}).get("owner_harness") or "auto"
    if owner not in {"auto", "codex"}:
        return []
    topology = git_topology(route.get("cwd"))
    prefix = "Codex owner: " if owner == "codex" else "자동 선택에서 Codex owner의 workspace-write가 선택되면: "
    if gpu_rows and owner == "auto":
        prefix = "자동 선택에서 Codex owner가 선택되면: "
    if sandbox == "read-only":
        message = prefix + "현재 sandbox는 read-only이며 source 쓰기를 허용하지 않습니다."
    elif sandbox == "danger-full-access":
        message = prefix + ("현재" if applied else "선택된") + " sandbox는 danger-full-access입니다. workspace-write의 경로 제한은 적용되지 않습니다."
    else:
        message = prefix + ("현재 workspace-write는" if applied else "workspace-write는")
        message += " source 편집을 허용해도 Git 메타데이터와 workspace 밖 경로는 별도 제약을 받습니다."
        if topology == "primary":
            message += (" primary checkout의 .git는 커밋에 필요한 부분만 쓰기가 허용되고 config·hooks는 읽기 전용입니다."
                        " 이 Codex가 named permission profile을 지원하지 않으면 기존처럼 .git 전체가 보호됩니다.")
        elif topology == "linked":
            message += " linked worktree owner에는 기존의 좁은 Git 메타데이터 grant가 있으며 common .git 전체 권한은 아닙니다."
        else:
            message += " Git 작업 폴더 유형은 확인되지 않았습니다."
    if explicit_writable_roots:
        message += " 명시된 쓰기 요청 경로: " + ", ".join(str(p) for p in explicit_writable_roots) + "."
    message += " 실제 쓰기 성공을 보증하지는 않습니다."
    return [{"code": CODE, "phase": "applied" if applied else "prospective",
             "owner_harness": owner, "sandbox": sandbox or "workspace-write-if-selected",
             "git_topology": topology, "git_writable_roots": [str(p) for p in git_writable_roots],
             "explicit_writable_roots": [str(p) for p in explicit_writable_roots],
             "message": message}] + gpu_rows


def receipt_advisories(receipt):
    """Promote wrapper observations from captured stdout; no control decisions."""
    result = []
    for line in receipt.splitlines():
        if not line.startswith(RECEIPT_KEY):
            continue
        try:
            value = json.loads(line[len(RECEIPT_KEY):])
        except (ValueError, TypeError):
            continue
        if (isinstance(value, dict) and value.get("code") in {CODE, GPU_SANDBOX.RECEIPT_CODE}
                and isinstance(value.get("message"), str)):
            if value not in result:
                result.append(value)
    return result
