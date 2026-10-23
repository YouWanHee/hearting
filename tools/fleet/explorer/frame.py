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
        return str((repo or {}).get("name") or ident) + "/"
    if kind == "session":
        session = next((item for item in vm.sessions if str(item.get("tag")) == ident), None)
        if session:
            return f"{session.get('repo') or '·'}/ › [{ident}]"
    if kind == "route":
        route = next((item for item in vm.routes if str(item.get("id")) == ident), None)
        if route:
            parent = f"[{route['session_tag']}] › " if route.get("session_tag") else ""
            title = clip(str(route.get("title") or "·"), 32)
            return f"{route.get('repo') or '·'}/ › {parent}{route.get('tree_label') or ident} · {title}"
    return state.view


@dataclass(frozen=True)
class CellFrame:
    cells: tuple
    hits: tuple
    cols: int
    rows: int
    title: str = "OVERVIEW"
    status: str = ""

    def text_rows(self):
        return tuple("".join(cell[0] for cell in row) for row in self.cells)


def draw(view_model, nav_state=None, cols=140, rows=45, beat=1):
    """Draw without I/O, clocks, mutable globals, or terminal dependencies."""
    vm = ExplorerViewModel.from_mapping(view_model)
    from .state import NavState
    state = nav_state if isinstance(nav_state, NavState) else NavState.from_value(nav_state)
    state = replace(state, title=_scope_title(vm, state))
    grid = CellGrid(cols, rows)
    chrome.draw_explorer(grid, vm, state)
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
    # View content owns rows above the shared feed/key/status bands only. Mark
    # overflow and discard offscreen cells/hits before those shared bands draw.
    body_end = max(0, int(rows) - 4)
    pushed = False
    if state.usage_open and body_end > 3:
        # The expanded usage block pushes the view pane down two rows; the explorer
        # rail is drawn again at its own offset and keeps compute anchored at the foot.
        pane = NAV_WIDTH + 1
        prior = [list(row) for row in grid.cells]
        pushed = any(cell[0] != " " or cell[1] is not None or cell[2] is not None
                     for row in prior[body_end - 2:body_end] for cell in row[pane:])
        for row_index in range(3, body_end):
            for col in range(grid.cols):
                grid.cells[row_index][col] = ((" ", None, None) if row_index < 5 or col < pane
                                              else prior[row_index - 2][col])
        view_hits = [Hit(hit.y + 2, hit.x0, hit.x1, hit.action) if 3 <= hit.y else hit
                     for hit in grid.hits if hit.x0 >= pane or hit.y < 0]
        grid.hits = []
        chrome.draw_explorer(grid, vm, state)
        grid.hits.extend(view_hits)
    overflow = pushed or any(any(cell[0] != " " or cell[1] is not None or cell[2] is not None
                       for cell in row) for row in grid.cells[body_end:])
    grid.hits = [hit for hit in grid.hits if hit.y < body_end or hit.y == -1]
    for row_index in range(body_end + 1, grid.rows):
        grid.fill(0, grid.cols, row_index, " ")
    chrome.draw_chrome(grid, vm, state, beat)
    if overflow and body_end < grid.rows:
        grid.put(max(0, grid.cols - 18), body_end, " ↓ more below ", "dim", limit=grid.cols - 1)
    parent = "-"
    if state.scope.startswith("session:"):
        tag = state.scope.partition(":")[2]
        session = next((item for item in vm.sessions if str(item.get("tag")) == tag), None)
        repo = (session or {}).get("repo_id", (session or {}).get("repo"))
        if repo is not None:
            parent = "repo:" + str(repo)
    elif state.scope.startswith("route:"):
        ident = state.scope.partition(":")[2]
        route = next((item for item in vm.routes if str(item.get("id")) == ident), None)
        route = route or {}
        parent = str(route.get("session_tag") or ("repo:" + str(route.get("repo")) if route.get("repo")
                                                  else "-"))
    grid.hits.append(Hit(-1, 0, 0, ("parent", parent)))
    grid.add_hit(1, int(cols) - 9, int(cols), ("usage",))
    return CellFrame(grid.snapshot(), tuple(grid.hits), int(cols), int(rows),
                     _scope_title(vm, state), state.status)

