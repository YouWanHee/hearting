#!/usr/bin/env python3
"""Run an isolated, report-only first-use journey for Hearting releases."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VERSION = "1"
PROFILES = {"claude-only": ["claude"], "all": ["claude", "codex", "opencode"]}
TASK_HARNESSES = {"claude-only": ["claude"], "all": ["codex", "opencode"]}
AUTH_NAMES = {"claude": [".credentials.json"], "codex": ["auth.json"], "opencode": ["auth.json"]}
DEFAULT_PROMPT = "Fix the typo in greet's output, run the existing focused test, and show the diff."


def safe_root(value: str, label: str, forbidden: list[Path]) -> Path:
    path = Path(value).expanduser().absolute()
    resolved = path.resolve()
    if path.is_symlink() or any(resolved == p or p in resolved.parents or resolved in p.parents for p in forbidden):
        raise ValueError(f"unsafe {label} path")
    if not resolved.exists():
        resolved.mkdir(parents=True, mode=0o700)
    if not resolved.is_dir():
        raise ValueError(f"{label} must be a directory")
    metadata = resolved.stat()
    if metadata.st_uid != os.getuid() and not (metadata.st_mode & 0o1000 and metadata.st_mode & 0o002):
        raise ValueError(f"{label} must be owned or a sticky shared temp directory")
    if platform.system() == "Linux":
        info = Path("/proc/self/mountinfo").read_text(errors="replace")
        # Reject paths under the project/NAS and host HOME irrespective of mount type.
        if any(str(resolved).startswith(prefix) for prefix in ("/home/nas/", str(Path.home()) + "/")):
            raise ValueError(f"{label} is on a protected path")
        records = []
        for line in info.splitlines():
            fields = line.split()
            mount = fields[4].replace("\\040", " ")
            if str(resolved) == mount or str(resolved).startswith(mount.rstrip("/") + "/"):
                records.append((len(mount), fields[fields.index("-") + 1]))
        if not records:
            raise ValueError(f"cannot identify {label} filesystem")
        fs_type = max(records)[1]
        if fs_type in {"nfs", "nfs4", "cifs", "smb3", "fuse.sshfs"}:
            raise ValueError(f"{label} is on a remote filesystem")
        if fs_type not in {"ext2", "ext3", "ext4", "xfs", "btrfs", "overlay", "tmpfs", "ramfs", "zfs"}:
            raise ValueError(f"cannot verify local {label} filesystem: {fs_type}")
    return resolved


def run(argv: list[str], *, env: dict[str, str], timeout: int, cwd: Path | None = None) -> dict:
    started = time.monotonic()
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout, text=True, errors="replace", start_new_session=True)
        return {"exit": p.returncode, "seconds": round(time.monotonic() - started, 3), "output": p.stdout[-12000:]}
    except subprocess.TimeoutExpired:
        return {"exit": 124, "seconds": round(time.monotonic() - started, 3), "output": "timeout"}
    except OSError as e:
        return {"exit": 127, "seconds": round(time.monotonic() - started, 3), "output": type(e).__name__}


def clean_env(home: Path, paths: list[Path], extras: dict[str, str] | None = None) -> dict[str, str]:
    env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
           "XDG_DATA_HOME": str(home / ".local/share"), "XDG_STATE_HOME": str(home / ".local/state"),
           "XDG_CACHE_HOME": str(home / ".cache"), "TMPDIR": str(home / "tmp"),
           "PATH": os.pathsep.join(map(str, paths)), "LANG": "C.UTF-8", "TERM": "dumb",
           "USER": str(os.getuid()), "DISABLE_AUTOUPDATER": "1"}
    env.update(extras or {})
    return env


def sandbox(argv: list[str], world: Path, env: dict[str, str], cwd: Path | None = None,
            cli: Path | None = None) -> list[str]:
    if platform.system() != "Linux":
        return argv
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("isolation-unavailable: bwrap not found")
    args = [bwrap, "--clearenv", "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--new-session", "--die-with-parent", "--proc", "/proc", "--dev", "/dev",
            "--tmpfs", "/tmp", "--dir", "/run"]
    for directory in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc/ssl", "/etc/ca-certificates"):
        if Path(directory).exists(): args += ["--ro-bind", directory, directory]
    args += ["--dir", "/home", "--tmpfs", "/home", "--dir", "/home/nas",
             "--tmpfs", "/home/nas", "--bind", str(world), "/journey"]
    if cli:
        args += ["--dir", "/opt", "--dir", "/opt/journey-cli",
                 "--ro-bind", str(cli), "/opt/journey-cli/cli"]
    for key, value in env.items():
        args += ["--setenv", key, value.replace(str(world), "/journey")]
    args += ["--chdir", str(Path("/journey") / cwd.relative_to(world)) if cwd else "/journey/home", "--"]
    return args + argv


def auth_file(root: Path, harness: str) -> Path:
    path = root / harness
    if harness == "opencode":
        path /= "opencode"
    return path / AUTH_NAMES[harness][0]


def journey_profile(profile: str, args: argparse.Namespace, repo: Path) -> dict:
    began = time.monotonic()
    record: dict = {"schema": "hearting-first-journey-v1", "profile": profile,
                    "source": args.source, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "isolation": {"level": "linux-bwrap" if platform.system() == "Linux" else "env-home-only",
                                  "protected_paths_enforced": platform.system() == "Linux", "d3": "not-proven"},
                    "steps": [], "failures": [], "harnesses": [],
                    "account_usage": {"before": "unknown", "after": "unknown", "attribution": "may include other work"}}
    tempbase = Path(args.local_root).resolve()
    world = Path(tempfile.mkdtemp(prefix=f"first-journey-{profile}-", dir=tempbase))
    os.chmod(world, 0o700)
    try:
        for part in ("home", "tmp", "bin", "logs", "repos"): (world / part).mkdir(mode=0o700)
        commands = ["python3", "bash", "sh", "git", "curl", "tar", "gzip", "mktemp", "node",
                    "mkdir", "chmod", "cp", "mv", "rm", "cat", "uname", "dirname", "readlink"]
        sysbin = world / "sysbin"
        sysbin.mkdir()
        for name in commands:
            found = shutil.which(name)
            if found:
                os.symlink(Path(found).resolve(), sysbin / name)
        cli_map = {name: shutil.which(name) for name in PROFILES[profile]}
        home = world / "home"
        for relative in ("tmp", ".config", ".local/share", ".local/state", ".cache", ".claude", ".codex"):
            (home / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
        binaries: dict[str, tuple[Path, str]] = {}
        cli_bin = world / "bin"
        for index, harness in enumerate(PROFILES[profile]):
            path = cli_map[harness]
            if path:
                link = cli_bin / harness
                # Keep the source path out of PATH; runtime binding is a later, explicit step.
                os.symlink(f"/opt/journey-cli/{harness}", link)
                binaries[harness] = (Path(path).resolve(), f"/opt/journey-cli/{harness}")
        install_bin = home / ".local/bin"
        install_bin.mkdir(parents=True)
        pathlist = [cli_bin, install_bin, sysbin]
        env = clean_env(home, pathlist, {"CLAUDE_CONFIG_DIR": str(home / ".claude"),
                                        "CODEX_HOME": str(home / ".codex")})
        for key in ("home", "tmp", "repos", "logs"):
            (world / key).mkdir(exist_ok=True)
        t0 = time.monotonic()
        if args.source == "checkout":
            builddir = world / "release-assets"
            p = subprocess.run([sys.executable, str(repo / "tools/install/build-release.py"),
                                "--version", "v0.0.0-journey.1", "--output", str(builddir), "--ref", args.ref],
                               cwd=repo, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
                               capture_output=True, text=True, timeout=args.step_timeout)
            step = {"name": "build-release", "seconds": round(time.monotonic()-t0, 3), "exit": p.returncode}
            if p.returncode == 0:
                index = {"tag_name": "v0.0.0-journey.1", "assets": []}
                for name in ("hearting.tar.gz", "hearting.tar.gz.sha256"):
                    index["assets"].append({"name": name, "browser_download_url": "file:///journey/release-assets/" + name})
                (builddir / "release-index.json").write_text(json.dumps(index), encoding="utf-8")
                shutil.copy2(builddir / "install.sh", builddir / "checkout-install.sh")
        else:
            step = {"name": "release-source", "seconds": 0, "exit": 0, "detail": "published installer resolves release assets at install time"}
        record["steps"].append(step)
        # Install through the regular distribution installer; model-free checks never use auth.
        runtime_args = [part for harness in PROFILES[profile] for part in ("--runtime", harness)]
        install_args = ["bash", "/journey/release-assets/checkout-install.sh", *runtime_args, "--no-auto-update"] if args.source == "checkout" else ["bash", "-c", ""]
        install_extras = {}
        if args.source == "checkout":
            install_extras = {"HARNESS_ALLOW_FILE_RELEASES": "1",
                              "HARNESS_RELEASE_INDEX_URL": "file:///journey/release-assets/release-index.json"}
        if args.source == "published":
            install_args[2] = "set -o pipefail; curl -fsSL https://raw.githubusercontent.com/dmlguq456/hearting/main/install.sh | bash -s -- " + " ".join(runtime_args) + " --no-auto-update"
        installed_env = {**env, **install_extras, "HARNESS_VERSION": args.tag}
        installed = run(sandbox(install_args, world, installed_env), env=installed_env, timeout=args.step_timeout)
        record["steps"].append({"name": "install", **{k:v for k,v in installed.items() if k != "output"}, "output_tail": installed["output"][-2000:]})
        if installed["exit"] != 0:
            record["failures"].append({"reason": "install-failed", "detail": installed["output"][-400:]})
        else:
            doctor = run(sandbox(["hearting", "runtime", "doctor", "--runtime",
                                  "all" if profile == "all" else "claude", "--strict", "--json"],
                                 world, env), env=env, timeout=args.step_timeout)
            record["steps"].append({"name": "strict-doctor", "exit": doctor["exit"],
                                    "seconds": doctor["seconds"], "output_tail": doctor["output"][-1500:]})
            if doctor["exit"] != 0:
                record["failures"].append({"reason": "strict-doctor-failed", "detail": doctor["output"][-300:]})
        # Demo test is explicitly observed before and after; baseline failure is expected.
        for harness in TASK_HARNESSES[profile]:
            repo_dir = world / "repos" / harness
            repo_dir.mkdir()
            fixture = repo / "tools/release/fixtures/first-journey"
            shutil.copy2(fixture / "greet.py", repo_dir / "greet.py")
            shutil.copy2(fixture / "test_greet.py", repo_dir / "test_greet.py")
            baseline_env = clean_env(home, pathlist, {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
            git_steps = []
            git_started = time.monotonic()
            for git_args in (["git", "init", "-q"], ["git", "config", "user.name", "Journey"],
                             ["git", "config", "user.email", "journey@localhost"],
                             ["git", "add", "greet.py", "test_greet.py"],
                             ["git", "commit", "-qm", "baseline demo"]):
                git_result = run(sandbox(list(git_args), world, baseline_env, repo_dir),
                                 env=baseline_env, timeout=args.step_timeout)
                git_steps.append(git_result["exit"])
                if git_result["exit"] != 0: break
            record["steps"].append({"name": f"baseline-repository:{harness}", "exit": git_steps[-1],
                                    "seconds": round(time.monotonic() - git_started, 3),
                                    "initial_commit": git_steps[-1] == 0})
            if git_steps[-1] != 0:
                record["failures"].append({"harness": harness, "reason": "baseline-repository-failed"})
            # Exercise the staged fixture in the private world and keep output local.
            baseline = run(sandbox(["python3", "-m", "unittest", "-v", "test_greet"], world,
                                   baseline_env, repo_dir), env=baseline_env,
                           timeout=args.step_timeout)
            record["steps"].append({"name": f"baseline:{harness}", "exit": baseline["exit"], "seconds": baseline["seconds"], "expected_failure": True, "output_tail": baseline["output"][-1000:]})
            if baseline["exit"] == 0 or "Helo" not in baseline["output"] or "Hello" not in baseline["output"]:
                record["failures"].append({"harness": harness, "reason": "baseline-test-not-observed", "detail": baseline["output"][-300:]})
            if args.model_free:
                model = {"status": "skipped(model-free)", "turns": 0, "canned_answers": 0, "session_tokens": "unknown"}
            elif not auth_file(Path(args.auth_root), harness).is_file():
                model = {"status": "measurement-login-missing", "turns": 0, "canned_answers": 0, "session_tokens": "unknown"}
            elif not cli_map[harness]:
                model = {"status": "cli-missing", "turns": 0, "session_tokens": "unknown"}
            else:
                model = {"status": "pending", "turns": 1, "canned_answers": 0, "session_tokens": "unknown"}
                store = auth_file(Path(args.auth_root), harness)
                try:
                    root = safe_root(args.auth_root, "auth-root", [Path.home(), Path("/home/nas")])
                    if root.stat().st_uid != os.getuid():
                        raise ValueError("auth-root must be owned by the current user")
                    with (root / ".lock").open("a") as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX)
                        before = store.read_bytes()
                        if harness == "claude":
                            target = home / ".claude/.credentials.json"
                        elif harness == "codex":
                            target = home / ".codex/auth.json"
                        else:
                            target = home / ".local/share/opencode/opencode/auth.json"
                        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        target.write_bytes(before); os.chmod(target, 0o600)
                        if harness == "claude":
                            env["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
                            command = ["claude", "-p", "--output-format", "stream-json", "--verbose", args.prompt]
                        elif harness == "codex":
                            env["CODEX_HOME"] = str(home / ".codex")
                            command = ["codex", "exec", "--json", "--sandbox", "workspace-write", "--cd", str(Path("/journey") / repo_dir.relative_to(world)), args.prompt]
                        else:
                            env["XDG_DATA_HOME"] = str(home / ".local/share")
                            command = ["opencode", "run", "--format", "json", args.prompt]
                        started = time.monotonic()
                        outcome = run(sandbox(command, world, env, repo_dir, Path(cli_map[harness]).resolve()),
                                      env=env, timeout=args.task_timeout)
                        (world / "logs" / f"{harness}.output-tail.txt").write_text(outcome["output"], encoding="utf-8")
                        record["steps"].append({"name": f"task:{harness}", "exit": outcome["exit"],
                                                "seconds": round(time.monotonic()-started, 3)})
                        after = target.read_bytes() if target.exists() and not target.is_symlink() else before
                        if after != before:
                            temp_auth = store.with_name(store.name + ".tmp")
                            temp_auth.write_bytes(after); os.chmod(temp_auth, 0o600); os.replace(temp_auth, store)
                        model["status"] = "completed" if outcome["exit"] == 0 else "task-failed"
                        # A task is complete only when the focused test passes after the initial failing baseline.
                        focused = run(sandbox(["python3", "-m", "unittest", "-v", "test_greet"], world,
                                              baseline_env, repo_dir), env=baseline_env, timeout=args.step_timeout)
                        record["steps"].append({"name": f"focused-test:{harness}", "exit": focused["exit"],
                                                "seconds": focused["seconds"]})
                        if outcome["exit"] == 0 and focused["exit"] != 0:
                            model["status"] = "task-incomplete-test-failed"
                        diff = run(sandbox(["git", "diff", "--", "greet.py"], world, baseline_env, repo_dir),
                                   env=baseline_env, timeout=args.step_timeout)
                        model["changed_demo"] = bool(diff["output"].strip())
                        if model["status"] == "completed" and not model["changed_demo"]:
                            model["status"] = "task-completed-without-demo-change"
                        model["session_tokens"] = "unknown"
                except Exception as exc:
                    model["status"] = "task-error"
                    model["detail"] = type(exc).__name__
            record["harnesses"].append({"name": harness, "model": model})
            if model["status"] not in {"skipped(model-free)", "completed"}:
                record["failures"].append({"harness": harness, "reason": model["status"], "detail": str(model.get("detail", ""))[:200]})
        record["isolation"]["d3"] = "namespace paths hidden; sentinel verification pending" if platform.system() == "Linux" else "not-proven"
    except Exception as exc:
        record["failures"].append({"reason": "journey-error", "detail": type(exc).__name__ + ": " + str(exc)[:300]})
    finally:
        record["elapsed_seconds"] = round(time.monotonic() - began, 3)
        if args.keep_world == "always" or (args.keep_world == "failure" and record["failures"]):
            record["world"] = str(world)
        else:
            shutil.rmtree(world, ignore_errors=True)
    return record


def write_report(record: dict, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(record, indent=2, sort_keys=True) + "\n"
    (output / "journey.json").write_text(raw, encoding="utf-8")
    lines = [f"# First journey: {record['profile']}", "", f"- Source: {record['source']}",
             f"- Isolation: {record['isolation']['level']} ({record['isolation']['d3']})",
             f"- Elapsed: {record['elapsed_seconds']} seconds", "", "## Steps", ""]
    lines += [f"- {s['name']}: exit {s['exit']} ({s['seconds']}s)" for s in record["steps"]]
    lines += ["", "## Harnesses", ""]
    lines += [f"- {h['name']}: {h['model']['status']}; turns={h['model']['turns']}; canned answers={h['model'].get('canned_answers', 0)}; session tokens={h['model']['session_tokens']}" for h in record["harnesses"]]
    lines += ["", "## Account usage", "", "Before/after: unknown. Attribution may include other work.", "", "## Failures", ""]
    lines += [f"- {f.get('harness', 'runner')}: {f['reason']}" for f in record["failures"]] or ["- None"]
    (output / "journey.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login"); login.add_argument("--harness", choices=AUTH_NAMES, required=True); login.add_argument("--auth-root", default=f"/tmp/hearting-measure-auth-{os.getuid()}")
    runp = sub.add_parser("run")
    runp.add_argument("--output", required=True); runp.add_argument("--source", choices=("published", "checkout"), default="published")
    runp.add_argument("--tag", default="latest"); runp.add_argument("--ref", default="HEAD"); runp.add_argument("--auth-root", default=f"/tmp/hearting-measure-auth-{os.getuid()}")
    runp.add_argument("--local-root", default="/tmp"); runp.add_argument("--prompt", default=DEFAULT_PROMPT); runp.add_argument("--model-free", action="store_true")
    runp.add_argument("--keep-world", choices=("always", "failure", "never"), default="failure"); runp.add_argument("--step-timeout", type=int, default=300); runp.add_argument("--task-timeout", type=int, default=600)
    args = parser.parse_args()
    if args.command == "login":
        try:
            root = safe_root(args.auth_root, "auth-root", [Path.home(), Path("/home/nas")])
            if root.stat().st_uid != os.getuid():
                raise ValueError("auth-root must be owned by the current user")
        except Exception as exc:
            print(f"unsafe-auth-root: {type(exc).__name__}", file=sys.stderr); return 2
        harness = args.harness; cli = shutil.which(harness)
        if not cli: print("cli-missing", file=sys.stderr); return 1
        with (root / ".lock").open("a") as lock:
            os.chmod(root, 0o700)
            os.chmod(root / ".lock", 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            world = Path(tempfile.mkdtemp(prefix=f"first-journey-login-{harness}-", dir="/tmp"))
            os.chmod(world, 0o700)
            try:
                for name in ("home", "tmp", "config", "data", "state", "cache", "claude", "codex", "opencode/opencode"):
                    (world / name).mkdir(parents=True, mode=0o700, exist_ok=True)
                config = {"claude": world / "claude", "codex": world / "codex", "opencode": world / "opencode"}[harness]
                env = clean_env(world / "home", [Path("/usr/bin"), Path("/bin")], {
                    "CLAUDE_CONFIG_DIR": str(config), "CODEX_HOME": str(config),
                    "XDG_CONFIG_HOME": str(world / "config"), "XDG_DATA_HOME": str(world / "data"),
                    "XDG_STATE_HOME": str(world / "state"), "XDG_CACHE_HOME": str(world / "cache")})
                if harness == "opencode": env["XDG_DATA_HOME"] = str(world / "opencode")
                command = {"claude": ["/opt/journey-cli/cli", "auth", "login"],
                           "codex": ["/opt/journey-cli/cli", "login"],
                           "opencode": ["/opt/journey-cli/cli", "auth", "login"]}[harness]
                result = subprocess.run(sandbox(command, world, env, cli=Path(cli).resolve()), env=env)
                if result.returncode == 0:
                    candidates = {"claude": config / ".credentials.json", "codex": config / "auth.json",
                                  "opencode": config / "opencode/auth.json"}
                    source = candidates[harness]
                    if not source.is_file() or source.is_symlink(): return 1
                    target = auth_file(root, harness); target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    temporary = target.with_name(target.name + ".tmp")
                    temporary.write_bytes(source.read_bytes()); os.chmod(temporary, 0o600); os.replace(temporary, target)
                return result.returncode
            finally:
                shutil.rmtree(world, ignore_errors=True)
    repo = Path(__file__).resolve().parents[2]
    try:
        local = safe_root(args.local_root, "local-root", [Path.home(), Path("/home/nas")])
        output = Path(args.output).expanduser().absolute()
        if output.is_symlink() or local == output.resolve():
            raise ValueError("output must be separate from local experiment root")
        auth = Path(args.auth_root).expanduser().absolute()
        args.local_root = str(local); args.auth_root = str(auth)
    except Exception as exc:
        print(f"unsafe-path: {type(exc).__name__}: {exc}", file=sys.stderr); return 2
    overall = 0
    for profile in ("claude-only", "all"):
        record = journey_profile(profile, args, repo)
        write_report(record, output / profile)
        if record["failures"]: overall = 1
    return overall


if __name__ == "__main__":
    raise SystemExit(main())
