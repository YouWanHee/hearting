#!/usr/bin/env python3
"""Read-only-by-default execution access failure diagnosis."""

from __future__ import annotations

import argparse
import errno as errno_module
import json
import os
from pathlib import Path
import socket
import sys
import uuid
from typing import Iterable, Mapping

from execution_access import AccessContext, ExecutionAccessError, load_request


CLASSIFICATIONS = frozenset(
    {
        "sandbox-network-disabled",
        "dns-resolution-failed",
        "tcp-connect-blocked",
        "remote-auth-rejected",
        "mount-read-only",
        "unix-permission-denied",
        "launcher-runtime-state-unwritable",
    }
)

_TCP_ERRNOS = {"ECONNREFUSED", "ETIMEDOUT", "EPERM", "ENETUNREACH", "EHOSTUNREACH"}
_AUTH_STATUSES = {401, 403}
_ONE_CHANGE = {
    "launcher-runtime-state-unwritable": "Make the launcher's exact private state directory writable at the parent launch boundary.",
    "mount-read-only": "Change the mount or outer sandbox grant for the exact failed path.",
    "unix-permission-denied": "Change the owner or mode of the exact failed path, not the sandbox policy.",
    "remote-auth-rejected": "Repair the remote credential or account authorization without changing sandbox grants.",
    "dns-resolution-failed": "Repair DNS resolution at the parent launch network boundary.",
    "tcp-connect-blocked": "Allow the exact destination at the parent network or firewall boundary.",
    "sandbox-network-disabled": "Restart from the top-level launch with an explicit network request.",
}


class DiagnosisError(ValueError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or reason


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _errno_name(value: object) -> str:
    if isinstance(value, int):
        return errno_module.errorcode.get(value, str(value))
    if isinstance(value, str):
        return value.upper()
    return ""


def diagnose(
    evidence: Mapping[str, object], *, launcher_state_roots: Iterable[str | Path] = ()
) -> dict[str, object]:
    """Classify prepared evidence without performing a probe or echoing errors."""

    path_value = evidence.get("path")
    path = Path(path_value) if isinstance(path_value, str) and path_value.startswith("/") else None
    private_roots = [Path(root) for root in launcher_state_roots]
    home = evidence.get("home")
    if isinstance(home, str) and home.startswith("/"):
        private_roots.append(Path(home) / ".codex" / ".harness")
    err = _errno_name(evidence.get("errno"))
    phase = str(evidence.get("phase") or "").lower()
    state_write_failed = (
        phase in {"write", "file", "filesystem"}
        and (err in {"EROFS", "EACCES", "EPERM"} or evidence.get("write_failed") is True)
    )
    if (
        path is not None
        and state_write_failed
        and any(_within(path, root) for root in private_roots)
    ):
        classification = "launcher-runtime-state-unwritable"
    else:
        status = evidence.get("http_status")
        if status in _AUTH_STATUSES or evidence.get("remote_auth_rejected") is True:
            classification = "remote-auth-rejected"
        elif (
            evidence.get("network_all_hosts_failed") is True
            and evidence.get("sandbox_network_enabled") is False
        ):
            classification = "sandbox-network-disabled"
        elif err.startswith("EAI_") or evidence.get("dns_failed") is True:
            classification = "dns-resolution-failed"
        elif phase in {"connect", "tcp"} and err in _TCP_ERRNOS:
            classification = "tcp-connect-blocked"
        elif err == "EROFS" or (
            evidence.get("read_succeeded") is True
            and evidence.get("write_failed") is True
            and evidence.get("mount_writable") is False
        ):
            classification = "mount-read-only"
        elif (
            phase in {"write", "file", "filesystem"}
            and err in {"EACCES", "EPERM"}
            and evidence.get("mount_writable") is True
        ):
            classification = "unix-permission-denied"
        else:
            raise DiagnosisError(
                "diagnosis-evidence-insufficient",
                "prepared evidence does not prove exactly one access-failure class",
            )

    summary: dict[str, object] = {
        "classification": classification,
        "evidence": {
            "errno": _errno_name(evidence.get("errno")) or None,
            "exit_code": evidence.get("exit_code")
            if isinstance(evidence.get("exit_code"), int)
            else None,
            "http_status": evidence.get("http_status")
            if evidence.get("http_status") in _AUTH_STATUSES
            else None,
            "phase": str(evidence.get("phase"))[:32]
            if isinstance(evidence.get("phase"), str)
            else None,
            "path": str(path) if path is not None else None,
        },
        "one_change": _ONE_CHANGE[classification],
    }
    # Raw stderr, URLs, authorization headers, tokens, keys, and passwords are
    # intentionally absent from the output schema.
    return summary


def _registry_receipt(jobs: Path, attempt_id: str) -> dict[str, object]:
    found: dict[str, str] | None = None
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise DiagnosisError("diagnosis-evidence-insufficient", "jobs registry unreadable") from exc
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = {}
        for item in fields[5].split(","):
            key, separator, value = item.partition("=")
            if separator:
                metadata[key] = value
        if metadata.get("attempt_id") == attempt_id:
            found = {
                key: value
                for key, value in metadata.items()
                if key.startswith("execution_access_")
            }
    if found is None:
        raise DiagnosisError("diagnosis-evidence-insufficient", "attempt receipt not found")
    network_state = found.get("execution_access_network")
    return {
        # A request being absent is not evidence that connectivity failed.
        # Only an explicit denied policy receipt proves the sandbox axis by
        # itself; callers may combine not-requested with prepared errno/probe
        # evidence in an evidence file.
        "network_all_hosts_failed": network_state == "denied",
        "sandbox_network_enabled": False if network_state == "denied" else None,
        "phase": "network",
        "receipt": found,
    }


def _request(args: argparse.Namespace):
    if not args.request_file:
        return None
    cwd = Path(args.worktree or os.getcwd()).resolve(strict=False)
    context = AccessContext.build(
        worktree=cwd,
        artifact_root=Path(args.artifact_root or cwd / ".agent_reports"),
        dispatch_state_root=Path(args.dispatch_state_root or cwd / ".dispatch"),
        agent_home=Path(args.agent_home or cwd / ".agent-home"),
        environ=os.environ,
    )
    return load_request(args.request_file, context=context)


def _probe_write(root: Path) -> dict[str, object]:
    probe = root / f".execution-access-probe-{uuid.uuid4().hex}"
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        return {"kind": "write", "target": str(root), "result": "ok"}
    except OSError as exc:
        return {
            "kind": "write",
            "target": str(root),
            "result": "failed",
            "errno": errno_module.errorcode.get(exc.errno or 0, "UNKNOWN"),
        }
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass


def _probe_connect(target: str, timeout: float) -> dict[str, object]:
    host, separator, port_text = target.rpartition(":")
    if not separator or not host or not port_text.isdigit():
        raise DiagnosisError("probe-target-invalid", "connect target must be host:port")
    try:
        with socket.create_connection((host.strip("[]"), int(port_text)), timeout=timeout):
            return {"kind": "connect", "target": target, "result": "ok"}
    except OSError as exc:
        return {
            "kind": "connect",
            "target": target,
            "result": "failed",
            "errno": errno_module.errorcode.get(exc.errno or 0, "UNKNOWN"),
        }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--evidence-file", type=Path)
    result.add_argument("--attempt-id")
    result.add_argument("--jobs", type=Path)
    result.add_argument("--request-file", type=Path)
    result.add_argument("--launcher-state-root", action="append", default=[])
    result.add_argument("--worktree")
    result.add_argument("--artifact-root")
    result.add_argument("--dispatch-state-root")
    result.add_argument("--agent-home")
    result.add_argument("--allow-probe", action="store_true")
    result.add_argument("--probe-write", type=Path)
    result.add_argument("--probe-connect")
    result.add_argument("--probe-timeout", type=float, default=3.0)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        request = _request(args)
        probes: list[dict[str, object]] = []
        if args.probe_write or args.probe_connect:
            if not args.allow_probe:
                raise DiagnosisError("probe-opt-in-required")
            if request is None:
                raise DiagnosisError("probe-request-required")
            if not 0 < args.probe_timeout <= 10:
                raise DiagnosisError("probe-timeout-invalid")
            if args.probe_write:
                target = args.probe_write.resolve(strict=False)
                if not any(_within(target, root) for root in request.writable_roots):
                    raise DiagnosisError("probe-path-outside-declared-root")
                probes.append(_probe_write(target))
            if args.probe_connect:
                if args.probe_connect not in request.network_hosts:
                    raise DiagnosisError("probe-host-outside-declared-hosts")
                probes.append(_probe_connect(args.probe_connect, args.probe_timeout))

        if args.evidence_file:
            evidence = json.loads(args.evidence_file.read_text(encoding="utf-8"))
            if not isinstance(evidence, dict):
                raise DiagnosisError("diagnosis-evidence-insufficient")
        elif args.attempt_id and args.jobs:
            evidence = _registry_receipt(args.jobs, args.attempt_id)
        elif probes:
            failed = next((item for item in probes if item["result"] == "failed"), None)
            if failed is None:
                print(json.dumps({"classification": "probe-succeeded", "probes": probes}, sort_keys=True))
                return 0
            evidence = {
                "errno": failed.get("errno"),
                "phase": "write" if failed["kind"] == "write" else "connect",
                "path": failed.get("target") if failed["kind"] == "write" else None,
                "mount_writable": True,
            }
        else:
            raise DiagnosisError("diagnosis-evidence-insufficient")
        result = diagnose(evidence, launcher_state_roots=args.launcher_state_root)
        if probes:
            result["probes"] = probes
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except ExecutionAccessError as exc:
        print(json.dumps({"reason": exc.reason}), file=sys.stderr)
        return 64
    except (DiagnosisError, OSError, json.JSONDecodeError) as exc:
        reason = exc.reason if isinstance(exc, DiagnosisError) else "diagnosis-evidence-insufficient"
        print(json.dumps({"reason": reason}), file=sys.stderr)
        return 64 if reason.startswith("probe-") else 65


if __name__ == "__main__":
    raise SystemExit(main())
