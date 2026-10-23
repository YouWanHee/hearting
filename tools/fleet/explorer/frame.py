"""Pure whole-screen draw orchestration for immutable explorer inputs."""
from dataclasses import dataclass

from .grid import CellGrid, Hit
from .view_model import ExplorerViewModel
from . import chrome, history, models, pages, peers, resources, tables


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
    overflow = any(any(cell[0] != " " or cell[1] is not None or cell[2] is not None
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
        parent = str(route.get("session_tag") or route.get("past_from") or
                     route.get("repo") or "-")
    grid.hits.append(Hit(-1, 0, 0, ("parent", parent)))
    grid.add_hit(1, int(cols) - 9, int(cols), ("usage",))
    return CellFrame(grid.snapshot(), tuple(grid.hits), int(cols), int(rows), state.title, state.status)

