"""Pure whole-screen draw orchestration for immutable explorer inputs."""
from dataclasses import dataclass, replace

from .grid import CellGrid, Hit
from .output import clip
from .view_model import ExplorerViewModel
from .chrome import NAV_WIDTH
from . import chrome, history, models, pages, peers, resources, tables


def _scope_title(vm, state):
    scope = str(state.scope or "-")
    kind, _, ident = scope.partition(":")
    if kind == "repo":
        repo = next((item for item in vm.repos
                     if str(item.get("id", item.get("name", ""))) == ident), None)
        return tables.repo_rule_label((repo or {}).get("name") or ident) or state.view
    if kind == "session":
        session = next((item for item in vm.sessions if str(item.get("tag")) == ident), None)
        if session:
            head = tables.repo_rule_label(session.get("repo"))
            return f"{head + ' › ' if head else ''}[{ident}]"
    if kind == "route":
        route = next((item for item in vm.routes if str(item.get("id")) == ident), None)
        if route:
            parent = f"[{route['session_tag']}] › " if route.get("session_tag") else ""
            title = clip(str(route.get("title") or "·"), 32)
            head = tables.repo_rule_label(route.get("repo"))
            return f"{head + ' › ' if head else ''}{parent}{route.get('tree_label') or ident} · {title}"
    return state.view


@dataclass(frozen=True)
class CellFrame:
    cells: tuple
    hits: tuple
    cols: int
    rows: int
    title: str = "OVERVIEW"
    status: str = ""
    pane_scroll: tuple = ()
    tree_scroll: tuple = ()

    def text_rows(self):
        return tuple("".join(cell[0] for cell in row) for row in self.cells)


class _PageRow:
    """List-like row view backed only when it falls inside the current page window."""

    def __init__(self, grid, y):
        self.grid, self.y = grid, int(y)

    def __getitem__(self, column):
        if isinstance(column, slice):
            return [self.grid._cell(self.y, x)
                    for x in range(*column.indices(self.grid.cols))]
        column = int(column)
        if column < 0:
            column += self.grid.cols
        return self.grid._cell(self.y, column)

    def __setitem__(self, column, value):
        if isinstance(column, slice):
            indexes = range(*column.indices(self.grid.cols))
            values = list(value)
            if len(indexes) != len(values):
                raise ValueError("page row slice assignment cannot change width")
            for x, cell in zip(indexes, values):
                self.grid._set_cell(self.y, x, cell)
            return
        column = int(column)
        if column < 0:
            column += self.grid.cols
        self.grid._set_cell(self.y, column, value)


class _PageRows:
    def __init__(self, grid):
        self.grid = grid
        self._last_y = None
        self._last_row = None

    def __getitem__(self, y):
        y = int(y)
        if y < 0:
            y += self.grid.rows
        if y < 0 or y >= self.grid.rows:
            raise IndexError(y)
        if y != self._last_y:
            self._last_y = y
            self._last_row = _PageRow(self.grid, y)
        return self._last_row


class _PageGrid(CellGrid):
    """Virtual-height page grid with bounded cell/hit storage.

    Renderers retain their existing logical row coordinates and bounds, while only the
    displayed slice and bottom overflow probe keep cells. End measurement tracks the
    final painted body row without materializing the intervening canvas.
    """

    def __init__(self, cols, rows, pane_x, keep_rows=(), complete_page=False):
        self.cols, self.rows = max(0, int(cols)), max(0, int(rows))
        self.pane_x = int(pane_x)
        self.probe_y = max(0, self.rows - 5)
        self.keep_start, self.keep_end = tuple(keep_rows) if keep_rows else (0, 0)
        self._cells = {}
        self.cells = _PageRows(self)
        self.hits = []
        self.trace = CellGrid.default_trace
        self.pane_clip = None
        self.complete_page = bool(complete_page)
        self.page_incomplete = False
        self.probe_overflow = False
        self.natural_page_end = None
        self.last_content_row = 2
        self.last_hit_row = 2

    def mark_page_incomplete(self):
        """Record renderer-side clipping that is not visible at the bottom probe."""
        self.page_incomplete = True

    def report_natural_page_end(self, y):
        y = int(y)
        self.natural_page_end = max(y, self.natural_page_end or 2)

    def _keeps(self, y):
        return y == self.probe_y or self.keep_start <= y < self.keep_end

    def _cell(self, y, x):
        if x < 0 or x >= self.cols:
            raise IndexError(x)
        row = self._cells.get(y)
        return row[x] if row is not None else (" ", None, None)

    def _set_cell(self, y, x, value):
        if y < 0 or y >= self.rows or x < 0 or x >= self.cols:
            return
        if (3 <= y < self.probe_y and x >= self.pane_x and
                (value[0] != " " or value[1] is not None or value[2] is not None)):
            self.last_content_row = max(self.last_content_row, y)
        if not self._keeps(y):
            return
        row = self._cells.get(y)
        if row is None:
            row = [(" ", None, None) for _ in range(self.cols)]
            self._cells[y] = row
        row[x] = value

    def add_hit(self, y, x0, x1, action):
        y, x0, x1 = int(y), int(x0), int(x1)
        if 0 <= y < self.rows and x0 < x1:
            left, right = max(0, x0), min(self.cols, x1)
            if left < right:
                if right > self.pane_x and 3 <= y < self.probe_y:
                    self.last_hit_row = max(self.last_hit_row, y)
                if self.keep_start <= y < self.keep_end:
                    self.hits.append(Hit(y, left, right, tuple(action)))


def _draw_page(grid, vm, state):
    if state.scope != "-":
        pages.draw_scope(grid, vm, state)
    elif state.view == "OVERVIEW":
        tables.draw_overview(grid, vm, state)
    elif state.view == "ROUTES":
        tables.draw_routes(grid, vm, state)
    elif state.view == "QUESTIONS":
        tables.draw_questions(grid, vm, state)
    elif state.view == "HISTORY":
        history.draw_history(grid, vm, state)
    elif state.view == "PEERS":
        peers.draw_peers(grid, vm, state)
    elif state.view == "COMPUTE":
        resources.draw_compute(grid, vm, state)
    elif state.view == "MEMORY":
        resources.draw_memory(grid, vm, state)
    elif state.view == "MODELS":
        models.draw_models(grid, vm, state)
    elif state.view == "SYSTEM":
        resources.draw_system(grid, vm, state)


def _page_canvas(vm, state, cols, logical_rows, pane_x, keep_rows=(), complete_page=False):
    grid = _PageGrid(cols, logical_rows, pane_x, keep_rows, complete_page)
    spill_y = max(0, int(logical_rows) - 5)
    for x in range(pane_x, grid.cols):
        grid.put(x, spill_y, "━", "line")
    baseline = tuple(grid.cells[spill_y][pane_x:])
    if grid.cols < 120:
        grid.pane_clip = (pane_x - 1, grid.cols - 1)
    _draw_page(grid, vm, state)
    grid.pane_clip = None
    grid.probe_overflow = tuple(grid.cells[spill_y][pane_x:]) != baseline
    return grid, grid.probe_overflow


def _page_end(grid, pane_x, logical_rows):
    """Last painted or actionable body row, excluding the bottom overflow probe."""
    limit = max(3, int(logical_rows) - 5)
    content = grid.last_content_row if 3 <= grid.last_content_row < limit else 2
    hits = grid.last_hit_row if 3 <= grid.last_hit_row < limit else 2
    return max(content, hits, grid.natural_page_end or 2)


def _page_max(grid, pane_x, logical_rows, viewport_rows, offset, overflow):
    if viewport_rows <= 0:
        return 0
    if grid.natural_page_end is not None:
        last = _page_end(grid, pane_x, logical_rows)
        return max(0, last - (3 + int(viewport_rows) - 1))
    if overflow:
        return max(0, int(offset)) + int(viewport_rows)
    last = _page_end(grid, pane_x, logical_rows)
    return max(0, last - (3 + int(viewport_rows) - 1))


def measure_pane_max(view_model, nav_state, cols, rows):
    """Measure a page's end only for the explicit End key; ordinary redraws stay viewport-sized."""
    vm = ExplorerViewModel.from_mapping(view_model)
    from .state import NavState
    state = nav_state if isinstance(nav_state, NavState) else NavState.from_value(nav_state)
    state = replace(state, title=_scope_title(vm, state))
    pushed = 2 if state.usage_open and rows - 2 > 7 else 0
    view_rows = int(rows) - pushed
    pane_x = NAV_WIDTH + 2
    body_end = max(0, int(rows) - 4)
    viewport_top = 4 + pushed
    viewport_rows = max(0, body_end - viewport_top)
    logical_rows = max(view_rows + viewport_rows, view_rows + 1)
    while True:
        grid, overflow = _page_canvas(vm, state, int(cols), logical_rows, pane_x)
        if grid.natural_page_end is not None:
            return _page_max(grid, pane_x, logical_rows, viewport_rows, 0, overflow)
        if not overflow and not grid.page_incomplete:
            return max(0, _page_end(grid, pane_x, logical_rows) -
                       (3 + viewport_rows - 1))
        logical_rows = max(logical_rows + viewport_rows, logical_rows * 2)


def draw(view_model, nav_state=None, cols=140, rows=45, beat=1):
    """Draw a viewport while retaining the full page row stream for keyboard and wheel scrolling."""
    vm = ExplorerViewModel.from_mapping(view_model)
    from .state import NavState
    state = nav_state if isinstance(nav_state, NavState) else NavState.from_value(nav_state)
    state = replace(state, title=_scope_title(vm, state))
    pushed_rows = 2 if state.usage_open and rows - 2 > 7 else 0
    view_rows = int(rows) - pushed_rows
    body_end = max(0, int(rows) - 4)
    content_top = 4 + pushed_rows
    viewport_rows = max(0, body_end - content_top)
    pane_x = NAV_WIDTH + 2
    offsets = dict(state.scroll)
    requested = max(0, int(offsets.get("pane", 0)))

    grid = CellGrid(cols, rows)
    tree_layout = chrome.draw_explorer(grid, vm, state)

    def render_page(offset, complete_page=False):
        logical_rows = max(1, view_rows + offset)
        shift = 1 + pushed_rows
        keep_start = max(0, content_top - shift + offset)
        keep_end = max(keep_start, body_end - shift + offset)
        page, overflow = _page_canvas(vm, state, int(cols), logical_rows, pane_x,
                                      (keep_start, keep_end), complete_page)
        overflow = overflow or page.page_incomplete
        maximum = _page_max(page, pane_x, logical_rows, viewport_rows, offset, overflow)
        return page, overflow, maximum, logical_rows

    page, overflow, pane_max, logical_rows = render_page(requested)
    effective = requested
    if page.natural_page_end is not None and effective > pane_max:
        effective = pane_max
        page, overflow, pane_max, logical_rows = render_page(effective)
    elif not overflow and effective > pane_max:
        effective = pane_max
        page, overflow, pane_max, logical_rows = render_page(effective)

    if (page.page_incomplete and page.natural_page_end is not None and
            effective >= pane_max):
        # At the natural end, use the row reserved for the renderer's continuation marker.
        page, overflow, pane_max, logical_rows = render_page(effective, complete_page=True)

    # Page renderers draw from row 3. The shared header owns row 3, so the page
    # starts one row lower; usage uses its existing two-row push as well.
    shift = 1 + pushed_rows
    for dest_y in range(content_top, body_end):
        source_y = dest_y - shift + effective
        if 0 <= source_y < page.rows:
            grid.cells[dest_y][pane_x:] = page.cells[source_y][pane_x:]
            edge = grid.cells[dest_y][pane_x - 1]
            first = grid.cells[dest_y][pane_x]
            if edge[2] is None and first[2] == 236:
                grid.cells[dest_y][pane_x - 1] = (edge[0], edge[1], first[2])
    for hit in page.hits:
        if hit.y < 3 or hit.x1 <= pane_x:
            continue
        screen_y = hit.y + shift - effective
        if content_top <= screen_y < body_end:
            grid.hits.append(Hit(screen_y, hit.x0, hit.x1, hit.action))
    grid.hits = [hit for hit in grid.hits if hit.y < body_end or hit.y == -1]
    chrome.draw_chrome(grid, vm, state, beat)
    if page.probe_overflow and body_end < grid.rows:
        grid.put(max(0, grid.cols - 18), body_end, " ↓ more below ", "dim", limit=grid.cols - 1)
    parent = "-"
    if state.scope.startswith("session:"):
        tag = state.scope.partition(":")[2]
        session = next((item for item in vm.sessions if str(item.get("tag")) == tag), None)
        repo = (session or {}).get("repo_id", (session or {}).get("repo"))
        if tables.repo_label(repo):
            parent = "repo:" + str(repo)
    elif state.scope.startswith("route:"):
        ident = state.scope.partition(":")[2]
        route = next((item for item in vm.routes if str(item.get("id")) == ident), None)
        route = route or {}
        parent = str(route.get("session_tag") or ("repo:" + str(route.get("repo")) if route.get("repo")
                                                  else "-"))
    grid.hits.append(Hit(-1, 0, 0, ("parent", parent)))
    grid.add_hit(1, int(cols) - 9, int(cols), ("usage",))
    pane_layout = (pane_x, content_top, int(cols), body_end, effective, pane_max,
                   viewport_rows, bool(overflow))
    return CellFrame(grid.snapshot(), tuple(grid.hits), int(cols), int(rows),
                     _scope_title(vm, state), state.status, pane_layout, tree_layout)

