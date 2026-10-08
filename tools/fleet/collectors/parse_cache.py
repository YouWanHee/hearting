"""Disposable exact-file parse results; never cache liveness or process truth."""

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile


# Source changes invalidate derived results across installed releases as well.
_VERSION = hashlib.sha256(Path(__file__).with_name("dispatch.py").read_bytes()).hexdigest()


def key(parser, path, stamp, limits):
    # One file per source/parser: live appends replace it instead of growing a
    # new cache file on every tick. The signature still checks the exact bytes.
    locator = (parser, os.path.abspath(path))
    signature = (parser, _VERSION, stamp, limits)
    return (hashlib.sha256(json.dumps(locator).encode()).hexdigest(),
            json.loads(json.dumps(signature)))


def _directory():
    root = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(root) / "hearting" / "fleet-parses"


def _valid_value(parser, value):
    """A valid JSON object may still be a damaged parse result."""
    if not isinstance(value, dict):
        return False
    if parser == "codex-attempt":
        if not {"token_usage", "thread_id", "thread_ambiguity", "activity", "exec_tool"} <= value.keys():
            return False
        if not isinstance(value["thread_ambiguity"], bool):
            return False
        if value["token_usage"] is not None and not isinstance(value["token_usage"], dict):
            return False
        activity = value["activity"]
        if activity is not None:
            if not isinstance(activity, dict):
                return False
            if not all(isinstance(activity.get(field), str) and activity[field]
                       for field in ("thread_id", "turn_id")):
                return False
            stamp = activity.get("observed_at")
            if (not isinstance(stamp, (int, float)) or isinstance(stamp, bool)
                    or not math.isfinite(stamp)):
                return False
        strings, counters = ("thread_id", "exec_tool"), ()
    elif parser == "claude-attempt":
        strings = ("session_id", "ambiguity", "model", "exec_tool")
        counters = ("active_context_tokens", "context_window_tokens", "session_input_tokens",
                    "session_cached_input_tokens", "session_output_tokens")
    elif parser == "opencode-attempt":
        strings, counters = ("session_id", "ambiguity"), ("active_context_tokens",)
    else:
        return False
    if any(field not in value or (value[field] is not None and not isinstance(value[field], str))
           for field in strings):
        return False
    return all(field in value and (value[field] is None or
               isinstance(value[field], int) and not isinstance(value[field], bool) and value[field] >= 0)
               for field in counters)


def load(cache_key):
    try:
        locator, signature = cache_key
        with (_directory() / (locator + ".json")).open(encoding="utf-8") as stream:
            record = json.load(stream)
        if (isinstance(record, dict) and record.get("signature") == signature
                and _valid_value(signature[0], record.get("value"))):
            return record.get("value")
    except (OSError, ValueError, TypeError, OverflowError):
        return None


def save(cache_key, value, source, stamp):
    """Publish only if the source stayed unchanged while being parsed."""
    temp = None
    try:
        st = os.stat(source)
        if (st.st_mtime_ns, st.st_size, st.st_dev, st.st_ino) != stamp:
            return
        directory = _directory()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp = tempfile.mkstemp(prefix=".parse-", dir=directory)
        locator, signature = cache_key
        # Cache metadata and parsed counters/identity only, never transcript text.
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"signature": signature, "value": value}, stream,
                      ensure_ascii=False, separators=(",", ":"))
        os.replace(temp, directory / (locator + ".json"))
    except (OSError, TypeError, ValueError):
        pass
    finally:
        if temp:
            try:
                os.unlink(temp)
            except OSError:
                pass
