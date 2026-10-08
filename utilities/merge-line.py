#!/usr/bin/env python3
"""Serialize a repository's branch update, head CI and merge across sessions."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from urllib.parse import quote, urlparse
import uuid


class MergeError(RuntimeError):
    pass


def one_line(value):
    return " ".join(str(value).split())


def pid_start(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return None if fields[0] in ("Z", "X") else fields[19]


def state_directory(repository):
    state = Path(os.environ.get("XDG_STATE_HOME", ""))
    if not state.is_absolute():
        state = Path.home() / ".local/state"
    key = hashlib.sha256(repository.lower().encode()).hexdigest()[:24]
    return state / "hearting/merge-line" / key


class MergeTurn:
    """The short bookkeeping lock gives the long-lived flock FIFO order."""

    def __init__(self, directory, pr, emit=print, interval=1):
        self.directory, self.pr = Path(directory), pr
        self.emit, self.interval = emit, interval
        self.token = uuid.uuid4().hex
        self.guard_fd = self.turn_fd = None

    @contextlib.contextmanager
    def guard(self):
        fcntl.flock(self.guard_fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(self.guard_fd, fcntl.LOCK_UN)

    def read(self):
        try:
            fd = os.open(self.directory / "line.json", os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return []
        with os.fdopen(fd) as source:
            rows = json.load(source)
        return [row for row in rows if pid_start(row["pid"]) == row["start"]]

    def save(self, rows):
        fd, name = tempfile.mkstemp(prefix="line-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(rows, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.directory / "line.json")
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
            self.guard_fd = os.open(self.directory / "bookkeeping.lock", flags, 0o600)
            self.turn_fd = os.open(self.directory / "merge.lock", flags, 0o600)
            with self.guard():
                rows = self.read()
                rows.append({"token": self.token, "pr": self.pr, "pid": os.getpid(),
                             "start": pid_start(os.getpid()), "holding": False})
                self.save(rows)
            last = None
            while True:
                with self.guard():
                    rows = self.read()
                    position = next(i for i, row in enumerate(rows) if row["token"] == self.token)
                    if position == 0:
                        try:
                            fcntl.flock(self.turn_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            pass
                        else:
                            rows[0]["holding"] = True
                            self.save(rows)
                            self.emit(f"merge-line: turn PR=#{self.pr}")
                            return self
                    ahead = rows[position - 1]["pr"] if position else "unknown"
                    waiting_position = position + (0 if rows[0]["holding"] else 1)
                    status = (waiting_position, ahead)
                    if status != last:
                        self.emit(f"merge-line: waiting PR=#{self.pr} position={waiting_position} ahead=#{ahead}")
                        last = status
                time.sleep(self.interval)
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        try:
            if self.guard_fd is not None:
                with self.guard():
                    self.save([row for row in self.read() if row["token"] != self.token])
        finally:
            for fd in (self.turn_fd, self.guard_fd):
                if fd is not None:
                    os.close(fd)
            self.turn_fd = self.guard_fd = None


class GitHub:
    def __init__(self):
        repo = self.json("repo", "view", "--json", "nameWithOwner,url,defaultBranchRef")
        self.repo = repo["nameWithOwner"]
        self.host = urlparse(repo["url"]).hostname
        self.base = repo["defaultBranchRef"]["name"]
        self.identity = f"{self.host}/{self.repo}"

    def json(self, *args):
        result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise MergeError(one_line(result.stderr or result.stdout or "GitHub request failed"))
        return json.loads(result.stdout)

    def api(self, endpoint, *args):
        return self.json("api", f"repos/{self.repo}/{endpoint}", "--hostname", self.host, *args)

    def snapshot(self, pr):
        return self.json("pr", "view", str(pr), "--repo", self.identity, "--json",
                         "state,isDraft,baseRefName,headRefOid,mergeable,statusCheckRollup")

    def base_head(self):
        return self.api(f"commits/{quote(self.base, safe='')}")["sha"]

    def contains_base(self, base, head):
        return self.api(f"compare/{base}...{head}")["merge_base_commit"]["sha"] == base

    def update(self, pr, head):
        self.api(f"pulls/{pr}/update-branch", "--method", "PUT", "-f", f"expected_head_sha={head}")

    def merge(self, pr, head):
        result = self.api(f"pulls/{pr}/merge", "--method", "PUT", "-f", f"sha={head}",
                          "-f", "merge_method=merge")
        if not result.get("merged"):
            raise MergeError(result.get("message", "GitHub did not merge the PR"))
        return result["sha"]


def check_state(snapshot):
    checks = snapshot.get("statusCheckRollup") or []
    pending, success, failed = not checks, False, []
    for check in checks:
        if check.get("__typename") == "StatusContext":
            state, name = check.get("state"), check.get("context", "status")
        else:
            state = check.get("conclusion") if check.get("status") == "COMPLETED" else "PENDING"
            name = check.get("name", "check")
        if state == "SUCCESS":
            success = True
        elif state in ("SKIPPED", "NEUTRAL"):
            continue
        elif state in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED",
                       "STARTUP_FAILURE", "STALE"):
            failed.append(name)
        else:
            pending = True
    if failed:
        raise MergeError("CI failed: " + ", ".join(failed))
    if checks and not pending and not success:
        raise MergeError("CI finished without a successful check")
    return not pending and success


def merge_pr(client, pr, emit=print, sleep=time.sleep, interval=10):
    last = None
    while True:
        snapshot = client.snapshot(pr)
        if snapshot["state"] == "MERGED":
            emit(f"merge-line: already merged PR=#{pr}")
            return None
        if snapshot["state"] != "OPEN" or snapshot["isDraft"]:
            raise MergeError("PR is closed or draft")
        if snapshot["baseRefName"] != client.base:
            raise MergeError(f"PR base must be {client.base}")
        if snapshot["mergeable"] == "CONFLICTING":
            raise MergeError("PR has merge conflicts")
        head, base = snapshot["headRefOid"], client.base_head()
        if not client.contains_base(base, head):
            emit(f"merge-line: updating PR=#{pr} head={head} base={base}")
            client.update(pr, head)
            deadline = time.monotonic() + 120
            while True:
                updated = client.snapshot(pr)
                if updated["headRefOid"] != head or updated["state"] != "OPEN" or updated["isDraft"]:
                    break
                if time.monotonic() >= deadline:
                    raise MergeError("branch update was accepted but the new head is not visible")
                sleep(interval)
            last = None
            continue
        if not check_state(snapshot) or snapshot["mergeable"] != "MERGEABLE":
            status = (head, tuple((c.get("name", c.get("context")), c.get("status"),
                                  c.get("conclusion", c.get("state")))
                                 for c in snapshot.get("statusCheckRollup") or []))
            if status != last:
                emit(f"merge-line: waiting CI PR=#{pr} head={head}")
                last = status
            sleep(interval)
            continue
        # No other participant can merge during this turn. Catch outside pushes
        # or raw merges too; the server then checks the exact head atomically.
        current = client.snapshot(pr)
        if current["headRefOid"] != head or client.base_head() != base:
            last = None
            continue
        if current["state"] != "OPEN":
            continue
        if current["isDraft"] or current["baseRefName"] != client.base:
            raise MergeError("PR became draft or changed its base")
        if not check_state(current) or current["mergeable"] != "MERGEABLE":
            sleep(interval)
            continue
        commit = client.merge(pr, head)
        emit(f"merge-line: merged PR=#{pr} head={head} commit={commit}")
        return commit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pr", type=int, help="PR number in the current GitHub repository")
    args = parser.parse_args()
    if args.pr < 1:
        parser.error("PR number must be positive")
    def interrupted(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        client = GitHub()
        with MergeTurn(state_directory(client.identity), args.pr,
                       emit=lambda line: print(line, flush=True)):
            merge_pr(client, args.pr, emit=lambda line: print(one_line(line), flush=True))
    except KeyboardInterrupt:
        print(f"merge-line: stopped PR=#{args.pr}; turn released", flush=True)
        return 130
    except (MergeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print(f"merge-line: failed PR=#{args.pr}: {one_line(exc)}; turn released", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
