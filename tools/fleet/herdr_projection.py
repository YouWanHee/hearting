#!/usr/bin/env python3
"""One pane-title projection shared by Claude, Codex, and OpenCode.

A pane header is the only identity surface all three harnesses share. Each runtime draws
its own status line differently and none of them can be made to agree, so the identity —
which session this is, and whether it is a steward — goes on the outside, in the herdr
pane header, where the shape is ours to fix (user 2026-09-09: "그 바깥에 장치를 두는게
맞겠다"). The header reads

    [3a] claude   통신 표시 일관성 수정
    [b0] claude ⚑ 하팅 감독 — 네 세션 관리

as `display_agent` = ``[<tag>] <harness>[ ⚑]`` and `title` = the summary Fleet's title
worker already produced. Nothing here generates a summary; it only projects one.

Display-only and fail-soft throughout: no herdr, no pane, no title, or a slow formatter
all mean "report less", never an error. Registered workers project nothing at all — the
pane belongs to the interactive session that owns it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_TOOLS = str(Path(__file__).resolve().parents[1])
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

from fleet.session_handle import (clip_cells, resolve_display_inputs,  # noqa: E402
                                  resolve_tag, sanitize_title)

HARNESSES = ("claude", "codex", "opencode")
_AGENT_W = 24            # herdr's display_agent budget
_TITLE_W = 48            # herdr's title budget
_STEWARD_MARK = "⚑"
_FORMATTER_TIMEOUT = 0.2
_HERDR_TIMEOUT = 0.5
_WORKER_ENV = ("AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH", "OPENCODE_DISPATCH_SLUG",
               "FLEET_TITLE_REFRESH", "MEM_DISTILL")


def is_worker() -> bool:
    """A registered/background worker never owns the interactive pane header (D-42)."""
    if os.environ.get("AGENT_SESSION_ROLE") == "worker":
        return True
    return any(os.environ.get(name) for name in _WORKER_ENV)


def _runtime_name(harness: str, session_id: str) -> str:
    """The name the runtime itself exposes (Codex's `thread_name`, a user-set Claude
    name). Same resolver Fleet's collectors use, so the pane and the board agree."""
    try:
        if harness == "codex":
            from fleet.collectors.codex import _home, _thread_runtime_names
            name = _thread_runtime_names(_home()).get(session_id)
            if isinstance(name, str) and name.strip():
                return name.strip()
    except Exception:
        pass
    try:
        inputs = resolve_display_inputs(harness, session_id)
        return inputs.get("runtime_name") or inputs.get("registry_name") or ""
    except Exception:
        return ""


def session_title(harness: str, session_id: str) -> str:
    """The summary Fleet's own title worker already wrote — never a newly generated one.

    Falls back to the runtime's own session name so a session whose sidecar has not been
    written yet still says something; it never falls back to the folder name, which would
    just repeat what herdr already shows beside the pane.
    """
    title = ""
    try:
        from fleet.titles import read
        title = sanitize_title((read(session_id, harness=harness) or {}).get("title"))
    except Exception:
        title = ""
    if title:
        return title
    # Only the runtime's OWN name is an acceptable stand-in. `display_name()` would fall
    # through to the folder — or to its literal "?" last resort — and a pane header saying
    # "?" is worse than one saying nothing.
    return sanitize_title(_runtime_name(harness, session_id))


def is_steward(harness: str, session_id: str) -> bool:
    """True when this session's marker holds steward ROLE evidence.

    Asks the ledger tool itself which entries count, exactly as Fleet's collector does —
    a second copy of that rule here is how the badge and the board start disagreeing.
    """
    if not session_id:
        return False
    try:
        import importlib.util
        for candidate in Path(__file__).resolve().parents:
            tool = candidate / "utilities" / "peer-message.py"
            if not tool.is_file():
                continue
            spec = importlib.util.spec_from_file_location("_peer_message_ro", str(tool))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            marker = (module.read_steward_markers() or {}).get((harness, session_id))
            return bool(marker and module.steward_evidence_targets(marker))
    except Exception:
        pass
    return False


def compose(harness: str, session_id: str, *, tag=None, steward=None, title=None,
            label=None) -> tuple:
    """→ ``(display_agent, title)`` in the fixed order number → harness → ⚑ → summary.

    ``label`` lets a user formatter rename the middle harness word; the badge and the
    steward mark stay ours, so a personal formatter cannot quietly delete the two things
    that identify the session.
    """
    harness = str(harness or "").lower()
    if tag is None:
        tag = resolve_tag(harness, session_id)
    if steward is None:
        steward = is_steward(harness, session_id)
    if title is None:
        title = session_title(harness, session_id)
    middle = sanitize_title(label) or harness or "agent"
    agent = ("[%s] %s" % (tag, middle)) if tag else middle
    if steward:
        agent += " " + _STEWARD_MARK
    return clip_cells(agent, _AGENT_W), clip_cells(sanitize_title(title), _TITLE_W)


def _formatter_path() -> Path:
    override = os.environ.get("HERDR_SESSION_METADATA_FORMATTER")
    if override:
        return Path(override).expanduser()
    config = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return config / "hearting" / "herdr-session-metadata"


def _formatter_overrides(harness: str, session_id: str, title: str) -> tuple:
    """F-95's optional personal formatter → ``(label, title)`` overrides, else ``(None, None)``."""
    formatter = _formatter_path()
    try:
        if not formatter.is_file() or not os.access(formatter, os.X_OK):
            return None, None
        result = subprocess.run(
            [str(formatter), "--harness", harness, "--session-id", session_id,
             "--summary", title],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            timeout=_FORMATTER_TIMEOUT, check=False)
        if result.returncode or len(result.stdout.encode("utf-8")) > 4096:
            return None, None
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            return None, None
        return value.get("display_agent"), value.get("title")
    except Exception:
        return None, None


def project(harness: str, session_id: str, *, pane_id=None, worker=None,
            report_session=True) -> bool:
    """Report this session's pane metadata to herdr. Always returns True (fail-soft)."""
    harness = str(harness or "").lower()
    if harness not in HARNESSES or not session_id:
        return True
    if worker if worker is not None else is_worker():
        return True
    pane = pane_id or os.environ.get("HERDR_PANE_ID", "")
    herdr = shutil.which("herdr")
    if not pane or not herdr:
        return True
    title = session_title(harness, session_id)
    label, custom_title = _formatter_overrides(harness, session_id, title)
    agent, shown_title = compose(harness, session_id, title=custom_title or title,
                                 label=label)
    source = "herdr:%s" % harness
    commands = []
    if report_session:
        commands.append([herdr, "pane", "report-agent-session", pane, "--source", source,
                         "--agent", harness, "--agent-session-id", session_id])
    metadata = [herdr, "pane", "report-metadata", pane, "--source", source,
                "--display-agent", agent]
    if shown_title:
        metadata += ["--title", shown_title]
    commands.append(metadata)
    for command in commands:
        try:
            subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=_HERDR_TIMEOUT, check=False)
        except Exception:
            pass
    return True


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", required=True, choices=list(HARNESSES))
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--pane")
    parser.add_argument("--no-report-session", action="store_true",
                        help="skip report-agent-session (the runtime's own hook owns it)")
    parser.add_argument("--print", action="store_true",
                        help="print the composed metadata instead of reporting it")
    args = parser.parse_args(argv)
    if args.print:
        agent, title = compose(args.harness, args.session_id)
        print(json.dumps({"display_agent": agent, "title": title}, ensure_ascii=False))
        return 0
    project(args.harness, args.session_id, pane_id=args.pane,
            report_session=not args.no_report_session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
