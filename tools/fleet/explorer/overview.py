"""Overview (P2 team / P4 remaining work) built only from observed relation evidence.

Executing lines use ``─▸``, observation ``┄▷`` and messages ``··→``. A relation the snapshot
does not carry is not drawn; an unknown one is shown as ``?`` with its reason, never as an
empty or successful value.
"""
from collections.abc import Mapping

from .ledger import harness_abbr

TITLE = "개요 · 팀 구성(P2) / 남은 일(P4)"
_OPEN = frozenset(("open", "running"))
_CLEANUP = {"pending": "정리 대기", "complete": "정리 끝", "unknown": "정리 ?"}
_DELIVERY = {"pending": "전달 대기", "accepted": "전달 접수", "acked": "전달 확인",
             "acknowledged": "전달 확인", "rejected": "전달 거부", "not-required": "전달 불필요",
             "peer-courier": "전달 중개", "unknown": "전달 ?"}


def _get(value, key, default=None):
    if value is None:
        return default
    found = value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)
    return default if found is None else found


def _main_sessions(snapshot):
    for session in _get(snapshot, "sessions", ()) or ():
        if _get(session, "is_child") or _get(session, "process_role") == "registered-worker":
            continue
        yield session


def _tag(session):
    tag = _get(session, "session_tag")
    return f"[{tag}]" if tag else "[?]"


def _name(value):
    text = _get(value, "name")
    return str(text) if text else "?"


def _distinct(harnesses):
    seen = []
    for harness in harnesses:
        abbr = harness_abbr(harness)
        if abbr not in seen:
            seen.append(abbr)
    return "/".join(seen)


def _lane(snapshot, session):
    sid = _get(session, "session_id")
    jobs = [job for job in _get(snapshot, "jobs", ()) or ()
            if sid and _get(job, "parent_sid") == sid and str(_get(job, "status") or "") in _OPEN]
    owners = [job for job in jobs if _get(job, "worker_type") == "owner"]
    running = [job for job in jobs if _get(job, "worker_type") != "owner"
               and _get(job, "liveness") == "working"]
    parts = [f"{_tag(session)} {harness_abbr(_get(session, 'harness'))}"]
    if owners:
        parts.append(f"감독 {_distinct(_get(job, 'harness') for job in owners)}")
        if running:
            parts.append(f"실행 중 단계 {_distinct(_get(job, 'harness') for job in running)}")
    return " ─▸ ".join(parts)


def _age(entry):
    age = _get(entry, "age_min")
    return f"{age}분 전" if isinstance(age, (int, float)) and not isinstance(age, bool) else "시각 ?"


def _relation_lines(snapshot, focus_sid):
    lines = []
    focus = None
    for session in _main_sessions(snapshot):
        if focus_sid and _get(session, "session_id") == focus_sid:
            focus = session
        for parent in _get(session, "steward_parents", ()) or ():
            lines.append(f"{_name(parent)} ┄▷ {_tag(session)}")
        targets = _get(session, "steward_targets", ()) or ()
        if targets:
            flag = " ⚑" if _get(session, "steward") else ""
            lines.append(f"{_tag(session)}{flag} 관찰 {len(targets)} 대상")
            for target in targets:
                lines.append(f"  ┄▷ {_name(target)} {harness_abbr(_get(target, 'harness'))}")
    return lines, focus


def _message_lines(session):
    lines = []
    received = _get(session, "peer_last_recv")
    if received:
        sender = _get(received, "from_name")
        lines.append(f"{sender or '?'} ··→ {_tag(session)} {_age(received)}")
    sent = _get(session, "peer_last_sent")
    if sent:
        lines.append(f"{_tag(session)} ··→ {_get(sent, 'to_name') or '?'} {_age(sent)}")
    return lines


def obligation_lines(obligations):
    """P4: 남은 일 n · 담당 …, then one pair of lines per unfinished duty."""
    if obligations is None:
        return ["남은 일 ? · 읽지 못함"]
    items = list(obligations)
    named = _distinct(item.get("harness") for item in items if item.get("harness"))
    responsible = "/".join(part for part in (named, "미귀속" if any(not i.get("harness") for i in items) else "") if part)
    lines = [f"남은 일 {len(items)}" + (f" · 담당 {responsible}" if items else "")]
    for number, item in enumerate(items, 1):
        age = item.get("age_min")
        stamp = f"{age}분 전" if isinstance(age, int) else "시각 ?"
        owner = harness_abbr(item.get("harness")) if item.get("harness") else "미귀속"
        lines.append(f"{number}. {item.get('attempt_id') or item.get('id') or '?'}")
        lines.append(f"   담당 {owner} · {_CLEANUP.get(item.get('cleanup'), '정리 ?')} · "
                     f"{_DELIVERY.get(item.get('delivery'), '전달 ?')} · {stamp}")
    return lines


def build_overview(snapshot=None, focus_result=None, *, sid=None, steward_error=None):
    """Team lane, observation/message relations, succession and remaining work as plain lines."""
    focus_sid = sid or (getattr(getattr(focus_result, "key", None), "sid", None))
    relations, focus = _relation_lines(snapshot, focus_sid)
    lines = [TITLE]
    if focus is not None:
        lines.append(_lane(snapshot, focus))
    if steward_error:
        lines.append(f"관계 읽지 못함 · {steward_error}")
    lines.extend(relations)
    if focus is not None:
        lines.extend(_message_lines(focus))
        successor = _get(focus, "effective_parent")
        lines.append(f"승계 {successor}" if successor else "승계 ? · 확인 근거 없음")
    obligations = getattr(focus_result, "obligations", None)
    lines.extend(obligation_lines(obligations))
    delivery = _get(snapshot, "pending_delivery")
    if isinstance(delivery, Mapping) and (delivery.get("pending") or delivery.get("expired")):
        lines.append(f"전달 기록 대기 {delivery.get('pending', 0)} · 만료 {delivery.get('expired', 0)}")
    return tuple(lines)
