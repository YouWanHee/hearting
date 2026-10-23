"""Small synthetic source helpers shared by Fleet explorer reader tests."""

import contextlib
import json
import sqlite3
from pathlib import Path
from datetime import timedelta, timezone

FIXTURE_DIR = Path(__file__).with_name("fixtures") / "explorer"
KST = timezone(timedelta(hours=9))


def load_source():
    source = json.loads((FIXTURE_DIR / "source.json").read_text(encoding="utf-8"))
    if "view_model" in source:
        view_model = json.loads((FIXTURE_DIR / source["view_model"]).read_text(encoding="utf-8"))
        return {**source, "sources": [view_model["values"]]}
    return source


def output_reader(**options):
    from tools.fleet.explorer.output import OutputReader
    options.setdefault("tz", KST)
    return OutputReader(**options)


def supervisor_rows(attempt_id, session_id, cwd):
    return [{"type": "dispatch.supervisor.session", "parent_attempt_id": attempt_id,
             "session_id": session_id, "cwd": cwd},
            {"type": "dispatch.supervisor.turn-started", "parent_attempt_id": attempt_id,
             "turn_ordinal": 1},
            {"type": "dispatch.supervisor.turn-completed", "parent_attempt_id": attempt_id,
             "turn_ordinal": 1}]


def write_opencode_db(path, sid, generation, rows):
    """Write synthetic v1/v2 OpenCode transcript rows for bounded reader tests."""
    with contextlib.closing(sqlite3.connect(path)) as connection:
        if generation == "v1":
            connection.execute("CREATE TABLE IF NOT EXISTS message (id TEXT, session_id TEXT, role TEXT)")
            connection.execute("CREATE TABLE IF NOT EXISTS part (session_id TEXT, message_id TEXT, data TEXT)")
            for row in rows:
                connection.execute("INSERT INTO message VALUES (?, ?, ?)",
                                   (row["message"], sid, row["role"]))
                connection.execute("INSERT INTO part (rowid, session_id, message_id, data) VALUES (?, ?, ?, ?)",
                                   (row["rowid"], sid, row["message"],
                                    json.dumps(row["data"], ensure_ascii=False)))
        else:
            connection.execute("CREATE TABLE IF NOT EXISTS session_message "
                               "(session_id TEXT, type TEXT, seq INTEGER, data TEXT)")
            for row in rows:
                connection.execute("INSERT INTO session_message VALUES (?, ?, ?, ?)",
                                   (sid, row["type"], row["seq"],
                                    json.dumps(row["data"], ensure_ascii=False)))
        connection.commit()
