"""Read-only COMPUTE, MEMORY, and SYSTEM projections for Fleet explorer."""
from collections.abc import Mapping

from .grid import CellGrid
from .output import clip


def _table_head(grid, x, width, y, columns):
    grid.fill(x, min(grid.cols - 1, x + width + 1), y, " ", tint=234)
    for offset, label in columns:
        grid.put(x + offset, y, label, "head", tint=234)
    return y + 1


def _group_sep(grid, x, width, y):
    for column in range(x + 1, min(grid.cols, x + width + 1)):
        grid.put(column, y, "┈", "line")
    return y + 1


def _pct_key(value):
    if value is None:
        return "dim"
    return "lvl_r" if value >= 80 else "lvl_y" if value >= 50 else "lvl_g"


def _gauge(grid, x, y, width, value, tint=None):
    if value is None:
        grid.put(x, y, "·" * width, "dim", tint=tint)
        return
    pct = max(0, min(100, value))
    filled = width if pct >= 100 else 0 if pct <= 0 else max(1, min(width - 1, int(width * pct / 100 + .5)))
    grid.put(x, y, "━" * filled, _pct_key(value), tint=tint)
    grid.put(x + filled, y, "─" * (width - filled), "dim", tint=tint)


def draw_compute(grid: CellGrid, vm, state=None, x=34, y=3):
    """Display collected host/GPU facts; absent measurements stay visibly unknown."""
    use = max(0, grid.cols - x - 2)
    hosts = tuple(vm.compute or ())
    y = _table_head(grid, x, use, y, ((1, "HOST"), (11, "GPU"), (16, "MODEL"),
                                      (31, "UTIL"), (38, "VRAM"), (64, "USED BY")))
    labels = tuple(vm.hosts or ())
    for host_index, record in enumerate(hosts):
        if host_index:
            y = _group_sep(grid, x, use, y)
        try:
            host, base, cpu, mem, swap, gpus = record
        except (TypeError, ValueError):
            host, base, cpu, mem, swap, gpus = "·", False, None, None, None, ()
        if host_index < len(labels) and isinstance(labels[host_index], Mapping):
            host = labels[host_index].get("name", host)
            base = labels[host_index].get("key") == "base_host"
        for gpu_index, gpu in enumerate(gpus or ()):
            idx, status, model, model_key, util, used, total, users = (tuple(gpu) + (None,) * 8)[:8]
            if gpu_index == 0:
                grid.put(x + 1, y, host, "work" if base else "soft")
            mark, status_key = (("●", "spin") if status == "working" else
                                {"idle": ("●", "idle"), "done": ("✓", "done"),
                                 "dead": ("✕", "dead")}.get(status, ("·", "dim")))
            grid.put(x + 11, y, mark, status_key)
            grid.put(x + 13, y, "·" if idx is None else str(idx), "dim")
            gpu_styles = {"gpu_ada": "h_claude", "gpu_5090": "g_work_off",
                          "gpu_4090": "h_claude", "gpu_legacy": "unknown"}
            grid.put(x + 16, y, "·" if model is None else str(model), gpu_styles.get(model_key, model_key or "dim"))
            grid.put(x + 31, y, " · " if util is None else f"{int(util):3d}%",
                     "soft" if status == "working" else "dim")
            if used is None or total in (None, 0):
                _gauge(grid, x + 38, y, 10, None)
                grid.put(x + 51, y, "·", "dim")
            else:
                percent = 100 * float(used) / float(total)
                _gauge(grid, x + 38, y, max(1, round(float(total) / 4)), percent)
                grid.put(x + 51, y, (f"{used:g}/{total:g} GB").rjust(10), "dim")
            cursor = x + 64
            for user in users or ():
                cursor = grid.put(cursor, y, "[", "dim")
                cursor = grid.put(cursor, y, user, "soft")
                cursor = grid.put(cursor, y, "]", "dim") + 1
            y += 1
        if cpu or mem or swap:
            for label, values, width, unit in (("CPU", cpu, 10, "thr"), ("MEM", mem, 8, "GB"),
                                               ("SWP", swap, 8, "GB")):
                cursor = grid.put(x + 11 if label == "CPU" else cursor + 3, y, label + " ", "label")
                if values and len(values) >= 2 and values[1]:
                    amount = 100 * float(values[0]) / float(values[1])
                    _gauge(grid, cursor, y, width, amount)
                    cursor += width + 1
                    value = f"{values[0]:g}/{values[1]:g} {unit}"
                else:
                    _gauge(grid, cursor, y, width, None)
                    cursor += width + 1
                    value = "·"
                cursor = grid.put(cursor, y, value, "dim")
            y += 1
    if not hosts:
        grid.put(x + 1, y, "·", "dim")
    busy = sum(1 for host in hosts for gpu in (host[5] if len(host) > 5 else ())
               if len(gpu) > 1 and gpu[1] == "working")
    total = sum(len(host[5]) for host in hosts if len(host) > 5)
    idle_hosts = sum(1 for host in hosts if len(host) > 5 and host[5]
                     and all(len(gpu) > 1 and gpu[1] == "idle" for gpu in host[5]))
    grid.put(x + 1, y, f"{len(hosts)}/{len(hosts)} hosts · GPU {busy}/{total} working · {idle_hosts} host idle", "dim")
    y += 2
    data = vm.system if isinstance(vm.system, Mapping) else {}
    extras = tuple(data.get("unregistered_gpu_processes") or ())
    if extras:
        summary = data.get("unregistered_gpu_summary") or {}
        cursor = grid.put(x, y, "── ", "line")
        cursor = grid.put(cursor, y, "unregistered GPU processes", "head")
        cursor = grid.put(cursor, y, "  " + str(summary.get("count", len(extras))) + " · owner [a3]", "dim")
        grid.put(cursor, y, " ", "branch_s")
        for col in range(cursor + 1, grid.cols - 1):
            grid.put(col, y, "─", "line")
        y += 1
        for entry in extras:
            host, gpu, command, memory, age = (tuple(entry) + (None,) * 5)[:5]
            grid.put(x + 3, y, f"{host}:{gpu}", "soft")
            grid.put(x + 16, y, command or "·", "soft")
            grid.put(x + 26, y, "·" if memory is None else f"{memory:g} GB", "dim")
            grid.put(x + 38, y, age or "·", "dim")
            y += 1
        remaining = summary.get("remaining")
        if remaining:
            grid.put(x + 3, y, f"… {remaining} more", "dim")


def draw_memory(grid: CellGrid, vm, state=None, x=34, y=3):
    """Show only bounded memory records already present in the snapshot."""
    use = max(0, grid.cols - x - 2)
    records = vm.memory
    if records is None:
        grid.put(x + 1, y, "·", "dim")
        return
    y = _table_head(grid, x, use, y, ((1, "PROJECT"), (14, "TIME"), (21, "ACTION"),
                                      (32, "TYPE"), (55, "RECORD")))
    for project_index, project in enumerate(records):
        if project_index:
            y = _group_sep(grid, x, use, y)
        try:
            name, rows = project
        except (TypeError, ValueError):
            continue
        for row_index, row in enumerate(rows or ()):
            when, action, kind, text = (tuple(row) + (None,) * 4)[:4]
            if row_index == 0:
                grid.put(x + 1, y, clip(str(name), 12), "soft")
            grid.put(x + 14, y, "·" if when is None else str(when), "dim")
            grid.put(x + 21, y, "+ saved" if action == "saved" else "· changed", "done" if action == "saved" else "dim")
            grid.put(x + 32, y, "·" if kind is None else str(kind), "dim")
            grid.put(x + 55, y, "·" if text is None else clip(str(text), max(0, use - 55)),
                     "soft" if action == "saved" else "now_sub", limit=x + use)
            y += 1
    summary = (vm.system or {}).get("memory_summary") if isinstance(vm.system, Mapping) else None
    if isinstance(summary, Mapping):
        grid.put(x + 1, y, (f"{summary.get('added_today', '·')} added today · "
                            f"{summary.get('projects', '·')} projects · "
                            f"{summary.get('expired', '·')} expired at {summary.get('expired_at', '·')} "
                            f"(lifecycle) · {summary.get('latest', '·')} latest shown"), "dim")


def draw_system(grid: CellGrid, vm, state=None, x=34, y=3):
    """Display system observations without probing collectors or reclassifying state."""
    data = vm.system if isinstance(vm.system, Mapping) else {}
    install = data.get("install") or {}
    usage = data.get("usage_api") or {}
    dispatch = data.get("dispatch") or {}
    delivery = data.get("peer_delivery") or {}
    collectors = tuple(data.get("collectors") or ())
    runtimes = tuple(data.get("runtimes") or ())
    def section(title, meta=""):
        cursor = grid.put(x, y, "── ", "line")
        cursor = grid.put(cursor, y, title, "head")
        if meta:
            cursor = grid.put(cursor, y, "  " + meta, "dim")
        grid.put(cursor, y, " ", "branch_s")
        for col in range(cursor + 1, grid.cols - 1):
            grid.put(col, y, "─", "line")
    def fields(values, lab_width=17):
        nonlocal y
        for label, parts in values:
            cursor = grid.put(x + 3, y, label, "label")
            cursor = x + 3 + lab_width
            for text, key in parts:
                cursor = grid.put(cursor, y, text, key)
            y += 1
    section("hearting", "install")
    y += 1
    runtimes_text = []
    for index, runtime in enumerate(runtimes):
        if index:
            runtimes_text.append((" · ", "dim"))
        runtimes_text.append((str(runtime), "hb_" + str(runtime)))
    api_parts = [(str(usage.get("state", "·")), "done" if usage.get("state") == "on" else "dim")]
    if usage.get("freshness"):
        api_parts.append((" · claude/codex/opencode fresh " + str(usage["freshness"]), "dim"))
    collector_parts = []
    for index, collector in enumerate(collectors):
        if index:
            collector_parts.append(("  ", "dim"))
        collector_parts.extend(((str(collector) + " ", "dim"), ("✓", "done")))
    collector_parts.extend((("  disabled ", "dim"), ("none", "dim")))
    version_parts = [(str(install.get("version", "·")), "ver"),
                     (" · managed/pinned · distribution" if install else "", "dim")]
    fields((("version:", version_parts), ("runtimes:", runtimes_text or [("·", "dim")]),
            ("usage api:", api_parts), ("collectors:", collector_parts)), 13)
    y += 1
    section("dispatch")
    y += 1
    gov = [(str(dispatch.get("governor_active", "·")), "soft"),
           (f" active / cap {dispatch.get('governor_cap', '·')} · dispatch {data.get('dispatch_count', '·')}", "dim")]
    jobs = [("↳ " + str(dispatch.get("jobs", "·")) + " jobs", "soft"),
            (" (", "dim"), ("⠏ " + str(dispatch.get("resource_running", "·")), "spin"),
            (") · resource " + str(dispatch.get("resource_running", "·")) + " running · " +
             str(dispatch.get("resource_exited", "·")) + " exited", "dim")]
    fields((("governor:", gov), ("jobs:", jobs)), 13)
    y += 1
    section("peer delivery")
    y += 1
    pending = [(str(delivery.get("pending", "·")), "lvl_y"), (" awaiting recipient", "dim")]
    obligations = [("[", "dim"), ("a3", "soft"), ("]", "dim"), ("  ", "soft"),
                   ("↻ pending msg 1", "soft"), ("  · 4d old", "dim")]
    records = [(str(delivery.get("records", "·")), "soft"), (" in the last window", "dim")]
    fields((("pending:", pending), ("obligations:", obligations), ("records:", records)), 13)
    y += 1
    extra = data.get("unregistered_gpu_processes")
    if extra:
        section("unregistered GPU processes", "11 · owner [a3] · see COMPUTE")
        y += 1
        for item in extra[:2]:
            host, index, command, memory, age = (tuple(item) + (None,) * 5)[:5]
            cursor = grid.put(x + 3, y, f"{host}:{index}", "soft")
            cursor = grid.put(cursor, y, "  " + ("·" if command is None else str(command)), "soft")
            cursor = grid.put(cursor, y, "  " + ("·" if memory is None else f"{memory:g} GB"), "dim")
            grid.put(cursor, y, "  " + ("·" if age is None else str(age)), "dim")
            y += 1
