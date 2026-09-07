#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
. "$ROOT/tools/memory/test-isolation.sh"
ROOT_FIX=$(mktemp -d "${TMPDIR:-/tmp}/hearting-isolation-contract.XXXXXX")
trap 'rm -rf "$ROOT_FIX"' EXIT HUP INT TERM

sentinel=$(mktemp -d "${TMPDIR:-/tmp}/hearting-ambient.XXXXXX")
printf '%s\n' sentinel >"$sentinel/memory.db"
before=$(cksum "$sentinel/memory.db")
ambient_agent_home=$(mktemp -d "${TMPDIR:-/tmp}/hearting-ambient-agent-home.XXXXXX")
printf '%s\n' sentinel >"$ambient_agent_home/memory-marker"
ambient_write_events=$(mktemp -d "${TMPDIR:-/tmp}/hearting-ambient-write-events.XXXXXX")
printf '%s\n' sentinel >"$ambient_write_events/write-events.jsonl"
before_agent_home=$(cksum "$ambient_agent_home/memory-marker")
before_write_events=$(cksum "$ambient_write_events/write-events.jsonl")
MEM_STORE="$sentinel"
AGENT_HOME="$ambient_agent_home"
CLAUDE_HOME="$ambient_agent_home"
XDG_STATE_HOME="$ambient_write_events"
export MEM_STORE AGENT_HOME CLAUDE_HOME XDG_STATE_HOME
hearting_test_isolate "$ROOT_FIX/fixture"
[ "$MEM_STORE" = "$ROOT_FIX/fixture/store" ]
[ "$HOME" = "$ROOT_FIX/fixture/home" ]
[ "$XDG_CONFIG_HOME" = "$ROOT_FIX/fixture/config" ]
[ "$XDG_DATA_HOME" = "$ROOT_FIX/fixture/data" ]
[ "$XDG_STATE_HOME" = "$ROOT_FIX/fixture/state" ]
[ "$MEM_WRITE_EVENTS" = "$ROOT_FIX/fixture/state/agent-memory/write-events.jsonl" ]
[ "$MEM_RECALL_EVENTS" = "$ROOT_FIX/fixture/state/agent-memory/recall-events.jsonl" ]
[ "$MEM_RECALL_RECEIPTS" = "$ROOT_FIX/fixture/state/agent-memory/recall-opportunities" ]
[ -z "${AGENT_HOME:-}" ]
[ -z "${CLAUDE_HOME:-}" ]
[ "$(cksum "$sentinel/memory.db")" = "$before" ]
[ "$(cksum "$ambient_agent_home/memory-marker")" = "$before_agent_home" ]
[ "$(cksum "$ambient_write_events/write-events.jsonl")" = "$before_write_events" ]

derived=$(hearting_test_derived_env python3 -c 'import os; print(os.environ.get("MEM_STORE", "") + "|" + os.environ.get("AGENT_HOME", "") + "|" + os.environ.get("CLAUDE_HOME", ""))')
[ "$derived" = "||" ]
case "$HOME" in "$HEARTING_TEST_ROOT"/*) ;; *) exit 1 ;; esac
case "$XDG_CONFIG_HOME" in "$HEARTING_TEST_ROOT"/*) ;; *) exit 1 ;; esac
case "$XDG_DATA_HOME" in "$HEARTING_TEST_ROOT"/*) ;; *) exit 1 ;; esac
case "$XDG_STATE_HOME" in "$HEARTING_TEST_ROOT"/*) ;; *) exit 1 ;; esac
case "$MEM_WRITE_EVENTS" in "$HEARTING_TEST_ROOT"/*) ;; *) exit 1 ;; esac
case "$MEM_RECALL_EVENTS" in "$HEARTING_TEST_ROOT"/*) ;; *) exit 1 ;; esac
case "$MEM_RECALL_RECEIPTS" in "$HEARTING_TEST_ROOT"/*) ;; *) exit 1 ;; esac
rm -rf "$ambient_agent_home" "$ambient_write_events"

count=$(find "$ROOT/tools/memory" -maxdepth 1 -name '*.test.sh' -type f \
  ! -name 'test-isolation-contract.test.sh' -exec sh -c \
  'rg -q "test-isolation\.sh" "$1"' sh {} \; -print | wc -l | tr -d ' ')
total=$(find "$ROOT/tools/memory" -maxdepth 1 -name '*.test.sh' -type f \
  ! -name 'test-isolation-contract.test.sh' | wc -l | tr -d ' ')
[ "$count" -eq "$total" ]

printf '%s\n' 'test isolation contract: PASS'
