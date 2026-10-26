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


def _page_canvas(vm, state, cols, logical_rows, pane_x):
    grid = CellGrid(cols, logical_rows)
    spill_y = max(0, int(logical_rows) - 5)
    for x in range(pane_x, grid.cols):
        grid.put(x, spill_y, "━", "line")
    baseline = tuple(grid.cells[spill_y][pane_x:])
    if grid.cols < 120:
        grid.pane_clip = (pane_x - 1, grid.cols - 1)
    _draw_page(grid, vm, state)
    grid.pane_clip = None
    overflow = tuple(grid.cells[spill_y][pane_x:]) != baseline
    return grid, overflow


def _page_end(grid, pane_x, logical_rows):
    """Last painted or actionable body row, excluding the bottom overflow probe."""
    limit = max(3, int(logical_rows) - 5)
    hit_rows = [hit.y for hit in grid.hits if 3 <= hit.y < limit and hit.x1 > pane_x]
    for y in range(limit - 1, 2, -1):
        if any(cell[0] != " " or cell[1] is not None or cell[2] is not None
               for cell in grid.cells[y][pane_x:]):
            return max(y, max(hit_rows, default=2))
    return max(hit_rows, default=2)


def _page_max(grid, pane_x, logical_rows, viewport_rows, offset, overflow):
    if viewport_rows <= 0:
        return 0
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
        if not overflow:
            return max(0, _page_end(grid, pane_x, logical_rows) -
                       (3 + viewport_rows - 1))
        if logical_rows >= 8192:
            return max(0, logical_rows - view_rows)
        logical_rows = min(8192, max(logical_rows + viewport_rows, logical_rows * 2))


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
    if requested >= (1 << 30):
        requested = measure_pane_max(vm, state, cols, rows)

    grid = CellGrid(cols, rows)
    tree_layout = chrome.draw_explorer(grid, vm, state)

    def render_page(offset):
        logical_rows = max(1, view_rows + offset)
        page, overflow = _page_canvas(vm, state, int(cols), logical_rows, pane_x)
        maximum = _page_max(page, pane_x, logical_rows, viewport_rows, offset, overflow)
        return page, overflow, maximum, logical_rows

    page, overflow, pane_max, logical_rows = render_page(requested)
    effective = requested
    if not overflow and effective > pane_max:
        effective = pane_max
        page, overflow, pane_max, logical_rows = render_page(effective)

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
    if overflow and body_end < grid.rows:
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

