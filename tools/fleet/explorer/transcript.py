"""Pure text layout helpers for native style history cards and transcripts."""
from __future__ import annotations

import re
from .grid import cell_width


def wrap_spans(spans, width):
    rows, current, used = [], [], 0
    width = max(1, int(width))
    for text, key in spans:
        part = ""
        for char in str(text):
            cells = cell_width(char)
            if used + cells > width and (current or part):
                if part:
                    current.append((part, key))
                    part = ""
                rows.append(current)
                current, used = [], 0
            if char == " " and not current and not part and rows:
                continue
            part += char
            used += cells
        if part:
            current.append((part, key))
    return rows + ([current] if current else [[]])


def markdown_spans(text, base="soft"):
    spans = []
    for index, segment in enumerate(re.split(r"(`[^`]+`)", str(text))):
        if not segment:
            continue
        if segment.startswith("`") and segment.endswith("`"):
            spans.append((segment[1:-1], "code"))
            continue
        pieces = segment.split("**")
        for offset, piece in enumerate(pieces):
            if piece:
                spans.append((piece, "bold" if offset % 2 else base))
    return spans


_LONG_ID = re.compile(r"\b(?:rrev|ref|rt|att|tidy|ss|ssc|input|delivery)[-_][0-9A-Za-z_-]{6,}\b|\b[0-9a-f]{12,}\b")


def card_spans(text, base="soft"):
    result = []
    for value, key in markdown_spans(text, base):
        if key == "code":
            result.append((value, key))
            continue
        for index, part in enumerate(_LONG_ID.split(value)):
            if part:
                result.append((part, "dim" if index % 2 else key))
    return result


def card_rows(lines, width):
    """Return wrapped (segments, tint) rows for the approved markdown card rules."""
    result = []
    for line in lines:
        line = str(line)
        if not line.strip():
            continue
        heading = re.match(r"^(#{1,3})\s+(.*)$", line)
        if heading:
            level = len(heading.group(1))
            if level == 2 and result:
                result.append(([], None))
            key = {1: "v", 2: "c", 3: "head"}[level]
            result.extend((row, None) for row in wrap_spans(card_spans(heading.group(2), key), width))
            continue
        item = re.match(r"^(\s*)([-*]|\d+[.)])\s+(.*)$", line)
        if item:
            depth = len(item.group(1).expandtabs(2)) // 2
            mark = ("•", "◦", "·")[min(depth, 2)] if item.group(2) in ("-", "*") else item.group(2)
            lead = "  " * depth + mark + " "
            hang = " " * cell_width(lead)
            body = wrap_spans(card_spans(item.group(3), "soft"), max(8, width - cell_width(lead)))
            for index, row in enumerate(body):
                result.append((([(lead, "dim")] if index == 0 else [(hang, None)]) + row, None))
            continue
        result.extend((row, None) for row in wrap_spans(card_spans(line.strip(), "soft"), width))
    return result


def dialogue_rows(lines, width, harness="claude"):
    """Render structured or legacy dialogue records with harness-native prompt marks."""
    marks = {"claude": (">", "●", "⎿"), "codex": ("›", "•", "└"),
             "opencode": (">", "●", "⎿")}.get(str(harness), (">", "●", "⎿"))
    user_mark, agent_mark, elbow = marks
    role_pattern = re.compile(r"^(user|assistant|thinking|tool output|tool [^:]{1,32}|description|attachment|runtime_wait|supervisor): ?(.*)$")
    events, current = [], None
    for item in lines:
        if isinstance(item, dict):
            head, body = str(item.get("role", "assistant")), str(item.get("text", ""))
        else:
            raw = str(item)
            match = role_pattern.match(raw)
            head, body = match.groups() if match else ("", raw)
        if not head:
            if current is None:
                current = {"kind": "assistant", "text": [], "out": None, "cont": True}
                events.append(current)
            target = current["out"] if current["kind"] == "tool" and current["out"] is not None else current["text"]
            target.append(body)
            continue
        if current and current["kind"] == "tool" and current["out"] is None and head in ("description", "tool output"):
            if head == "description":
                current["desc"] = body
            else:
                current["out"] = [body]
            continue
        if head.startswith("tool ") and head != "tool output":
            current = {"kind": "tool", "name": head[5:], "text": [body] if body else [], "out": None}
        else:
            current = {"kind": head, "text": [body], "out": None}
        events.append(current)

    rows = []

    def paragraph(prefix, texts, base, tint=None):
        first = True
        indent = " " * sum(cell_width(text) for text, _key in prefix)
        for text in texts:
            wrapped = wrap_spans(markdown_spans(text, base), max(8, width - cell_width(indent)))
            for row in wrapped:
                rows.append(((prefix if first else [(indent, None)]) + row, tint))
                first = False

    for event in events:
        kind = event["kind"]
        if kind in ("attachment", "description"):
            continue
        if rows:
            rows.append(([], None))
        if kind == "user":
            paragraph([(user_mark + " ", "dim")], event["text"], "v", 234)
        elif kind == "assistant":
            prefix = [("  ", None)] if event.get("cont") else [(agent_mark + " ", "soft")]
            paragraph(prefix, event["text"], "soft")
        elif kind == "thinking":
            rows.append(([('✻ Thinking…', "dim")], None))
        elif kind == "tool":
            output = [str(value) for value in (event.get("out") or [])]
            for index, value in enumerate(output[:3]):
                prefix = "  " + elbow + "  " if index == 0 else "     "
                rows.extend(([(prefix, "dim")] + row, None) for row in
                            wrap_spans([(_strip_token(value), "dim")], max(8, width - 5)))
            if len(output) > 3:
                rows.append(([('     … +%d lines' % (len(output) - 3), "dim")], None))
        else:
            paragraph([(agent_mark + " ", "dim")], event["text"], "dim")
    return rows


def _strip_token(text):
    return text if text.strip() not in ("<persisted-output>", "</persisted-output>") else ""
