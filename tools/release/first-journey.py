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
import hashlib
import urllib.request
import urllib.error
import signal
import sqlite3
import re
import importlib.util
from pathlib import Path

VERSION = "1"
PROFILES = {"claude-only": ["claude"], "all": ["claude", "codex", "opencode"]}
TASK_HARNESSES = {"claude-only": ["claude"], "all": ["codex", "opencode"]}
AUTH_NAMES = {"claude": [".credentials.json"], "codex": ["auth.json"], "opencode": ["auth.json"]}
DEFAULT_PROMPT = "Fix the typo in greet's output, run the existing focused test, and show the diff."
_ACTIVE_AUTH: tuple[Path, Path, bytes] | None = None


def persist_active_auth() -> None:
    if not _ACTIVE_AUTH: return
    store, target, original = _ACTIVE_AUTH
    try:
        if target.is_file() and not target.is_symlink():
            latest = target.read_bytes()
            if latest != original:
                temporary = store.with_name(store.name + ".tmp")
                temporary.write_bytes(latest); os.chmod(temporary, 0o600); os.replace(temporary, store)
    except OSError: pass


def auth_signal(signum, _frame):
    persist_active_auth()
    raise SystemExit(128 + signum)


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
    process = None
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors="replace", start_new_session=True)
        output, _ = process.communicate(timeout=timeout)
        return {"exit": process.returncode, "seconds": round(time.monotonic() - started, 3), "output": output[-12000:]}
    except subprocess.TimeoutExpired:
        if process:
            try: os.killpg(process.pid, signal.SIGTERM); process.communicate(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
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
            cli: list[tuple[Path, str]] | None = None, probes: bool = False) -> list[str]:
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
    real_home = str(Path.home())
    args += ["--dir", "/home", "--tmpfs", "/home", "--dir", real_home, "--dir", "/home/nas",
             "--tmpfs", "/home/nas", "--bind", str(world), "/journey"]
    if cli: args += ["--dir", "/opt", "--dir", "/opt/journey-cli"]
    made_dirs = {"/opt", "/opt/journey-cli"}
    for source, destination in cli or []:
        parent = "/opt/journey-cli"
        for part in Path(destination).parent.relative_to("/opt/journey-cli").parts:
            parent = str(Path(parent) / part)
            if parent not in made_dirs:
                args += ["--dir", parent]; made_dirs.add(parent)
        args += ["--ro-bind", str(source), destination]
    for key, value in env.items():
        args += ["--setenv", key, value.replace(str(world), "/journey")]
    args += ["--chdir", str(Path("/journey") / cwd.relative_to(world)) if cwd else "/journey/home"]
    if probes:
        args += ["--setenv", "JOURNEY_PROBE_HOME", real_home,
                 "--setenv", "JOURNEY_PROBE_NAS", "/home/nas",
                 "--setenv", "JOURNEY_PROBE_NAME", world.name]
        probe = "import os,pathlib,json,shutil; h=pathlib.Path(os.environ['JOURNEY_PROBE_HOME']); n=pathlib.Path(os.environ['JOURNEY_PROBE_NAS']); r={'real_home_empty':not list(h.iterdir()),'nas_empty':not list(n.iterdir())}; h.joinpath(os.environ['JOURNEY_PROBE_NAME']).write_text('x'); n.joinpath(os.environ['JOURNEY_PROBE_NAME']).write_text('x'); r['herdr_absent_from_path']=shutil.which('herdr') is None; print(json.dumps(r))"
        return args + ["--", "python3", "-c", probe]
    return args + ["--"] + argv


def auth_file(root: Path, harness: str) -> Path:
    return root / harness / AUTH_NAMES[harness][0]


def published_installer(world: Path, tag: str, timeout: int) -> tuple[Path, str, str]:
    base = "https://github.com/dmlguq456/hearting/releases/download/"
    request = urllib.request.Request("https://github.com/dmlguq456/hearting/releases/latest", method="HEAD")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        resolved = response.geturl().rstrip("/").split("/")[-1]
    if tag != "latest":
        resolved = tag
    paths = []
    for suffix in ("install.sh", "install.sh.sha256"):
        with urllib.request.urlopen(base + resolved + "/" + suffix, timeout=timeout) as response:
            paths.append(response.read())
    installer, checksum = paths
    expected = checksum.decode("ascii", "strict").split()[0]
    actual = hashlib.sha256(installer).hexdigest()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected) or actual.lower() != expected.lower():
        raise ValueError("published installer checksum mismatch")
    target = world / "release-assets" / "install.sh"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(installer)
    target.chmod(0o700)
    return target, resolved, actual


def detect_card(text: str) -> bool:
    labels = ["task", "why", "route", "scope", "done"]
    lower = text.lower()
    header = "[실행 확인]" in text or "[execution confirmation]" in lower or "[execution check]" in lower
    positions = [lower.find(label) for label in labels]
    option = any(line.strip() in {"진행 / 수정 / 중단", "proceed / revise / stop"} for line in lower.splitlines())
    return header and all(p >= 0 for p in positions) and positions == sorted(positions) and option


def telemetry(world: Path, harness: str) -> dict:
    # Keep unknown distinct from zero; every reader is restricted to the disposable world.
    if harness == "claude":
        totals = {k: 0 for k in ("input", "output", "cache_creation", "cache_read")}
        seen = {}; sessions = set()
        project_dirs = [p for p in (world / "home").rglob("projects") if p.is_dir()]
        files = [f for p in project_dirs for f in p.rglob("*.jsonl")]
        for file in files:
            for line in file.read_text(errors="replace").splitlines():
                try: row = json.loads(line)
                except json.JSONDecodeError: continue
                msg = row.get("message", {}); usage = msg.get("usage", {})
                mid = msg.get("id")
                if not mid or not usage or row.get("type") != "assistant": continue
                sessions.add(row.get("sessionId", file.stem)); seen[mid] = usage
        for usage in seen.values():
            for key, field in (("input", "input_tokens"), ("output", "output_tokens"),
                               ("cache_creation", "cache_creation_input_tokens"), ("cache_read", "cache_read_input_tokens")):
                value = usage.get(field)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0: totals[key] += value
        return {"status": "measured" if seen else "unknown", **(totals if seen else {k: None for k in totals}),
                "sessions": len(sessions), "reason": None if seen else "no-claude-usage-records"}
    if harness == "opencode":
        db = world / "home/.local/share/opencode/opencode.db"
        if not db.exists(): return {"status": "unknown", "total": None, "reason": "opencode-db-missing"}
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
            if "message" not in tables: raise ValueError("schema")
            columns = {row[1] for row in con.execute("pragma table_info(message)")}
            names = [x for x in ("input_tokens", "output_tokens", "cache_read", "cache_creation") if x in columns]
            if not {"input_tokens", "output_tokens"}.issubset(names): raise ValueError("schema")
            rows = list(con.execute("select " + ",".join(names) + " from message"))
            return {"status": "measured", "total": sum(sum(v or 0 for v in row) for row in rows), "reason": None}
        except (sqlite3.Error, ValueError): return {"status": "unknown", "total": None, "reason": "unsupported-opencode-schema"}
    rollouts = list((world / "home").rglob("rollout-*.jsonl"))
    if rollouts:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fleet"))
            from token_budget import parse_codex_token_count
            latest = {}
            for file in rollouts:
                for line in file.read_text(errors="replace").splitlines():
                    try: row = json.loads(line)
                    except json.JSONDecodeError: continue
                    if row.get("type") == "event_msg" and row.get("payload", {}).get("type") == "token_count":
                        latest[file] = parse_codex_token_count(line, session_id=file.stem)
            vals = [x for x in latest.values() if x.session_total_tokens is not None]
            if vals:
                final = vals[-1]
                return {"status": "measured", "total": final.session_total_tokens,
                        "input": final.session_input_tokens, "output": final.session_output_tokens,
                        "cached_input": final.session_cached_input_tokens,
                        "reasoning_output": final.session_reasoning_output_tokens,
                        "rate_limits": None, "reason": None}
        except (ImportError, ValueError, TypeError): pass
    return {"status": "unknown", "total": None, "reason": "no-supported-codex-token-count"}


def claude_account_snapshot(config: Path) -> dict | None:
    spec = importlib.util.spec_from_file_location("journey_usage_api", Path(__file__).resolve().parents[1] / "fleet/collectors/usage_api.py")
    if not spec or not spec.loader: return None
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    previous = os.environ.get("CLAUDE_CONFIG_DIR")
    try:
        os.environ["CLAUDE_CONFIG_DIR"] = str(config)
        data = module._fetch()
    finally:
        if previous is None: os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else: os.environ["CLAUDE_CONFIG_DIR"] = previous
    if not data: return None
    return {key: data.get(key) for key in ("rl_5h", "rl_7d", "rs_5h", "rs_7d")}


def event_text(output: str, harness: str) -> tuple[str, str | None]:
    text = output; sid = None
    for line in output.splitlines():
        try: row = json.loads(line)
        except json.JSONDecodeError: continue
        sid = sid or row.get("session_id") or row.get("thread_id") or row.get("sessionID")
        if harness == "claude":
            msg = row.get("message", {})
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                content = msg.get("content", text)
                text = "\n".join(str(x.get("text", "")) for x in content if isinstance(x, dict)) if isinstance(content, list) else str(content)
            if row.get("type") == "result": text = str(row.get("result", text))
        elif harness == "codex":
            if row.get("type") == "thread.started": sid = row.get("thread_id", sid)
            if row.get("type") == "item.completed" and row.get("item", {}).get("type") == "agent_message": text = str(row["item"].get("text", text))
        else:
            if row.get("type") in {"text", "assistant"}: text = str(row.get("text", row.get("content", text)))
    return text, sid


def judge_repo(repo: Path, baseline_test: bytes) -> dict:
    git = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True).stdout
    baseline_sha = hashlib.sha256(baseline_test).hexdigest()
    worktrees = [Path(line[9:]) for line in git("worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")]
    refs = git("for-each-ref", "--format=%(refname)", "refs/heads").splitlines()
    candidates: list[tuple[str, Path, bool]] = [("working-tree", repo, True)]
    for path in worktrees:
        if path != repo and path.is_dir(): candidates.append((f"worktree:{path.name}", path, True))
    commits = git("log", "--all", "--format=%H").splitlines()
    for commit in commits:
        candidates.append((f"commit:{commit[:12]}", repo, False))
    checked = []
    for label, base, working in candidates:
        if working:
            source = base / "greet.py"; test = base / "test_greet.py"; cwd = base
        else:
            source_data = subprocess.run(["git", "show", f"{label[7:]}:greet.py"], cwd=repo, capture_output=True).stdout
            test_data = subprocess.run(["git", "show", f"{label[7:]}:test_greet.py"], cwd=repo, capture_output=True).stdout
            source = repo / ".journey-candidate-greet.py"; test = repo / ".journey-candidate-test.py"
            if not source_data or not test_data: continue
            source.write_bytes(source_data); test.write_bytes(test_data); cwd = repo
        if not source.is_file() or not test.is_file(): continue
        if hashlib.sha256(test.read_bytes()).hexdigest() != baseline_sha: checked.append({"candidate":label,"passed":False,"reason":"baseline-test-edited"}); continue
        try:
            output = subprocess.run([sys.executable, str(source)], cwd=cwd, capture_output=True, text=True, timeout=5)
            result = subprocess.run([sys.executable, "-m", "unittest", "-q", "test_greet"], cwd=cwd, capture_output=True, text=True, timeout=10)
            passed = output.returncode == 0 and output.stdout.strip() == "Hello" and result.returncode == 0
        except (OSError, subprocess.TimeoutExpired): passed = False
        checked.append({"candidate": label, "passed": passed, "reason": None if passed else "greet-or-focused-test-failed"})
        if not working:
            source.unlink(missing_ok=True); test.unlink(missing_ok=True)
    routes = list((repo / ".agent_reports/.runtime/routes").glob("*")) if (repo / ".agent_reports/.runtime/routes").exists() else []
    passed = next((item["candidate"] for item in checked if item["passed"]), None)
    return {"code_fixed": passed is not None, "focused_test_passed": passed is not None,
            "route_evidence_observed": bool(routes), "candidate": passed, "candidates": checked,
            "worktrees": [str(p) for p in worktrees], "local_refs": refs}


def headless_turn(harness: str, executable: str, prompt: str, cwd: Path, world: Path,
                  env: dict[str, str], timeout: int, mounts: list[tuple[Path, str]]) -> dict:
    virtual_cwd = "/journey/" + str(cwd.relative_to(world))
    if harness == "claude": argv = [executable, "-p", "--output-format", "stream-json", "--verbose", prompt]
    elif harness == "codex": argv = [executable, "exec", "--json", "--cd", virtual_cwd, prompt]
    else: argv = [executable, "run", "--format", "json", prompt]
    turns = []; answers = 0; sid = None; result = {"status": "task-incomplete", "turns": turns, "canned_answers": 0}; texts=[]
    deadline = time.monotonic() + timeout
    for index in range(1, 5):
        if index > 1:
            if harness == "claude": argv = [executable, "-p", "--output-format", "stream-json", "--verbose", "--resume", sid, "진행"]
            elif harness == "codex": argv = [executable, "exec", "resume", "--json", sid, "진행"]
            else: argv = [executable, "run", "--format", "json", "--session", sid, "진행"]
        remaining = max(1, int(deadline - time.monotonic()))
        step = run(sandbox(argv, world, env, cwd, mounts), env=env, timeout=remaining, cwd=cwd)
        text, found = event_text(step["output"], harness); sid = found or sid
        texts.append(text)
        turns.append({"index": index, "exit": step["exit"], "seconds": step["seconds"], "session_id": sid})
        if step["exit"] != 0: result["status"] = "task-failed"; break
        if text and ("permission" in text.lower() or "refus" in text.lower()): result["status"] = "unexpected-question"; result["turn_index"] = index; break
        if text and ("Hello" in text or "test_greet" in text) and not detect_card(text): result["status"] = "candidate-complete"; break
        if detect_card(text) and answers < 3 and sid:
            answers += 1; continue
        result["status"] = "unexpected-question" if "?" in text else "task-incomplete"; result["turn_index"] = index; break
    result.update({"turns": turns, "canned_answers": answers, "session_id": sid, "output": "\n".join(texts)})
    if answers >= 3 and result["status"] != "candidate-complete": result["status"] = "confirmation-cap"
    return result


def journey_profile(profile: str, args: argparse.Namespace, repo: Path) -> dict:
    global _ACTIVE_AUTH
    began = time.monotonic()
    record: dict = {"schema": "hearting-first-journey-v1", "schema_version": 1, "profile": profile,
                    "mode": "model-free" if args.model_free else "real",
                    "source": {"kind": args.source, "tag": None, "ref": args.ref if args.source == "checkout" else None, "sha256": None},
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "isolation": {"level": "linux-bwrap" if platform.system() == "Linux" else "env-home-only",
                                  "protected_paths_enforced": platform.system() == "Linux", "d3": "not-proven", "checks": [],
                                  "host_io_exceptions": ["source-staging", "summary-export"]},
                    "steps": [], "failures": [], "harnesses": [],
                    "account_usage": {"before": None, "after": None, "delta_pp": None, "attribution": "uncertain", "note": "may include other work", "reason": "measurement-credential-unavailable"},
                    "headless_uncovered": ["login", "first trust dialog", "theme", "native question tool"]}
    tempbase = Path(args.local_root).resolve()
    world = Path(tempfile.mkdtemp(prefix=f"first-journey-{profile}-", dir=tempbase))
    os.chmod(world, 0o700)
    record["isolation"]["world"] = str(world)
    try:
        for part in ("home", "tmp", "bin", "logs", "repos"): (world / part).mkdir(mode=0o700)
        commands = ["python3", "bash", "sh", "git", "curl", "tar", "gzip", "mktemp",
                    "mkdir", "chmod", "cp", "mv", "rm", "cat", "uname", "dirname", "readlink"]
        sysbin = world / "sysbin"
        sysbin.mkdir()
        for name in commands:
            found = shutil.which(name)
            if found:
                resolved = Path(found).resolve()
                if str(resolved).startswith(str(Path.home()) + "/"):
                    continue
                os.symlink(resolved, sysbin / name)
        cli_map = {name: shutil.which(name) for name in PROFILES[profile]}
        home = world / "home"
        for relative in ("tmp", ".config", ".local/share", ".local/state", ".cache", ".claude", ".codex"):
            (home / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
        binaries: dict[str, tuple[Path, str]] = {}
        cli_mounts: list[tuple[Path, str]] = []
        cli_bin = world / "bin"
        for index, harness in enumerate(PROFILES[profile]):
            path = cli_map[harness]
            if path:
                link = cli_bin / harness
                # Keep the source path out of PATH; runtime binding is a later, explicit step.
                resolved = Path(path).resolve()
                if harness == "codex":
                    release = resolved.parent.parent
                    target = f"/opt/journey-cli/codex"
                    cli_mounts.append((release, target))
                    executable = target + "/bin/codex"
                else:
                    target = f"/opt/journey-cli/{harness}"
                    cli_mounts.append((resolved, target))
                    executable = target
                os.symlink(executable, link)
                binaries[harness] = (resolved, executable)
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
        install_args = ["sh", "/journey/release-assets/checkout-install.sh", *runtime_args, "--no-auto-update"] if args.source == "checkout" else ["sh", "/journey/release-assets/install.sh", *runtime_args, "--no-auto-update"]
        install_extras = {}
        if args.source == "checkout":
            install_extras = {"HARNESS_ALLOW_FILE_RELEASES": "1",
                              "HARNESS_RELEASE_INDEX_URL": "file:///journey/release-assets/release-index.json"}
        resolved_tag, installer_sha = "checkout", None
        if args.source == "published":
            try:
                installer_path, resolved_tag, installer_sha = published_installer(world, args.tag, args.step_timeout)
            except Exception as exc:
                record["failures"].append({"reason": "published-installer-fetch-failed", "detail": type(exc).__name__})
                installer_path = None
            if installer_path is None:
                installed = {"exit": 1, "seconds": 0, "output": "published installer unavailable"}
            else:
                install_args[1] = "/journey/release-assets/install.sh"
                installed = run(sandbox(install_args, world, env, cli=cli_mounts), env=env, timeout=args.step_timeout)
        else:
            installed = run(sandbox(install_args, world, {**env, **install_extras}, cli=cli_mounts), env={**env, **install_extras}, timeout=args.step_timeout)
        record["source_detail"] = {"resolved_tag": resolved_tag, "installer_sha256": installer_sha}
        record["source"]["tag"] = resolved_tag
        record["source"]["sha256"] = installer_sha
        record["steps"].append({"name": "install", **{k:v for k,v in installed.items() if k != "output"}, "output_tail": installed["output"][-400:].replace(str(world), "<world>")})
        if installed["exit"] != 0:
            record["failures"].append({"reason": "install-failed", "detail": installed["output"][-300:].replace(str(world), "<world>")})
        else:
            visible = []
            for name in ("claude", "codex", "opencode", "herdr"):
                probe = run(sandbox(["sh", "-c", f"command -v {name}"], world, env, cli=cli_mounts), env=env, timeout=args.step_timeout)
                if probe["exit"] == 0: visible.append(name)
            versions = {}
            for name in PROFILES[profile]:
                if name in binaries:
                    version = run(sandbox([binaries[name][1], "--version"], world, env, cli=cli_mounts), env=env, timeout=args.step_timeout)
                    versions[name] = version["output"][-120:].strip().replace(str(world), "<world>") if version["exit"] == 0 else "unknown"
            expected_visible = ["claude"] if profile == "claude-only" else ["claude", "codex", "opencode"]
            record["environment"] = {"visible_cli_names": visible, "cli_versions": versions,
                                      "transport_flags": {"claude": "-p --output-format stream-json --verbose", "codex": "exec --json", "opencode": "run --format json"},
                                      "allowlist_names": sorted(env)}
            if visible != expected_visible: record["failures"].append({"reason": "cli-visibility-mismatch"})
            if platform.system() == "Linux":
                probe = run(sandbox(["true"], world, env, cli=cli_mounts, probes=True), env=env, timeout=args.step_timeout)
                probe_values = {}
                try: probe_values = json.loads(probe["output"].splitlines()[-1])
                except (ValueError, IndexError): pass
                checks = {"real_home_empty": probe_values.get("real_home_empty") is True,
                          "nas_empty": probe_values.get("nas_empty") is True,
                          "home_write_not_host_visible": not (Path.home()/world.name).exists(), "nas_write_not_host_visible": not (Path("/home/nas")/world.name).exists(),
                          "forbidden_env_absent": not any(k.startswith(("HERDR_", "AGENT_", "CLAUDE_CODE_")) for k in env),
                          "herdr_absent_from_path": probe_values.get("herdr_absent_from_path") is True}
                record["isolation"]["checks"] = [{"check": k, "passed": v} for k,v in checks.items()]
                record["isolation"]["probe_exit"] = probe["exit"]
                record["isolation"]["probe_detail"] = probe["output"][-240:].replace(str(world), "<world>")
                record["isolation"]["d3"] = all(checks.values())
                if not record["isolation"]["d3"]: record["failures"].append({"reason": "isolation-probe-failed"})
            runtimes = PROFILES[profile]
            for runtime in runtimes:
                for label, command in (("runtime-status", ["harness", "runtime", "status", "--runtime", runtime, "--json"]),
                                       ("strict-doctor", ["hearting", "runtime", "doctor", "--runtime", runtime, "--strict"])):
                    checked = run(sandbox(command, world, env, cli=cli_mounts), env=env, timeout=args.step_timeout)
                    tail = checked["output"][-400:].replace(str(world), "<world>")
                    fresh = None
                    if label == "runtime-status" and checked["exit"] == 0:
                        try: fresh = json.loads(checked["output"]).get("freshness") == "fresh"
                        except (ValueError, AttributeError): fresh = False
                    record["steps"].append({"name": f"{label}:{runtime}", "exit": checked["exit"], "seconds": checked["seconds"], "fresh": fresh, "output_tail": tail})
                    if checked["exit"] != 0 or fresh is False:
                        record["failures"].append({"reason": f"{label}-failed", "harness": runtime})
            settings = home / ".claude/settings.json"
            if settings.exists():
                try:
                    cfg = json.loads(settings.read_text())
                    commands = [hook.get("command", "") for entries in cfg.get("hooks", {}).values()
                                for entry in entries for hook in entry.get("hooks", []) if isinstance(hook, dict)]
                    paths = [p for command in commands for p in re.findall(r"\$HOME/([^\"\s]+)", command)]
                    def exists_in_world(relative):
                        candidate = home / relative
                        if candidate.is_file(): return True
                        if candidate.is_symlink():
                            target_path = os.readlink(candidate)
                            if target_path.startswith("/journey/"):
                                return (world / target_path[len("/journey/"):]).is_file()
                        return False
                    missing = [p for p in paths if not exists_in_world(p)]
                    if missing: record["failures"].append({"reason": "claude-hook-command-missing", "count": len(missing)})
                except (ValueError, OSError): record["failures"].append({"reason": "claude-settings-invalid"})
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
            record["steps"].append({"name": f"baseline:{harness}", "exit": baseline["exit"], "seconds": baseline["seconds"], "expected_failure": True, "output_tail": baseline["output"][-300:].replace(str(world), "<world>")})
            if baseline["exit"] == 0 or "Helo" not in baseline["output"] or "Hello" not in baseline["output"]:
                record["failures"].append({"harness": harness, "reason": "baseline-test-not-observed", "detail": baseline["output"][-300:]})
            if args.model_free:
                model = {"status": "skipped(model-free)", "turns": 0, "canned_answers": 0,
                         "session_tokens": {"status":"unknown","input":None,"output":None,"total":None,"reason":"model-free"}}
            elif any(step["name"] == "install" and step["exit"] != 0 for step in record["steps"]):
                model = {"status": "install-failed", "turns": 0, "canned_answers": 0, "session_tokens": {"status":"unknown","input":None,"output":None,"reason":"installation-failed"}}
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
                            target = home / ".local/share/opencode/auth.json"
                        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        target.write_bytes(before); os.chmod(target, 0o600)
                        _ACTIVE_AUTH = (store, target, before)
                        if harness == "claude": env["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
                        elif harness == "codex": env["CODEX_HOME"] = str(home / ".codex")
                        else: env["XDG_DATA_HOME"] = str(home / ".local/share")
                        started = time.monotonic()
                        usage_started = time.monotonic()
                        account_before = claude_account_snapshot(home / ".claude") if harness == "claude" else None
                        if harness == "claude": record["steps"].append({"name":"usage-before:claude","exit":0 if account_before else 1,"seconds":round(time.monotonic()-usage_started,3),"status":"measured" if account_before else "unknown"})
                        outcome = headless_turn(harness, binaries[harness][1], args.prompt, repo_dir, world,
                                                env, args.task_timeout, cli_mounts)
                        usage_started = time.monotonic()
                        account_after = claude_account_snapshot(home / ".claude") if harness == "claude" else None
                        if harness == "claude": record["steps"].append({"name":"usage-after:claude","exit":0 if account_after else 1,"seconds":round(time.monotonic()-usage_started,3),"status":"measured" if account_after else "unknown"})
                        if harness == "claude":
                            same_5h = account_before and account_after and account_before.get("rs_5h") == account_after.get("rs_5h")
                            same_7d = account_before and account_after and account_before.get("rs_7d") == account_after.get("rs_7d")
                            model["account_usage"] = {"before": account_before, "after": account_after,
                                "delta_pp": {"5h": account_after["rl_5h"]-account_before["rl_5h"] if same_5h and account_before.get("rl_5h") is not None and account_after.get("rl_5h") is not None else None,
                                             "7d": account_after["rl_7d"]-account_before["rl_7d"] if same_7d and account_before.get("rl_7d") is not None and account_after.get("rl_7d") is not None else None},
                                "attribution": "uncertain", "note": "may include other work"}
                        (world / "logs" / f"{harness}.output-tail.txt").write_text(outcome["output"], encoding="utf-8")
                        record["steps"].append({"name": f"task:{harness}", "exit": next((t["exit"] for t in reversed(outcome["turns"])), 1),
                                                "seconds": round(time.monotonic()-started, 3)})
                        after = target.read_bytes() if target.exists() and not target.is_symlink() else before
                        if after != before:
                            temp_auth = store.with_name(store.name + ".tmp")
                            temp_auth.write_bytes(after); os.chmod(temp_auth, 0o600); os.replace(temp_auth, store)
                        _ACTIVE_AUTH = None
                        model["status"] = outcome["status"]
                        model["turns"] = len(outcome["turns"])
                        model["canned_answers"] = outcome["canned_answers"]
                        # A task is complete only when the focused test passes after the initial failing baseline.
                        focused = run(sandbox(["python3", "-m", "unittest", "-v", "test_greet"], world,
                                              baseline_env, repo_dir), env=baseline_env, timeout=args.step_timeout)
                        record["steps"].append({"name": f"focused-test:{harness}", "exit": focused["exit"],
                                                "seconds": focused["seconds"]})
                        if outcome["status"] == "candidate-complete" and focused["exit"] != 0:
                            model["status"] = "task-incomplete-test-failed"
                        diff = run(sandbox(["git", "diff", "--", "greet.py"], world, baseline_env, repo_dir),
                                   env=baseline_env, timeout=args.step_timeout)
                        model["changed_demo"] = bool(diff["output"].strip())
                        if model["status"] == "candidate-complete" and not model["changed_demo"]:
                            model["status"] = "task-completed-without-demo-change"
                        model["session_tokens"] = telemetry(world, harness)
                        if harness == "claude": record["account_usage"] = model["account_usage"]
                        model["verification"] = judge_repo(repo_dir, (fixture / "test_greet.py").read_bytes())
                        if model["verification"]["candidate"] and model["verification"]["route_evidence_observed"]:
                            model["status"] = "completed"
                        elif model["status"] == "candidate-complete":
                            model["status"] = "route-evidence-missing"
                except Exception as exc:
                    model["status"] = "task-error"
                    model["detail"] = type(exc).__name__
            record["harnesses"].append({"name": harness, "model": model})
            if model["status"] not in {"skipped(model-free)", "completed"}:
                record["failures"].append({"harness": harness, "reason": model["status"], "detail": str(model.get("detail", ""))[:200]})
        if platform.system() != "Linux": record["isolation"]["d3"] = "not-proven"
    except Exception as exc:
        record["failures"].append({"reason": "journey-error", "detail": (type(exc).__name__ + ": " + str(exc)[:300]).replace(str(world), "<world>")})
    finally:
        persist_active_auth()
        _ACTIVE_AUTH = None
        record["elapsed_seconds"] = round(time.monotonic() - began, 3)
        record["duration_s"] = record["elapsed_seconds"]
        record["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if args.keep_world == "always" or (args.keep_world == "failure" and record["failures"]):
            record["world"] = str(world)
        else:
            shutil.rmtree(world, ignore_errors=True)
    return record


def write_report(record: dict, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(record, indent=2, sort_keys=True) + "\n"
    (output / "journey.json").write_text(raw, encoding="utf-8")
    source_kind = record["source"].get("kind") if isinstance(record["source"], dict) else record["source"]
    lines = [f"# First journey: {record['profile']}", "", f"- Source: {source_kind}",
             f"- Resolved tag: {record.get('source_detail', {}).get('resolved_tag', 'unknown')}",
             f"- Installer SHA-256: {record.get('source_detail', {}).get('installer_sha256', 'unknown')}",
             f"- Isolation: {record['isolation']['level']} (d3={record['isolation']['d3']})",
             f"- Elapsed: {record['elapsed_seconds']} seconds", "", "## Steps", ""]
    lines += [f"- {s['name']}: exit {s['exit']} ({s['seconds']}s)" for s in record["steps"]]
    lines += ["", "## Harnesses", ""]
    lines += [f"- {h['name']}: {h['model']['status']}; turns={h['model']['turns']}; canned answers={h['model'].get('canned_answers', 0)}; session tokens={h['model']['session_tokens']}" for h in record["harnesses"]]
    lines += ["", "## Account usage", "", f"Before: {record['account_usage'].get('before')}; after: {record['account_usage'].get('after')}; delta_pp: {record['account_usage'].get('delta_pp')}; attribution uncertain ({record['account_usage'].get('note')}).", "", "## Headless uncovered", ""]
    lines += [f"- {x}" for x in record.get("headless_uncovered", [])]
    lines += ["", "## Isolation checks", ""]
    lines += [f"- {x['check']}: {'PASS' if x['passed'] else 'FAIL'}" for x in record["isolation"].get("checks", [])] or ["- not-proven"]
    lines += ["", "## Failures", ""]
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
                for name in ("home", "tmp", "config", "data", "state", "cache", "claude", "codex", "opencode"):
                    (world / name).mkdir(parents=True, mode=0o700, exist_ok=True)
                config = {"claude": world / "claude", "codex": world / "codex", "opencode": world / "opencode"}[harness]
                env = clean_env(world / "home", [Path("/usr/bin"), Path("/bin")], {
                    "CLAUDE_CONFIG_DIR": str(config), "CODEX_HOME": str(config),
                    "XDG_CONFIG_HOME": str(world / "config"), "XDG_DATA_HOME": str(world / "data"),
                    "XDG_STATE_HOME": str(world / "state"), "XDG_CACHE_HOME": str(world / "cache")})
                if harness == "opencode": env["XDG_DATA_HOME"] = str(world / "data")
                resolved = Path(cli).resolve()
                if harness == "codex": mounts=[(resolved.parent.parent,"/opt/journey-cli/codex")]; executable="/opt/journey-cli/codex/bin/codex"
                else: mounts=[(resolved,"/opt/journey-cli/"+harness)]; executable="/opt/journey-cli/"+harness
                command = {"claude": [executable, "auth", "login"], "codex": [executable, "login"],
                           "opencode": [executable, "auth", "login"]}[harness]
                result = subprocess.run(sandbox(command, world, env, cli=mounts), env=env)
                if result.returncode == 0:
                    candidates = {"claude": config / ".credentials.json", "codex": config / "auth.json",
                                  "opencode": world / "data/opencode/auth.json"}
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
    signal.signal(signal.SIGINT, auth_signal)
    signal.signal(signal.SIGTERM, auth_signal)
    for profile in ("claude-only", "all"):
        record = journey_profile(profile, args, repo)
        write_report(record, output / profile)
        if record["failures"]: overall = 1
    return overall


if __name__ == "__main__":
    raise SystemExit(main())
