#!/usr/bin/env python3
"""session-tidy decision records: one key, one body, one write, one waiting file.

A user's answer to a question (an AskUserQuestion / request_user_input / question
tool call found in a transcript, or a frame-review interview answer) becomes one
``type=decision`` working-tier project record.  Both paths build the record's
source key with ``choice_source_key`` so the same question and the same answer
are one record, whichever path saw it first; a source that already exists is
skipped, never overwritten.

    tidy_decisions.py drain [--cwd D]     write the waiting decisions of that project
    tidy_decisions.py key --question Q --answer A [--answer A ...]

When the memory write fails (store missing, locked, slow) the original text
stays in ``<state>/session-tidy/decisions/pending/<key>.json`` (0600) and the next
``mem tidy-apply`` writes it and removes the file.  Nothing here ever raises into
the caller that accepted the answer: ``record_interview_answers`` reports one
stderr line at most.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Iterator, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MEMORY_DIR = ROOT / "tools" / "memory"
for _path in (str(HERE),):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import session_tidy as st  # noqa: E402

KEY_PREFIX = "user-choice:"
DECISION_TYPE = "decision"
DECISION_TIER = "working"
DECISION_ACTOR = "tidy-applier"
SCHEMA = 1
QUESTION_MAX = 600
OPTION_LABEL_MAX = 120
OPTION_DESC_MAX = 240
OPTIONS_MAX = 8
ANSWER_MAX = 600
NOTE_MAX = 1200
BODY_MAX = 4000
# The release path waits this long at most for the memory tool (seconds).
RECORD_TIMEOUT_SEC = 8.0
LOCK_WAIT_SEC = 5.0


# ---------------------------------------------------------------------------
# Source key
# ---------------------------------------------------------------------------

def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value).strip())


def choice_source_key(question, answers) -> str:
    """``user-choice:<sha256>`` of the question and the chosen answers.

    ``question`` is the question text as asked; ``answers`` the chosen option
    labels, or the free text the user typed.  Nothing else enters the key
    (option lists, descriptions, notes and corrections stay in the body), so a
    transcript parser and the interview path agree on it.
    """
    if isinstance(answers, str):
        answers = [answers]
    picked = sorted(item for item in (_text(a) for a in (answers or [])) if item)
    payload = {"answers": picked, "question": _text(question)}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return KEY_PREFIX + hashlib.sha256(raw).hexdigest()


def _key_hex(source: str) -> str:
    return source[len(KEY_PREFIX):] if source.startswith(KEY_PREFIX) else hashlib.sha256(
        source.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Payload and body
# ---------------------------------------------------------------------------

def _clip(value, limit: int, one_line: bool = False) -> str:
    value = " ".join(_text(value).split()) if one_line else _text(value)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def make_payload(question, answers, *, options=None, note="", correction="", origin=None,
                 cwd=None, asked_at="") -> dict:
    """The self-contained record request: all original text, plus where it came from."""
    if isinstance(answers, str):
        answers = [answers]
    answers = [_text(a) for a in (answers or []) if _text(a)]
    clean_options = []
    for option in (options or [])[:OPTIONS_MAX]:
        if isinstance(option, dict):
            clean_options.append({"label": _text(option.get("label")),
                                  "description": _text(option.get("description") or option.get("means"))})
        else:
            clean_options.append({"label": _text(option), "description": ""})
    return {
        "schema": SCHEMA,
        "source": choice_source_key(question, answers),
        "question": _text(question),
        "options": clean_options,
        "answers": answers,
        "note": _text(note),
        "correction": _text(correction),
        "origin": dict(origin or {}),
        "cwd": str(cwd or os.getcwd()),
        "asked_at": _text(asked_at),
        "recorded_on": datetime.date.today().isoformat(),
    }


def describe_origin(origin: dict) -> str:
    kind = _text((origin or {}).get("kind"))
    if kind == "frame-interview":
        parts = ["방향 확인 답"]
        if origin.get("route_id"):
            parts.append(f"route {origin['route_id']}")
        if origin.get("round"):
            parts.append(f"{origin['round']}차")
        return " · ".join(parts)
    if kind == "transcript":
        who = " ".join(x for x in (_text(origin.get("harness")), _text(origin.get("session"))[:8]) if x)
        return "대화 기록의 질문 도구" + (f" ({who})" if who else "")
    return kind or "알 수 없음"


def decision_body(payload: dict) -> str:
    """Record text: question, options, chosen answer, note/correction, provenance, date."""
    lines = [f"[결정] {_clip(payload.get('question'), QUESTION_MAX)}"]
    options = payload.get("options") or []
    if options:
        lines.append("선택지:")
        for index, option in enumerate(options, 1):
            label = _clip(option.get("label"), OPTION_LABEL_MAX, one_line=True)
            desc = _clip(option.get("description"), OPTION_DESC_MAX, one_line=True)
            lines.append(f"  {index}. {label}" + (f" — {desc}" if desc else ""))
    answers = payload.get("answers") or []
    lines.append("고른 답: " + (" / ".join(_clip(a, ANSWER_MAX) for a in answers) or "(없음)"))
    if payload.get("note"):
        lines.append("사용자 메모: " + _clip(payload["note"], NOTE_MAX))
    if payload.get("correction"):
        lines.append("이해 확인 정정: " + _clip(payload["correction"], NOTE_MAX))
    when = _text(payload.get("asked_at")) or payload.get("recorded_on") or ""
    lines.append(f"출처: {describe_origin(payload.get('origin') or {})} · {when}".rstrip(" ·"))
    body = "\n".join(lines)
    if len(body) > BODY_MAX:
        body = body[: BODY_MAX - 1] + "…"
    return body


def decision_headline(payload: dict) -> str:
    answers = " / ".join(payload.get("answers") or [])
    return _clip(f"결정: {payload.get('question', '')} → {answers}", 120, one_line=True)


# ---------------------------------------------------------------------------
# Locks and waiting files
# ---------------------------------------------------------------------------

class LockBusy(RuntimeError):
    """The apply lock stayed taken for the whole wait."""


@contextlib.contextmanager
def apply_lock(timeout: Optional[float] = None) -> Iterator[None]:
    """The one global lock for memory writes made by tidy (apply, undo, decision records)."""
    root = st.ensure_dir(st.state_root())
    path = root / "apply.lock"
    st._reject_symlink(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise LockBusy(str(path)) from None
                time.sleep(0.1)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def pending_dir() -> Path:
    return st.state_root() / "decisions" / "pending"


def save_pending(payload: dict) -> Optional[Path]:
    path = pending_dir() / f"{_key_hex(payload['source'])[:40]}.json"
    try:
        st.atomic_write_json(path, payload)
    except (OSError, st.StateError) as exc:
        sys.stderr.write(f"[decision] waiting file not written: {exc}\n")
        return None
    return path


def list_pending() -> list:
    """[(path, payload)] oldest first; unreadable files are left alone."""
    folder = pending_dir()
    try:
        names = sorted(folder.iterdir(), key=lambda p: (p.stat().st_mtime, p.name))
    except OSError:
        return []
    found = []
    for path in names:
        if path.suffix != ".json" or path.name.startswith("."):
            continue
        payload = st.read_json(path)
        if isinstance(payload, dict) and payload.get("source") and payload.get("question") is not None:
            found.append((path, payload))
    return found


def discard_pending(path: Path) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)


# ---------------------------------------------------------------------------
# Memory write (existing write path; skip when the source exists)
# ---------------------------------------------------------------------------

def load_mem():
    if str(MEMORY_DIR) not in sys.path:
        sys.path.insert(0, str(MEMORY_DIR))
    import mem  # type: ignore
    return mem


def project_origin(mem, cwd) -> str:
    return str(mem.project_key(Path(cwd), seed=True))


def existing_decision(mem, payload: dict, cwd_origin: str) -> Optional[str]:
    """Id of the active decision with this source in this project, else None."""
    con = mem.get_con()
    try:
        return mem.find_by_source(DECISION_TIER, "project", DECISION_TYPE,
                                  payload["source"], cwd_origin, con)
    finally:
        con.close()


def write_decision(mem, payload: dict, cwd_origin: Optional[str] = None):
    """Write one decision record. Returns ``("written"|"skipped"|"quality", record id or None)``.

    ``skipped`` = this source already exists (never overwritten) or the very same
    text is already a record.  The write itself is ``mem.write_record`` with its
    sanitizer, working-tier expiry and write journal.
    """
    cwd_origin = cwd_origin or project_origin(mem, payload.get("cwd") or os.getcwd())
    found = existing_decision(mem, payload, cwd_origin)
    if found:
        return "skipped", found
    body = decision_body(payload)
    con = mem.get_con()
    try:
        duplicate = mem.find_dup(DECISION_TIER, "project", mem.sanitize(body)[0], cwd_origin, con=con)
    finally:
        con.close()
    if duplicate:
        return "skipped", duplicate
    rid = mem.write_record(
        DECISION_TIER, "project", DECISION_TYPE, body, cwd_origin=cwd_origin,
        source=payload["source"], quiet=True, journal_action="decision-record",
        journal_insert_only=True, journal_actor=DECISION_ACTOR, journal_cwd=payload.get("cwd"),
        headline=decision_headline(payload))
    if not rid:
        return "quality", None
    return "written", rid


def drain_pending(mem=None, project_origin_filter: Optional[str] = None, cwd=None) -> dict:
    """Write every waiting decision of one project and remove the files that landed.

    A file whose write fails stays for the next drain.  Caller holds ``apply_lock``.
    """
    mem = mem or load_mem()
    wanted = project_origin_filter
    if wanted is None and cwd is not None:
        wanted = project_origin(mem, cwd)
    result = {"written": 0, "skipped": 0, "kept": 0, "ids": []}
    for path, payload in list_pending():
        try:
            origin = project_origin(mem, payload.get("cwd") or os.getcwd())
            if wanted is not None and origin != wanted:
                continue
            status, rid = write_decision(mem, payload, origin)
        except BaseException as exc:  # noqa: BLE001 - a failed write keeps its file
            if isinstance(exc, KeyboardInterrupt):
                raise
            sys.stderr.write(f"[decision] kept {path.name}: {type(exc).__name__}: {str(exc)[:160]}\n")
            result["kept"] += 1
            continue
        if status == "written":
            result["written"] += 1
            result["ids"].append(rid)
        else:
            result["skipped"] += 1
        discard_pending(path)
    return result


# ---------------------------------------------------------------------------
# D-87: frame-review answers
# ---------------------------------------------------------------------------

def interview_payloads(interview: dict, answers: dict, *, route_id="", cwd=None, actor_kind="unknown") -> list:
    """One payload per answered question (plus the understanding correction when given)."""
    if (actor_kind != "user" or not isinstance(interview, dict) or not isinstance(answers, dict)
            or answers.get("actor_kind") != "user"):
        return []
    origin = {"kind": "frame-interview", "route_id": _text(route_id or interview.get("route_id")),
              "round": interview.get("round") or 1, "actor_kind": actor_kind}
    given = answers.get("answers") if isinstance(answers.get("answers"), dict) else {}
    correction = _text(answers.get("correction"))
    out = []
    for question in interview.get("questions") or []:
        if not isinstance(question, dict):
            continue
        entry = given.get(_text(question.get("id")))
        if not isinstance(entry, dict):
            continue
        options = [o for o in (question.get("options") or []) if isinstance(o, dict)]
        labels = [_text(o.get("label")) for o in options]
        choice, note = entry.get("choice"), _text(entry.get("note"))
        if isinstance(choice, bool):
            continue
        if isinstance(choice, int) and 0 <= choice < len(labels):
            picked, kept_note = [labels[choice]], note
        elif isinstance(choice, str) and choice in labels:
            picked, kept_note = [choice], note
        elif note:
            picked, kept_note = [note], ""  # off-menu: the user's own words are the answer
        else:
            continue
        text = _text(question.get("question"))
        if not text:
            continue
        out.append(make_payload(text, picked, options=options, note=kept_note,
                                correction=correction, origin=origin, cwd=cwd,
                                asked_at=_text(interview.get("created"))))
    if answers.get("understanding_confirmed") is False and correction:
        understanding = _text(interview.get("understanding"))
        out.append(make_payload("이해 확인: " + (understanding or "방향 확인 문장"),
                                ["수정 요청: " + correction], origin=origin, cwd=cwd,
                                asked_at=_text(interview.get("created"))))
    return out


def record_interview_answers(interview, answers, *, route_id="", cwd=None,
                             timeout: float = RECORD_TIMEOUT_SEC, stderr=None, actor_kind="unknown") -> str:
    """Record the answers just accepted at the frame-review gate. Never raises.

    The original text is saved to the waiting folder first; then one short
    ``drain`` child writes the records and removes the files.  Any failure or
    timeout leaves the files for the next tidy and costs one stderr line.
    Returns ``"recorded"``, ``"waiting"`` or ``"none"`` (nothing to record).
    """
    stderr = stderr or sys.stderr
    try:
        payloads = interview_payloads(interview, answers, route_id=route_id, cwd=cwd, actor_kind=actor_kind)
        if not payloads:
            return "none"
        saved = [p for p in (save_pending(item) for item in payloads) if p is not None]
        if not saved:
            return "waiting"
        argv = [sys.executable, str(Path(__file__).resolve()), "drain", "--cwd", str(cwd or os.getcwd())]
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                   stdin=subprocess.DEVNULL, check=False)
        if completed.returncode == 0 and not any(p.exists() for p in saved):
            return "recorded"
        reason = (completed.stderr or completed.stdout or f"exit {completed.returncode}").strip().splitlines()
        stderr.write("[decision] 방향 확인 답의 기억 기록이 대기 중입니다 (다음 정리 때 반영): "
                     f"{reason[-1][:160] if reason else 'unknown'}\n")
        return "waiting"
    except subprocess.TimeoutExpired:
        stderr.write("[decision] 방향 확인 답의 기억 기록이 시간 안에 끝나지 않아 대기 중입니다 (다음 정리 때 반영)\n")
        return "waiting"
    except BaseException as exc:  # noqa: BLE001 - the accepted answer must never fail on this
        if isinstance(exc, KeyboardInterrupt):
            raise
        with contextlib.suppress(Exception):
            stderr.write(f"[decision] 방향 확인 답의 기억 기록을 건너뜁니다: {type(exc).__name__}: {str(exc)[:120]}\n")
        return "waiting"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tidy_decisions", allow_abbrev=False)
    sub = parser.add_subparsers(dest="cmd", required=True)
    drain = sub.add_parser("drain", help="write the waiting decisions of one project")
    drain.add_argument("--cwd", default=None)
    key = sub.add_parser("key", help="print the source key of a question and answers")
    key.add_argument("--question", required=True)
    key.add_argument("--answer", action="append", default=[])
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "key":
        print(choice_source_key(args.question, args.answer))
        return 0
    try:
        with apply_lock(timeout=LOCK_WAIT_SEC):
            result = drain_pending(cwd=args.cwd or os.getcwd())
    except LockBusy:
        sys.stderr.write("apply lock busy; waiting decisions stay for the next tidy\n")
        return 3
    if result["written"]:
        # This child writes through ``mem.write_record`` without ``mem.main()``, so it asks
        # for the exchange a foreground write owes by the same call ``main()`` makes.
        with contextlib.suppress(Exception):
            load_mem()._exchange_after_command()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if not result["kept"] else 1


if __name__ == "__main__":
    sys.exit(main())
