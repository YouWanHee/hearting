"""Cell-accurate drawing surface and ordered hit-map geometry."""
from __future__ import annotations

from dataclasses import dataclass
from .style import normalize_style


def cell_width(char):
    if not char:
        return 0
    from .output import cell_width as existing_width
    return existing_width(char)


@dataclass(frozen=True)
class Hit:
    y: int
    x0: int
    x1: int
    action: tuple


class CellGrid:
    def __init__(self, cols, rows, background=None):
        self.cols, self.rows = max(0, int(cols)), max(0, int(rows))
        self.cells = [[(" ", None, background) for _ in range(self.cols)] for _ in range(self.rows)]
        self.hits = []

    def put(self, x, y, text, key=None, tint=None, limit=None):
        x, y = int(x), int(y)
        if y < 0 or y >= self.rows:
            return x
        right = self.cols if limit is None else min(self.cols, int(limit))
        style = normalize_style(key, tint)
        for char in str(text):
            width = cell_width(char)
            if width <= 0:
                continue
            if x + width > right:
                break
            if x >= 0:
                self.cells[y][x] = (char, style[0], style[1])
                for offset in range(1, width):
                    self.cells[y][x + offset] = ("", style[0], style[1])
            x += width
        return x

    def fill(self, x0, x1, y, glyph=" ", key=None, tint=None):
        y = int(y)
        if y < 0 or y >= self.rows:
            return
        lo, hi = max(0, int(x0)), min(self.cols, int(x1))
        style = normalize_style(key, tint)
        for x in range(lo, hi):
            self.cells[y][x] = (glyph, style[0], style[1])

    def add_hit(self, y, x0, x1, action):
        y, x0, x1 = int(y), int(x0), int(x1)
        if 0 <= y < self.rows and x0 < x1:
            left, right = max(0, x0), min(self.cols, x1)
            if left < right:
                self.hits.append(Hit(y, left, right, tuple(action)))

    def replace_hits(self, hits):
        self.hits = []
        for y, x0, x1, action in hits:
            self.add_hit(y, x0, x1, action)

    def rows_text(self):
        return tuple("".join(cell[0] for cell in row) for row in self.cells)

    def snapshot(self):
        return tuple(tuple(row) for row in self.cells)


Grid = CellGrid
