#!/usr/bin/env python3
"""Frame interview: the questions a person answers at the `frame-review` gate,
and the intent document their answers produce (SD-129).

The owner writes `shards/frame/interview.json` after the frame group joins and
raises the gate with it as the reviewable artifact. The depth-0 session puts
the questions to the user one topic at a time, records the answers with
`workflow-supervisor.py release --decision proceed --answers <file>`, and the
owner renders `shards/frame/intent.md` from interview + answers before `plan`
starts. `plan` reads the intent document as its brief.

The acceptance bar is the user's own sentence (2026-09-06): "핵심은 이해하기
쉽게 사용자에게 조사를 하고 물어봐야 해". So the validator refuses, before
the gate is raised, an interview a tired reader could not answer without
reading the plan: harness vocabulary, long questions, more than one topic per
question, a question with no recommended answer, or more questions than the
intensity allows. Facts a tool can establish are not questions -- the owner
investigates them (grill-me rule) -- so every question carries `why`: the
reason only the user can decide it.

    frame_interview.py validate      --interview F --intensity I
    frame_interview.py answers-template --interview F
    frame_interview.py validate-answers --interview F --answers A
    frame_interview.py render-intent --interview F --answers A --out intent.md
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "frame_interview_v1"
ANSWERS_SCHEMA = "frame_interview_answers_v1"

# Questions per raise, by intensity. `direct`/`quick` have no gate; their caps
# govern the inline interview the depth-0 session runs inside the §0.4 card.
QUESTION_CAP = {
    "direct": 1, "quick": 3,
    "standard": 7, "strong": 7, "thorough": 7, "adversarial": 7,
}
MAX_ROUNDS = 2                 # raises per route that may carry an interview
MAX_QUESTION_CHARS = 160
MAX_SENTENCES = 2
MAX_OPTION_LABEL_CHARS = 40
MAX_OPTION_MEANS_CHARS = 120
MAX_TOPIC_CHARS = 40
MAX_WHY_CHARS = 140
MAX_UNDERSTANDING_CHARS = 220
MAX_BRIEF_FIELD_CHARS = 500
MIN_OPTIONS, MAX_OPTIONS = 2, 4
KINDS = ("yes-no", "choice")
BRIEF_FIELDS = ("problem", "outcome", "affected", "constraints", "open")

# Harness vocabulary a user should never have to decode. Matched as whole
# words, case-insensitively, in question/option/understanding/brief text.
JARGON = (
    "route", "routes", "dispatch", "dispatched", "owner", "attempt", "attempts",
    "worker", "workers", "gate", "gates", "ledger", "supervisor", "node", "nodes",
    "shard", "shards", "intensity", "harness", "carrier", "marker", "receipt",
    "registry", "depth", "topology", "recipe", "frame-review", "plan-check",
    "impl-review", "parallel group", "worktree", "hook", "hooks", "sidecar",
    "quiescent", "asyncrewake", "sweep", "envelope", "pipeline", "conductor",
    "라우트", "디스패치", "오너", "어템프트", "워커", "게이트", "레저", "슈퍼바이저",
    "노드", "샤드", "하네스", "캐리어", "마커", "리시트", "레지스트리", "토폴로지",
    "워크트리", "훅", "사이드카", "파이프라인", "컨덕터",
)
JARGON_IDS = re.compile(r"\b(?:SD-\d+|rt-[0-9a-f]{6,}|att-[0-9a-f]{6,}|cyc_[0-9a-f]{6,}|camp_[0-9a-f]{6,}|rrev_[0-9a-f]{6,})\b", re.I)
_JARGON_PATTERNS = [re.compile(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])", re.I) for term in JARGON]
_SENTENCE_END = re.compile(r"[.!?。？！]+(?:\s|$)")


class InterviewError(ValueError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def jargon_hits(text: str) -> list[str]:
    hits = [term for term, pattern in zip(JARGON, _JARGON_PATTERNS) if pattern.search(text)]
    hits += [m.group(0) for m in JARGON_IDS.finditer(text)]
    return hits


def _sentences(text: str) -> int:
    parts = [p for p in _SENTENCE_END.split(text.strip()) if p.strip()]
    return max(1, len(parts)) if text.strip() else 0


def is_interview(value) -> bool:
    return isinstance(value, dict) and value.get("schema") == SCHEMA


def question_cap(intensity: str) -> int:
    return QUESTION_CAP.get(str(intensity or "").strip(), QUESTION_CAP["standard"])


def validate(interview: dict, *, intensity: str = "standard") -> list[str]:
    """Every reason this interview may not be put in front of a person. Empty
    means it may. Never raises on shape -- the reasons are the output."""

    errors: list[str] = []
    if not is_interview(interview):
        return [f"schema: expected {SCHEMA!r}"]
    if not _text(interview.get("route_id")):
        errors.append("route_id: missing")
    if not _text(interview.get("summary")):
        errors.append("summary: path to frame-summary.json missing")
    understanding = _text(interview.get("understanding")).strip()
    if not understanding:
        errors.append("understanding: the owner's one-sentence restatement is missing")
    else:
        if len(understanding) > MAX_UNDERSTANDING_CHARS:
            errors.append(f"understanding: {len(understanding)} chars > {MAX_UNDERSTANDING_CHARS}")
        if _sentences(understanding) > 1:
            errors.append("understanding: must be one sentence")
        for hit in jargon_hits(understanding):
            errors.append(f"understanding: harness word {hit!r}")
    brief = interview.get("brief")
    if not isinstance(brief, dict):
        errors.append("brief: missing (problem/outcome/affected/constraints/open)")
    else:
        for field in BRIEF_FIELDS:
            text = _text(brief.get(field)).strip()
            if not text and field != "open":
                errors.append(f"brief.{field}: missing")
            if len(text) > MAX_BRIEF_FIELD_CHARS:
                errors.append(f"brief.{field}: {len(text)} chars > {MAX_BRIEF_FIELD_CHARS}")
            for hit in jargon_hits(text):
                errors.append(f"brief.{field}: harness word {hit!r}")
    questions = interview.get("questions")
    if not isinstance(questions, list):
        errors.append("questions: must be a list (empty is allowed)")
        questions = []
    cap = question_cap(intensity)
    if len(questions) > cap:
        errors.append(f"questions: {len(questions)} > cap {cap} for intensity {intensity!r}")
    seen: set[str] = set()
    topics: set[str] = set()
    for index, question in enumerate(questions):
        where = f"questions[{index}]"
        if not isinstance(question, dict):
            errors.append(f"{where}: not an object")
            continue
        qid = _text(question.get("id")).strip()
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", qid or ""):
            errors.append(f"{where}.id: missing or not a short slug")
        elif qid in seen:
            errors.append(f"{where}.id: duplicate {qid!r}")
        seen.add(qid)
        topic = _text(question.get("topic")).strip()
        if not topic:
            errors.append(f"{where}.topic: missing")
        elif len(topic) > MAX_TOPIC_CHARS:
            errors.append(f"{where}.topic: {len(topic)} chars > {MAX_TOPIC_CHARS}")
        elif topic.lower() in topics:
            errors.append(f"{where}.topic: {topic!r} already asked -- one topic, one question")
        topics.add(topic.lower())
        text = _text(question.get("question")).strip()
        if not text:
            errors.append(f"{where}.question: missing")
        else:
            if len(text) > MAX_QUESTION_CHARS:
                errors.append(f"{where}.question: {len(text)} chars > {MAX_QUESTION_CHARS}")
            if _sentences(text) > MAX_SENTENCES:
                errors.append(f"{where}.question: more than {MAX_SENTENCES} sentences")
            if " and " in f" {text.lower()} " and text.count("?") > 1:
                errors.append(f"{where}.question: asks two things at once")
            for hit in jargon_hits(text):
                errors.append(f"{where}.question: harness word {hit!r}")
        kind = _text(question.get("kind"))
        if kind not in KINDS:
            errors.append(f"{where}.kind: {kind!r} not in {KINDS}")
        options = question.get("options")
        if not isinstance(options, list) or not (MIN_OPTIONS <= len(options) <= MAX_OPTIONS):
            errors.append(f"{where}.options: need {MIN_OPTIONS}-{MAX_OPTIONS} options")
            options = options if isinstance(options, list) else []
        elif kind == "yes-no" and len(options) != 2:
            errors.append(f"{where}.options: a yes-no question has exactly 2 options")
        for oindex, option in enumerate(options):
            owhere = f"{where}.options[{oindex}]"
            if not isinstance(option, dict):
                errors.append(f"{owhere}: not an object")
                continue
            label = _text(option.get("label")).strip()
            means = _text(option.get("means")).strip()
            if not label:
                errors.append(f"{owhere}.label: missing")
            elif len(label) > MAX_OPTION_LABEL_CHARS:
                errors.append(f"{owhere}.label: {len(label)} chars > {MAX_OPTION_LABEL_CHARS}")
            if not means:
                errors.append(f"{owhere}.means: say in one line what choosing it does")
            elif len(means) > MAX_OPTION_MEANS_CHARS:
                errors.append(f"{owhere}.means: {len(means)} chars > {MAX_OPTION_MEANS_CHARS}")
            for hit in jargon_hits(label + " " + means):
                errors.append(f"{owhere}: harness word {hit!r}")
        recommended = question.get("recommended")
        if not isinstance(recommended, int) or isinstance(recommended, bool) \
                or not (0 <= recommended < max(len(options), 1)):
            errors.append(f"{where}.recommended: must index one option -- every question carries a recommended answer")
        why = _text(question.get("why")).strip()
        if not why:
            errors.append(f"{where}.why: say why only the user can decide this (a fact a tool can find is not a question)")
        elif len(why) > MAX_WHY_CHARS:
            errors.append(f"{where}.why: {len(why)} chars > {MAX_WHY_CHARS}")
        for hit in jargon_hits(why):
            errors.append(f"{where}.why: harness word {hit!r}")
    round_no = interview.get("round", 1)
    if not isinstance(round_no, int) or isinstance(round_no, bool) or not (1 <= round_no <= MAX_ROUNDS):
        errors.append(f"round: must be 1..{MAX_ROUNDS}")
    return errors


def answers_template(interview: dict) -> dict:
    """What the depth-0 session fills in after asking: one entry per question,
    plus whether the owner's restatement was confirmed."""

    return {
        "schema": ANSWERS_SCHEMA,
        "route_id": interview.get("route_id"),
        "round": interview.get("round", 1),
        "understanding_confirmed": None,
        "correction": "",
        "answers": {
            _text(q.get("id")): {"choice": None, "note": ""}
            for q in interview.get("questions", []) if isinstance(q, dict)
        },
    }


def validate_answers(interview: dict, answers: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(answers, dict) or answers.get("schema") != ANSWERS_SCHEMA:
        return [f"schema: expected {ANSWERS_SCHEMA!r}"]
    if answers.get("route_id") != interview.get("route_id"):
        errors.append("route_id: answers do not belong to this interview")
    if answers.get("round", 1) != interview.get("round", 1):
        errors.append("round: answers belong to a different round")
    confirmed = answers.get("understanding_confirmed")
    if confirmed not in (True, False):
        errors.append("understanding_confirmed: must be true or false -- the user confirms the restatement")
    elif confirmed is False and not _text(answers.get("correction")).strip():
        errors.append("correction: say in the user's words what the owner got wrong")
    given = answers.get("answers")
    if not isinstance(given, dict):
        return errors + ["answers: must map question id -> {choice, note}"]
    questions = {_text(q.get("id")): q for q in interview.get("questions", []) if isinstance(q, dict)}
    for qid in given:
        if qid not in questions:
            errors.append(f"answers.{qid}: no such question")
    for qid, question in questions.items():
        entry = given.get(qid)
        if not isinstance(entry, dict):
            errors.append(f"answers.{qid}: missing")
            continue
        choice = entry.get("choice")
        options = question.get("options") if isinstance(question.get("options"), list) else []
        labels = [_text(o.get("label")) for o in options if isinstance(o, dict)]
        if isinstance(choice, bool) or not isinstance(choice, int) or not (0 <= choice < len(labels)):
            if isinstance(choice, str) and choice in labels:
                entry["choice"] = labels.index(choice)
            else:
                errors.append(f"answers.{qid}.choice: must index one of {labels}")
    return errors


def render_intent(interview: dict, answers: dict, *, now: str | None = None) -> str:
    """The agreed intent document `plan` reads first. Plain sections in the
    order intent.md uses (Problem, Proposed Outcome, Affected, Constraints,
    Decisions, Open Questions), every decision traceable to its question."""

    stamp = now or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    brief = interview.get("brief") if isinstance(interview.get("brief"), dict) else {}
    given = answers.get("answers") if isinstance(answers.get("answers"), dict) else {}
    confirmed = answers.get("understanding_confirmed") is True
    lines = [
        "---",
        "status: agreed" if confirmed else "status: agreed-with-correction",
        f"created: {stamp}",
        f"route_id: {interview.get('route_id', '-')}",
        f"round: {interview.get('round', 1)}",
        f"schema: frame_intent_v1",
        "---",
        "",
        "# Intent",
        "",
        "## Confirmed understanding",
        "",
        _text(interview.get("understanding")).strip() or "-",
    ]
    if not confirmed:
        lines += ["", "**User's correction:** " + (_text(answers.get("correction")).strip() or "-")]
    section = {
        "problem": "Problem", "outcome": "Proposed Outcome",
        "affected": "Affected Users / Systems", "constraints": "Constraints",
    }
    for field, title in section.items():
        lines += ["", f"## {title}", "", _text(brief.get(field)).strip() or "-"]
    lines += ["", "## Decisions", ""]
    questions = [q for q in interview.get("questions", []) if isinstance(q, dict)]
    if not questions:
        lines.append("No question needed a decision from the user; the direction above stands as proposed.")
    for question in questions:
        qid = _text(question.get("id"))
        entry = given.get(qid) if isinstance(given.get(qid), dict) else {}
        options = [o for o in question.get("options", []) if isinstance(o, dict)]
        choice = entry.get("choice")
        chosen = options[choice] if isinstance(choice, int) and 0 <= choice < len(options) else None
        recommended = question.get("recommended")
        followed = isinstance(choice, int) and choice == recommended
        lines.append(f"- **{_text(question.get('topic'))}** (`{qid}`): {_text(question.get('question')).strip()}")
        if chosen is not None:
            tag = "recommended" if followed else "user's own choice"
            lines.append(f"  - Decision: **{_text(chosen.get('label'))}** ({tag}) — {_text(chosen.get('means')).strip()}")
        else:
            lines.append("  - Decision: unanswered")
        note = _text(entry.get("note")).strip()
        if note:
            lines.append(f"  - User's note: {note}")
    lines += ["", "## Open Questions", "", _text(brief.get("open")).strip() or "None recorded."]
    lines += ["", "## Sources", "", f"- interview: {interview.get('self_path', 'shards/frame/interview.json')}",
              f"- summary: {_text(interview.get('summary')) or '-'}", ""]
    return "\n".join(lines)


def _load(path: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InterviewError("interview-unreadable", f"{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InterviewError("interview-unreadable", f"{path}: not an object")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="frame_interview")
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate"); v.add_argument("--interview", required=True); v.add_argument("--intensity", default="standard")
    t = sub.add_parser("answers-template"); t.add_argument("--interview", required=True)
    va = sub.add_parser("validate-answers"); va.add_argument("--interview", required=True); va.add_argument("--answers", required=True)
    r = sub.add_parser("render-intent"); r.add_argument("--interview", required=True); r.add_argument("--answers", required=True); r.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    interview = _load(args.interview)
    if args.command == "validate":
        errors = validate(interview, intensity=args.intensity)
        print(json.dumps({"valid": not errors, "errors": errors,
                          "questions": len(interview.get("questions") or []),
                          "cap": question_cap(args.intensity)}, ensure_ascii=False))
        return 0 if not errors else 65
    if args.command == "answers-template":
        print(json.dumps(answers_template(interview), ensure_ascii=False, indent=2))
        return 0
    answers = _load(args.answers)
    errors = validate_answers(interview, answers)
    if args.command == "validate-answers":
        print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False))
        return 0 if not errors else 65
    if errors:
        print(json.dumps({"valid": False, "errors": errors}, ensure_ascii=False), file=sys.stderr)
        return 65
    interview_errors = validate(interview, intensity=str(interview.get("intensity") or "standard"))
    if interview_errors:
        print(json.dumps({"valid": False, "errors": interview_errors}, ensure_ascii=False), file=sys.stderr)
        return 65
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(render_intent(interview, answers), encoding="utf-8")
    tmp.replace(out)
    print(json.dumps({"intent": str(out), "questions": len(interview.get("questions") or [])}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InterviewError as exc:
        print(f"frame_interview: {exc}", file=sys.stderr)
        raise SystemExit(64)
