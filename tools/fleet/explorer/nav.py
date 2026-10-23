"""Small read-only session helpers shared by the live snapshot adapter."""

from collections.abc import Mapping


def _get(value, *names, default=None):
    for name in names:
        item = value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)
        if item is not None and item != "":
            return item
    return default


def is_main(session):
    return not _get(session, "is_child") and _get(session, "process_role") != "registered-worker"


def find_session(snapshot, sid):
    for session in _get(snapshot, "sessions", default=()) or ():
        ids = [_get(session, "session_id", "sid", "id")]
        ids.extend(_get(session, "session_aliases", default=()) or ())
        if sid and sid in ids:
            return session
    return None


def generation_chain(entries, harness, sid):
    """Generations of a seat, oldest first: one per session-tidy card, then the running session (``now``).

    A generation follows the one before it as the same session (a compact: ``cont``) or a new session
    (``new``; a clear when its start source says so). A running session that took over by a retire
    handover is not told apart in the seat records, so ``existing`` is never produced here.
    """
    entries = tuple(entries or ())
    items = []
    for entry in entries:
        for card in entry.get("cards") or ():
            items.append({"harness": entry.get("harness"), "sid": entry.get("sid"), "card": card,
                          "entry": entry, "now": False})
    running = next((entry for entry in entries if entry.get("current")), None)
    items.append({"harness": harness, "sid": sid, "card": None, "entry": running, "now": True})
    for index, item in enumerate(items):
        if index == 0:
            item["kind"], item["connection"] = "new", None
            continue
        previous = items[index - 1]
        if item["sid"] == previous["sid"]:
            item["kind"], item["connection"] = "cont", "compact · same session went on"
        else:
            source = (item["entry"] or {}).get("source")
            item["kind"] = "new"
            item["connection"] = ("clear · new session took over" if source == "clear"
                                  else "new session · no handover record")
        if item["harness"] != previous["harness"]:
            item["connection"] += f"  ·  {previous['harness']} → {item['harness']}"
    return items


def default_generation(chain):
    """The generation a seat opens on: the newest one that has a card, else the running one."""
    for index in range(len(chain) - 1, -1, -1):
        card = chain[index].get("card")
        if card and card.get("status") == "available":
            return index
    return len(chain) - 1
