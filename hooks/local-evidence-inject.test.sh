#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
HOOK="$ROOT/hooks/local-evidence-inject.sh"
TMP=$(mktemp -d)
# The probe caches its rendered block per artifact root; keep every case in this
# suite off the caller's real cache.
export XDG_CACHE_HOME="$TMP/cache"
trap 'rm -rf "$TMP"' EXIT

# Project with legacy, shared, and campaign-cycle evidence artifacts.
mkdir -p "$TMP/project/.agent_reports/research/topic-card" \
  "$TMP/project/.agent_reports/documents/briefing" \
  "$TMP/project/.agent_reports/shared/analysis/ref_x/revisions/rrev_y" \
  "$TMP/project/.agent_reports/campaigns/camp_a/cycles/cyc_b/artifacts/research"
printf 'card body stays private\n' > "$TMP/project/.agent_reports/research/topic-card/card.md"
printf 'briefing body\n' > "$TMP/project/.agent_reports/documents/briefing/overview.md"
printf 'analysis body\n' > "$TMP/project/.agent_reports/shared/analysis/ref_x/revisions/rrev_y/report.md"
printf 'cycle research body\n' > "$TMP/project/.agent_reports/campaigns/camp_a/cycles/cyc_b/artifacts/research/notes.md"

# Hook-JSON mode: structured context with counts and entry paths, no bodies.
# SessionStart is the registered surface: only a newly written artifact changes
# the block, so a per-prompt repeat spent ~360 tokens a turn re-stating it.
printf '{"hook_event_name":"SessionStart","source":"startup","cwd":"%s"}\n' "$TMP/project" \
  | "$HOOK" > "$TMP/hook.out"
python3 - "$TMP/hook.out" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
assert value["hookSpecificOutput"]["hookEventName"] == "SessionStart"
context = value["hookSpecificOutput"]["additionalContext"]
assert "Local evidence present" in context
assert "research: 2 file(s)" in context
assert "documents: 1 file(s)" in context
assert "analysis: 1 file(s)" in context
assert "research/topic-card/card.md" in context
assert "Local evidence before recall" in context
assert "card body stays private" not in context
assert len(context.encode("utf-8")) <= 2400
PY

# CLI text mode.
"$HOOK" --cwd "$TMP/project" --format text > "$TMP/cli.out"
grep -q 'Local evidence present' "$TMP/cli.out"
grep -q 'documents/briefing/overview.md' "$TMP/cli.out"

# No evidence artifacts: silent.
mkdir -p "$TMP/bare"
"$HOOK" --cwd "$TMP/bare" --format text > "$TMP/bare.out"
[ ! -s "$TMP/bare.out" ]

# A prompt-submit payload stays accepted and echoes its own event name, so an
# adapter with no session-start context surface keeps working.
printf '{"hook_event_name":"UserPromptSubmit","prompt":"x","cwd":"%s"}\n' "$TMP/project" \
  | "$HOOK" > "$TMP/prompt.out"
grep -q '"hookEventName": "UserPromptSubmit"' "$TMP/prompt.out"

# Any other event is silent.
printf '{"hook_event_name":"Stop","cwd":"%s"}\n' "$TMP/project" \
  | "$HOOK" > "$TMP/stop.out"
[ ! -s "$TMP/stop.out" ]

# The rendered block is cached per artifact root, bodies included nowhere.
[ "$(find "$XDG_CACHE_HOME/hearting/local-evidence" -name '*.json' | wc -l)" -eq 1 ]
CACHE=$(find "$XDG_CACHE_HOME/hearting/local-evidence" -name '*.json')
python3 - "$CACHE" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
assert value["root"].endswith("/.agent_reports"), value["root"]
assert value["ts"] > 0
assert "Local evidence present" in value["context"]
assert "card body stays private" not in value["context"]
PY

# A stale entry is served immediately; the rescan happens out of band.
python3 - "$CACHE" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
value["ts"] = 0
value["context"] = "# STALE SENTINEL"
json.dump(value, open(sys.argv[1], "w", encoding="utf-8"))
PY
"$HOOK" --cwd "$TMP/project" --format text | grep -q 'STALE SENTINEL'
for _ in $(seq 1 40); do
  python3 -c 'import json, sys; sys.exit(0 if json.load(open(sys.argv[1], encoding="utf-8"))["ts"] else 1)' "$CACHE" && break
  sleep 0.25
done
python3 - "$CACHE" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
assert value["ts"] > 0, "background refresh never landed"
assert "Local evidence present" in value["context"]
PY
# The refresh lock is released, so a later stale hit can refresh again.
[ ! -e "$CACHE.refresh" ]

# A truncated walk reports a lower bound and says so, rather than rendering a
# partial count as a total. 501 files trip the 500-per-group cap deterministically.
mkdir -p "$TMP/capped/.agent_reports/research/bulk"
python3 - "$TMP/capped/.agent_reports/research/bulk" <<'PY'
import pathlib, sys
base = pathlib.Path(sys.argv[1])
for index in range(501):
    (base / f"note-{index:04d}.md").write_text("body\n", encoding="utf-8")
PY
"$HOOK" --cwd "$TMP/capped" --format text > "$TMP/capped.out"
grep -q 'research: 500+ file(s)' "$TMP/capped.out"
grep -q 'Newest of the scanned subset' "$TMP/capped.out"

# The same store, unconstrained, reports an exact count and the plain label.
rm -rf "$XDG_CACHE_HOME/hearting"
"$HOOK" --cwd "$TMP/project" --format text > "$TMP/exact.out"
grep -q 'research: 2 file(s)' "$TMP/exact.out"
grep -q 'Newest entries:' "$TMP/exact.out"

# An unwritable cache location degrades to a plain rescan, never to silence.
rm -rf "$XDG_CACHE_HOME/hearting"
XDG_CACHE_HOME=/proc/nonexistent "$HOOK" --cwd "$TMP/project" --format text \
  | grep -q 'Local evidence present'
rm -rf "$XDG_CACHE_HOME/hearting"

# Worker sessions are exempt.
printf '{"hook_event_name":"SessionStart","source":"startup","cwd":"%s"}\n' "$TMP/project" \
  | AGENT_SESSION_ROLE=worker "$HOOK" > "$TMP/worker.out"
[ ! -s "$TMP/worker.out" ]

# Malformed payload: silent, fail-open.
printf 'not json' | "$HOOK" > "$TMP/malformed.out" 2> "$TMP/malformed.err"
[ ! -s "$TMP/malformed.out" ] && [ ! -s "$TMP/malformed.err" ]

"$HOOK" --help | grep -q 'evidence artifacts exist'
echo 'local evidence prompt probe: PASS'
