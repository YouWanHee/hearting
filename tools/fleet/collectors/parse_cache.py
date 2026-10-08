"""Disposable exact-file parse results; never cache liveness or process truth."""

import hashlib
import json
import os
from pathlib import Path
import tempfile


# Source changes invalidate derived results across installed releases as well.
_VERSION = hashlib.sha256(Path(__file__).with_name("dispatch.py").read_bytes()).hexdigest()


def key(parser, path, stamp, limits):
    # One file per source/parser: live appends replace it instead of growing a
    # new cache file on every tick. The signature still checks the exact bytes.
    locator = (parser, os.path.abspath(path))
    signature = (_VERSION, stamp, limits)
    return (hashlib.sha256(json.dumps(locator).encode()).hexdigest(),
            json.loads(json.dumps(signature)))


def _directory():
    root = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(root) / "hearting" / "fleet-parses"


def load(cache_key):
    try:
        locator, signature = cache_key
        with (_directory() / (locator + ".json")).open(encoding="utf-8") as stream:
            record = json.load(stream)
        if isinstance(record, dict) and record.get("signature") == signature:
            return record.get("value")
    except (OSError, ValueError, TypeError):
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
