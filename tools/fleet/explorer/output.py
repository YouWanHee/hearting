"""Pure, bounded display formatting for attempt summaries and output lines."""

from dataclasses import dataclass
import unicodedata
from typing import Iterable, Optional, Sequence, Tuple


@dataclass(frozen=True)
class OutputLine:
    text: str
    omitted_lines: int = 0


def cell_width(text: str) -> int:
    width = 0
    for char in text:
        code = ord(char)
        if (unicodedata.combining(char) or code in (0xFE0E, 0xFE0F, 0x200B, 0x200C, 0x200D, 0x2060)):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def clip(text: str, width: int, ellipsis: str = "…") -> str:
    if width <= 0:
        return ""
    if cell_width(text) <= width:
        return text
    available = max(0, width - cell_width(ellipsis))
    out = []
    used = 0
    for char in text:
        cells = cell_width(char)
        if used + cells > available:
            break
        out.append(char)
        used += cells
    return "".join(out) + (ellipsis if width >= cell_width(ellipsis) else "")


def render_output(records: Iterable[object], width: int, height: int, *, scroll: int = 0) -> Tuple[str, ...]:
    """Render records as one physical row each, then apply a viewport and cell-safe clipping."""
    rows = []
    for record in records:
        if isinstance(record, OutputLine):
            text, omitted = record.text, record.omitted_lines
        elif isinstance(record, str):
            text, omitted = record, 0
        else:
            text, omitted = str(getattr(record, "text", record)), int(getattr(record, "omitted_lines", 0) or 0)
        if omitted:
            text = f"{text} (+{omitted}줄)"
        rows.append(clip(text.replace("\n", " "), width))
    start = max(0, min(int(scroll), max(0, len(rows) - max(0, height))))
    return tuple(rows[start:start + max(0, height)])


def attempt_summary_lines(job):
    """Keep the existing dispatch summary fields available in PR-1 output."""
    def value(name):
        return job.get(name) if isinstance(job, dict) else getattr(job, name, None)
    fields = []
    for name, label in (("title", "작업"), ("summary", "요약"), ("stage", "단계"),
                        ("status", "상태"), ("liveness", "생존"),
                        ("summary_ts", "요약 시각")):
        item = value(name)
        if item not in (None, ""):
            fields.append(f"{label}: {item}")
    decision = value("attempt_decision")
    if isinstance(decision, dict):
        for key, label in (("outcome", "결과"), ("action", "다음"), ("responsible", "담당"), ("reason", "근거")):
            item = decision.get(key)
            if item:
                fields.append(f"{label}: {item}")
    return tuple(fields)
