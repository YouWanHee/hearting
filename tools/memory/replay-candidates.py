#!/usr/bin/env python3
"""Replay one prompt sequence through the candidate probe three ways (measurement only).

  a  the old behaviour: every prompt gets its full candidate block
  b  each record is shown once per session
  c  b plus the optional short body for one clearly leading new record

The same prompts run against a private copy of a memory database. The source
database is opened read-only and never written; the copy lives in a temporary
store with the automatic exchange off, no remote, and its own event, receipt and
display-history paths, all removed on exit. Prompt text stays in memory: nothing
printed, written to a file or logged contains it (only SHA-256 hashes of it).

  replay-candidates.py --db <memory.db|store-dir> (--transcript <session.jsonl> | --prompts <file>)
                       [--cwd DIR] [--limit N] [--json] [--json-out FILE]

``--transcript`` is a Claude session jsonl (its user prompts, in order);
``--prompts`` is a text file with one prompt per line. Variant (a) uses an
internal measurement-only switch, MEM_CANDIDATE_DEDUP=0, that ordinary use never sets.
"""
import argparse
import collections
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

MEM_PATH = Path(__file__).resolve().parent / "mem.py"
ID_LINE = re.compile(r"^- \[[^\]]+\] (\S+): ", re.M)

VARIANTS = (
    ("a", "current (no dedup)", {"MEM_CANDIDATE_DEDUP": "0"}),
    ("b", "dedup only", {}),
    ("c", "dedup + short body", {"MEM_CANDIDATE_BODY": "1"}),
)
VARIANT_SWITCHES = ("MEM_CANDIDATE_DEDUP", "MEM_CANDIDATE_BODY")


def prompts_from_transcript(path):
    """The user prompts of a Claude transcript in order, plus its most common cwd."""
    prompts = []
    cwds = collections.Counter()
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            if isinstance(row.get("cwd"), str) and row["cwd"]:
                cwds[row["cwd"]] += 1
            if row.get("type") != "user":
                continue
            if row.get("isMeta") or row.get("isCompactSummary") or row.get("isSidechain"):
                continue
            message = row.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                content = "\n".join(
                    block["text"] for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                    and isinstance(block.get("text"), str))
            if not isinstance(content, str):
                continue
            text = content.strip()
            if not text or text.startswith(("<local-command", "<command-")):
                continue
            prompts.append(text)
    return prompts, (cwds.most_common(1)[0][0] if cwds else "")


def prompts_from_file(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return [line.strip() for line in handle if line.strip()]


def copy_database(source, target):
    """Copy the database through SQLite's backup API; the source is only ever read."""
    source = Path(source)
    if source.is_dir():
        source = source / "memory.db"
    if not source.is_file():
        raise SystemExit(f"replay-candidates: no database at {source}")
    last = None
    for query in ("mode=ro", "mode=ro&immutable=1"):
        try:
            src = sqlite3.connect(f"{source.resolve().as_uri()}?{query}", uri=True, timeout=5)
            try:
                dst = sqlite3.connect(str(target))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
                return
            finally:
                src.close()
        except sqlite3.Error as exc:
            last = exc
    raise SystemExit(f"replay-candidates: cannot read {source}: {last}")


def isolate_environment(tmp):
    """Point every path the probe touches into ``tmp`` before mem.py is imported."""
    for key in list(os.environ):
        if key.startswith(("MEM_", "AGENT_", "CODEX_", "OPENCODE_", "FLEET_")):
            del os.environ[key]
    home = tmp / "home"
    (tmp / "store").mkdir()
    (tmp / "config" / "hearting").mkdir(parents=True)
    (tmp / "config" / "hearting" / "memory-sync.json").write_text('{"enabled": false}\n')
    home.mkdir()
    os.environ.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp / "config"),
        "XDG_STATE_HOME": str(tmp / "state"),
        "XDG_DATA_HOME": str(tmp / "data"),
        "TMPDIR": str(tmp),
        "AGENT_HOME": str(tmp / "agent-home"),
        "MEM_STORE": str(tmp / "store"),
        "MEM_PROJECTS": str(tmp / "projects"),
        "MEM_RECALL_EVENTS": str(tmp / "events" / "recall-events.jsonl"),
        "MEM_RECALL_RECEIPTS": str(tmp / "events" / "recall-opportunities"),
        "MEM_CANDIDATE_SEEN": str(tmp / "events" / "candidate-seen"),
        "MEM_WRITE_EVENTS": str(tmp / "events" / "write-events.jsonl"),
        "MEM_EXCHANGE_AUTO": "0",
        "MEM_SYNC_REMOTE": "0",
    })


def load_mem():
    spec = importlib.util.spec_from_file_location("mem_replay_target", MEM_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def replay_variant(mem, key, switches, prompts, limit, run_id):
    for name in VARIANT_SWITCHES:
        os.environ.pop(name, None)
    os.environ.update(switches)
    session = f"replay-{run_id}-{key}"
    total = calls_with_output = body_lines = 0
    shown = collections.Counter()
    for index, prompt in enumerate(prompts):
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            mem.candidates(prompt, limit=limit, runtime="replay",
                           session_id=session, turn_id=f"turn-{index}")
        context = sink.getvalue().rstrip("\n")
        size = len(context.encode("utf-8"))
        total += size
        if size:
            calls_with_output += 1
        for record_id in ID_LINE.findall(context):
            shown[record_id] += 1
        body_lines += sum(1 for line in context.splitlines() if line.startswith("  > "))
    top = shown.most_common(1)
    return {
        "calls": len(prompts),
        "calls_with_output": calls_with_output,
        "total_bytes": total,
        "ids_shown": sum(shown.values()),
        "distinct_ids": len(shown),
        "most_repeated_id": top[0][0] if top else "",
        "most_repeated_count": top[0][1] if top else 0,
        "body_lines": body_lines,
    }


def format_table(report):
    header = ("variant", "calls", "output calls", "total bytes", "ids shown",
              "distinct ids", "top repeat", "bodies")
    rows = [header]
    for key, label, _ in VARIANTS:
        item = report["variants"][key]
        rows.append((f"{key} {label}", str(item["calls"]), str(item["calls_with_output"]),
                     str(item["total_bytes"]), str(item["ids_shown"]),
                     str(item["distinct_ids"]), str(item["most_repeated_count"]),
                     str(item["body_lines"])))
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
             for row in rows]
    verdicts = report["verdicts"]
    lines.append("")
    lines.append(f"(b) < (a): {'PASS' if verdicts['b_lt_a'] else 'FAIL'}"
                 f"   ({report['variants']['b']['total_bytes']} < "
                 f"{report['variants']['a']['total_bytes']})")
    lines.append(f"(c) <= (b): {'PASS' if verdicts['c_le_b'] else 'FAIL'}"
                 f"   ({report['variants']['c']['total_bytes']} <= "
                 f"{report['variants']['b']['total_bytes']})")
    lines.append(f"(c) < (a): {'PASS' if verdicts['c_lt_a'] else 'FAIL'}"
                 f"   ({report['variants']['c']['total_bytes']} < "
                 f"{report['variants']['a']['total_bytes']})")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", required=True, help="memory.db copy (or a store directory)")
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--transcript", help="Claude session jsonl")
    source.add_argument("--prompts", help="text file, one prompt per line")
    ap.add_argument("--cwd", help="project directory the probe runs in")
    ap.add_argument("--limit", type=int, default=6)
    ap.add_argument("--json", dest="json_stdout", action="store_true",
                    help="print the JSON report instead of the table")
    ap.add_argument("--json-out", help="also write the JSON report here")
    args = ap.parse_args(argv)

    if args.transcript:
        prompts, seen_cwd = prompts_from_transcript(args.transcript)
    else:
        prompts, seen_cwd = prompts_from_file(args.prompts), ""
    if not prompts:
        print("replay-candidates: no prompts found", file=sys.stderr)
        return 2
    cwd = args.cwd or (seen_cwd if seen_cwd and Path(seen_cwd).is_dir() else os.getcwd())
    db_source = Path(args.db)
    json_out = Path(args.json_out).resolve() if args.json_out else None
    with tempfile.TemporaryDirectory(prefix="replay-candidates-") as raw:
        tmp = Path(raw)
        isolate_environment(tmp)
        copy_database(db_source, tmp / "store" / "memory.db")
        os.chdir(cwd)
        mem = load_mem()
        run_id = tmp.name[-8:]
        report = {
            "schema": 1,
            "prompts": len(prompts),
            "prompt_sha256": [hashlib.sha256(p.encode("utf-8")).hexdigest() for p in prompts],
            "limit": args.limit,
            "variants": {key: replay_variant(mem, key, switches, prompts, args.limit, run_id)
                         for key, _, switches in VARIANTS},
        }
    totals = {key: report["variants"][key]["total_bytes"] for key, _, _ in VARIANTS}
    report["verdicts"] = {
        "b_lt_a": totals["b"] < totals["a"],
        "c_le_b": totals["c"] <= totals["b"],
        "c_lt_a": totals["c"] < totals["a"],
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    if json_out:
        json_out.write_text(text + "\n", encoding="utf-8")
    print(text if args.json_stdout else format_table(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
