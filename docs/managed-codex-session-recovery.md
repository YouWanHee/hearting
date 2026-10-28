# Codex visible-main transition evidence

This document records the upstream App Server protocol and Hearting's bounded
headless delivery evidence. It does not promise an interactive Codex launcher
or automatic recovery of a user-facing main session. The one automatic step for
a live main is the transient-capacity continuation below.

## Transient capacity continuation

Codex ends a turn on `serverOverloaded` ("Selected model is at capacity") when
the server sends no Retry-After, and on `responseTooManyFailedAttempts` (429 or
503) after its own stream retries. The rollout records the end as
`task_complete.error.codex_error_info`, `thread/turns/list` shows the turn as
`failed` with `codexErrorInfo`, and no Stop hook runs.

The Codex prompt hook starts `utilities/codex_capacity_resume.py` for each main
turn. That observer reads only the turn's own end. After such a failure it waits
1, 2, 4, 8 or 16 minutes by how many continuations in a row made no progress,
then re-checks the app server: the failed turn is still the latest, the native
queue is empty, and no question or approval is waiting. Only then does it add
one continuation to the same thread through `thread/queue/add`, with a client ID
derived from the failed turn. It passes no model or settings, so the queued turn
runs on what the thread has selected now. A usage limit, auth error, interrupt,
or any newer turn leaves the thread alone. The last decision per thread is in
`$HARNESS_STATE_ROOT/codex-capacity-resume/<thread>.json` (default
`~/.local/state/hearting/`).

## Evidence boundary

The official [OpenAI App Server contract](https://learn.chatgpt.com/docs/app-server)
provides `thread/start`,
`thread/resume`, and `thread/fork`. Successful calls return a thread; a fork can
report its predecessor, and session metadata identifies the conversation tree.
Calls can fail. The protocol does not define Hearting's visible terminal pane,
completion owner, TUI epoch, or batch fence.

Hearting therefore changes the visible-main binding only after the sole TUI's
exact request ID receives a successful response in the same gateway epoch. The
latest requested transition wins. Present contradictory `sessionId` or
`forkedFromId` values reject the transition; optional absent values are recorded
as unavailable rather than invented. Every completion carries the exact thread,
epoch, binding generation, parent session, canonical jobs registry, attempts,
and sealed batch. Missing or stale epoch/generation evidence is rejected before
an upstream `turn/start` or `turn/steer`.

| Observation | Binding effect |
|---|---|
| first successful TUI start/resume | establishes the binding |
| later successful TUI start | selects the new root conversation |
| later successful TUI resume | selects the requested conversation |
| direct TUI fork of the current binding | advances to the fork |
| initial explicit TUI fork | establishes from the exact requested source |
| resume of a previously forked thread | accepts valid persisted ancestry without granting it authority |
| failed transition or stale older response | none |
| bare notification, sibling, tool, or native-subagent thread | diagnostic only |
| another TUI epoch | none; the old evidence is stale |

The UI action that triggered the original incident has not been reproduced, so
do not label it as start, resume, or fork without captured request/response
evidence.

Revisiting a conversation retains the witnessed launcher root in a bounded
transition history. Resuming the same thread in the same epoch preserves the
generation and its pending batches. A true thread change or reconnect still
rejects stale batches rather than delivering them to another conversation.

## Read-only Herdr check

The current Codex projection has one checked terminal-host adapter: Herdr. The
check validates the owner-private Herdr socket and proves the source pane's live
workspace, tab, and cwd. It creates nothing:

```sh
preflight.sh interactive-main-recovery --check \
  --pane "$HERDR_PANE_ID" \
  --socket "$HERDR_SOCKET_PATH" \
  --workspace /absolute/project/path
```

The `--start` path is not a supported interactive recovery promise. The
read-only `--check` result is observational evidence only and does not establish
that a user-facing main can be launched or recovered.

Do not use this path for registered headless workers. Do not edit credentials,
sessions, logs, cache, database files, `config.toml`, trust state, or the
canonical jobs registry to recover an interactive main.

## Release boundary

A running managed session stays pinned to the release that created its gateway.
Source changes do not hot-upgrade it. Build and install one collision-free
release through the normal package path, align Claude/Codex/OpenCode projections,
then open a new managed Codex session for end-to-end verification. Existing
sessions may finish on their pinned release.
