"""Private, observational state for explicitly marked final-reply questions."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import fcntl


_MARKER = re.compile(r"^\s*\[(?:답변 필요|결정 필요)\]\s*(.*)$")
_MAX_QUESTION = 160
_SAFE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


def state_root() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(xdg) / "hearting" / "human-waits"


def _path(seat_key: str) -> Path:
    digest = hashlib.sha256(str(seat_key).encode("utf-8")).hexdigest()
    return state_root() / (digest + ".json")


def _cache_path() -> Path:
    return state_root() / "projection.json"


def _clean(value: str) -> str:
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value))
    value = " ".join("".join(ch for ch in value if ch >= " " and ch != "\x7f").split())
    return value[:_MAX_QUESTION]


def marked_question(text: str) -> str | None:
    """Return the short first marked line, never infer from punctuation or tone."""
    if not isinstance(text, str):
        return None
    lines = text.splitlines()
    for line in lines:
        match = _MARKER.match(line)
        if match:
            return _clean(match.group(1)) or _clean(line.strip())
    return None


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    meta = os.lstat(path.parent)
    if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid():
        raise OSError("answer-wait directory is not private owner state")
    os.chmod(path.parent, 0o700)
    fd, name = tempfile.mkstemp(prefix=".wait-", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def open_signal(harness: str, session_id: str, seat_key: str, text: str, *,
                sequence: int = 0, now: float | None = None) -> bool:
    question = marked_question(text)
    if (not question or harness not in {"claude", "codex", "opencode"}
            or not _SAFE.fullmatch(str(session_id)) or not seat_key):
        return False
    moment = time.time() if now is None else float(now)
    generation = max(0, int(sequence))
    item_id = hashlib.sha256((harness + "\0" + session_id + "\0" + seat_key +
                              "\0" + question + "\0" + str(generation)).encode()).hexdigest()
    path = _path(seat_key)
    prior = _read(path)
    if prior and prior.get("id") == "S4:" + item_id and prior.get("question") == question:
        moment = float(prior.get("since", moment))
    value = {"schema_version": 1, "id": "S4:" + item_id, "source": "S4",
             "harness": harness, "session_id": session_id, "seat_key": str(seat_key),
             "question": question, "since": moment, "sequence": generation}
    try:
        _write(path, value)
        _notify_item(value, boundary="registered")
        _ensure_watcher({"items": [value]})
        return True
    except (OSError, ValueError, TypeError):
        return False


def close_signal(seat_key: str, *, sequence: int | None = None) -> bool:
    """Close only this exact seat, and never let an older input clear a newer wait."""
    path = _path(seat_key)
    try:
        current = _read(path)
        if current is None:
            return True
        if sequence is not None and int(current.get("sequence", 0)) > int(sequence):
            return False
        path.unlink()
        return True
    except (OSError, ValueError, TypeError):
        return False


def _read(path: Path):
    try:
        meta = os.lstat(path)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                or stat.S_IMODE(meta.st_mode) & 0o077):
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or value.get("source") != "S4" or not isinstance(value.get("question"), str)
                or not value.get("seat_key") or not value.get("id")):
            return None
        return value
    except (OSError, ValueError, TypeError):
        return None


def read_signals(*, now: float | None = None) -> list[dict]:
    """Read valid private signals. This function never creates or changes state."""
    root = state_root()
    try:
        entries = tuple(root.iterdir())
    except OSError:
        return []
    instant = time.time() if now is None else float(now)
    out = []
    for path in entries:
        if path.suffix != ".json" or path.is_symlink():
            continue
        value = _read(path)
        if value is None or not isinstance(value.get("since"), (int, float)):
            continue
        value["stale"] = instant - float(value["since"]) >= 7 * 86400
        out.append(value)
    return sorted(out, key=lambda item: (item.get("since", 0), item["id"]))


def publish_cache(result: dict) -> bool:
    """Publish a private derived view for fast display surfaces."""
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        return False
    by_session: dict[str, int] = {}
    for item in result["items"]:
        if item.get("stale"):
            continue
        owner = item.get("owner") or {}
        harness, sid = owner.get("harness"), owner.get("session_id")
        if harness and sid:
            key = str(harness) + "\0" + str(sid)
            by_session[key] = by_session.get(key, 0) + 1
    value = {"schema_version": 1, "observed_at": float(result.get("observed_at") or time.time()),
             "complete": bool(result.get("complete")), "total": len(result["items"]),
             "by_session": by_session}
    try:
        _write(_cache_path(), value)
        _notify_result(result)
        _ensure_watcher(result)
        return True
    except (OSError, ValueError, TypeError):
        return False


def cached_count(harness: str, session_id: str, *, now: float | None = None,
                 max_age: float = 60.0) -> tuple[int, int] | None:
    """Return (global total, exact session count) from a fresh complete cache."""
    path = _cache_path()
    try:
        meta = os.lstat(path)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                or stat.S_IMODE(meta.st_mode) & 0o077):
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    current = time.time() if now is None else float(now)
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or not isinstance(value.get("observed_at"), (int, float))
            or current - float(value["observed_at"]) > max_age):
        return None
    counts = value.get("by_session") if isinstance(value.get("by_session"), dict) else {}
    key = str(harness) + "\0" + str(session_id)
    total = int(value.get("total", 0)) if value.get("complete") else -1
    return total, int(counts.get(key, 0))


def _refresh_cache() -> bool:
    root = state_root()
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = root / ".projection.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            import sys
            project_root = str(Path(__file__).resolve().parents[1])
            if project_root not in sys.path:
                sys.path.insert(0, project_root)
            from tools.fleet.collectors import dispatch
            import human_gate_observation
            result = human_gate_observation.read_answer_waits(
                jobs_paths=dispatch.human_gate_registry_paths())
            return publish_cache(result)
        finally:
            os.close(fd)
    except Exception:
        return False


def _claim_notification(item_id: str, boundary: str):
    digest = hashlib.sha256((item_id + "\0" + boundary).encode()).hexdigest()
    root = state_root() / "notifications"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    path = root / (digest + ".sent")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        return None
    return path, fd


def _notify_item(item: dict, *, boundary: str, sound: bool = False) -> bool:
    executable = shutil.which("herdr")
    if not executable:
        return False
    claim = None
    try:
        claim = _claim_notification(str(item.get("id") or ""), boundary)
        if claim is None:
            return True
        marker, fd = claim
        os.write(fd, (boundary + "\n").encode())
        os.fchmod(fd, 0o600)
        os.close(fd)
        claim = (marker, None)
        question = _clean(item.get("question") or "사용자 답 대기")
        age = max(0, int((time.time() - float(item.get("since") or time.time())) // 3600))
        title = "◑답변 필요" + (" · %dh" % age if boundary in {"1h", "6h"} else "")
        command = [executable, "notification", "show", title, "--body", question or "사용자 답 대기"]
        if sound:
            command.extend(("--sound", "request"))
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=5, check=False)
        if result.returncode == 0:
            return True
        marker.unlink(missing_ok=True)
        return False
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        if claim is not None:
            try:
                if claim[1] is not None:
                    os.close(claim[1])
            except OSError:
                pass
            claim[0].unlink(missing_ok=True)
        return False


def _notify_result(result: dict) -> None:
    now = float(result.get("observed_at") or time.time())
    for item in result.get("items", ()):
        if not isinstance(item, dict) or now - float(item.get("since") or now) >= 7 * 86400:
            continue
        _notify_item(item, boundary="registered")
        age = now - float(item.get("since") or now)
        if age >= 6 * 3600:
            _notify_item(item, boundary="1h")  # skipped boundary prevents a delayed replay
            _notify_item(item, boundary="6h", sound=True)
        elif age >= 3600:
            _notify_item(item, boundary="1h", sound=True)


def _ensure_watcher(result: dict) -> None:
    if not shutil.which("herdr") or not any(
            float(item.get("since") or time.time()) + 6 * 3600 > time.time()
            for item in result.get("items", ()) if isinstance(item, dict)):
        return
    root = state_root()
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = root / ".watch.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--watch-fd", str(fd)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, pass_fds=(fd,),
                         start_new_session=True)
        os.close(fd)  # the child inherited the locked open description
    except (OSError, subprocess.SubprocessError):
        try:
            os.close(fd)
        except (OSError, UnboundLocalError):
            pass


def _watch(fd: int) -> None:
    try:
        project_root = str(Path(__file__).resolve().parents[1])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from tools.fleet.collectors import dispatch
        import human_gate_observation
        while True:
            now = time.time()
            result = human_gate_observation.read_answer_waits(
                jobs_paths=dispatch.human_gate_registry_paths(), now=now)
            publish_cache(result)
            future = [float(item.get("since") or now) + 6 * 3600 - now
                      for item in result.get("items", ()) if isinstance(item, dict)
                      and now - float(item.get("since") or now) < 6 * 3600]
            if not future:
                return
            time.sleep(max(1, min(60, min(future))))
    except Exception:
        return
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        except OSError:
            pass


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument("--watch-fd", type=int)
    parser.add_argument("--count", nargs=2, metavar=("HARNESS", "SESSION"))
    args = parser.parse_args(argv)
    if args.refresh_cache:
        _refresh_cache()
        return 0
    if args.watch_fd is not None:
        _watch(args.watch_fd)
        return 0
    if args.count:
        value = cached_count(*args.count)
        if value is not None:
            print(("?" if value[0] < 0 else str(value[0])) + " " + str(value[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
