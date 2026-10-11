"""Optional, create-once operator preferences shared by all Fleet producers."""
import json
import os
from pathlib import Path


def config_path():
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config").expanduser()
    if not root.is_absolute():
        raise ValueError("XDG_CONFIG_HOME must be absolute")
    return root / "hearting" / "fleet.json"


def _read():
    path = config_path()
    if path.is_symlink() or not path.is_file():
        raise ValueError("Fleet config must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("Fleet config must contain a JSON object") from exc
    language = value.get("title_language", "auto") if isinstance(value, dict) else None
    if (not isinstance(language, str) or not language.strip() or len(language) > 40
            or not all(ch.isalpha() or ch in " _-" for ch in language)):
        raise ValueError("title_language must be auto or a language name/code")
    return language.strip()


def title_language():
    try:
        return _read()
    except ValueError:
        return "auto"


def declared_language():
    """One operator preference for all producers; native settings stay read-only."""
    explicit = (os.environ.get("FLEET_NOW_LANG") or "").strip()
    if explicit and explicit.lower() != "auto":
        return explicit
    language = title_language()
    if language.lower() != "auto":
        return language
    # Claude currently exposes a native reply-language preference. It belongs
    # to the operator, so Codex/OpenCode Fleet rows use it too. Their native
    # config schemas expose no equivalent key; do not invent one.
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    try:
        path = root / "settings.json"
        if path.stat().st_size > 1 << 20:
            return ""
        value = json.loads(path.read_text(encoding="utf-8"))
        language = value.get("language") if isinstance(value, dict) else None
        if (isinstance(language, str) and 0 < len(language.strip()) <= 40
                and all(ch.isalpha() or ch in " _-" for ch in language)
                and language.strip().lower() != "auto"):
            return language.strip()
    except (OSError, UnicodeError, ValueError):
        pass
    return ""


def ensure(*, dry_run=False):
    path = config_path()
    if path.exists() or path.is_symlink():
        return {"status": "preserved", "path": str(path)}
    if dry_run:
        return {"status": "would-create", "path": str(path)}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return {"status": "preserved", "path": str(path)}
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write('{"title_language": "auto"}\n')
    return {"status": "created", "path": str(path)}


def validate():
    path = config_path()
    if not path.exists() and not path.is_symlink():
        return {"status": "absent", "ok": True, "path": str(path),
                "detail": "title uses NOW's operator-language selection"}
    try:
        language = _read()
    except ValueError as exc:
        return {"status": "invalid", "ok": False, "path": str(path), "detail": str(exc)}
    return {"status": "valid", "ok": True, "path": str(path), "detail": "title_language=" + language}
