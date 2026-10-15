# Capability: session-tidy

This is the portable capability contract for `session-tidy`. It defines runtime-neutral meaning and adapter obligations. It is not a Claude Skill file.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `session-tidy` |
| Group | `ops` |
| Supported modes | `none` |
| Portable meaning | Write a handoff card; tidy memory; clear the window. |
| Argument shape | `[정리] \| 정리만 \| 인계 <받을 세션>` |

## Invocation Semantics

Whenever a seat card is injected, its fixed header instructs the receiving session to
start its first reply, before any tool call, with one summary in the user's language,
about five lines using the three headings `하던 일 · 남은 일 · 다음 할 일`, then continue.
If the user first gives a different instruction instead of continuing, skip the summary
and follow only that instruction. This guidance survives body truncation within the
2,400-byte injection limit, including bundled memory references; it is absent from
late memory-only injections and peer handoff messages.

Claude Code, Codex, and OpenCode give the interactive main session a non-blocking notice based on the latest request's input usage: strictly above 500,000 tokens recommends `/session-tidy`, and strictly above 700,000 asks for tidying now. Each threshold is emitted once per native session ID; a jump across both emits the urgent notice once and consumes both. Compaction does not reset these notices. Owners, workers, auxiliary/reviewer sessions, and subagents are excluded. These notices do not change the runtime's compaction policy.

Runtime compaction counts as a session tidy invocation by the main session. Each completed summary may be saved to that session's own seat card and queues memory tidying; a shared project fallback seat only queues memory tidying, so one session's compaction never replaces another session's card. Only a nonempty automatic Claude or OpenCode summary that was successfully saved to its own verified herdr pane/native seat may use the existing tidy booking to snapshot handover state, clear, and continue. The detached helper waits for transcript evidence that the turn containing that compaction actually ended; an idle signal by itself is insufficient, and a compact booking may wait up to three hours. The card preserves the full summary, a bounded recent conversation tail the summary may not have seen, and the original transcript path. Manual or unknown compaction, an empty summary, failed card persistence, a project fallback/outside-herdr seat, workers or sub-sessions, and Codex remote-V2 compaction never trigger automatic clear. Codex compaction remains queue-only; its existing manual tidy clear/continue behavior is unchanged. Never block or delay compaction to complete tidying. Worker sessions are excluded.

At a shared project fallback seat each session keeps its own card, so one session's card never replaces another's; a different session receives only the seat's newest card, once, and only if it first appeared after that card's author was last active.

The main-authored card keeps work in progress, waiting decisions, the next step
and related references first. Below those four fields, add "오늘 한 일"
(today's work, with PR/version/result pointers) and "사용자 지시" (brief user
quotations marked finished, remaining or deferred), within about 90 lines.
Write it from the conversation the main session already holds.

The calling main session writes its own handoff card (`utilities/session_tidy.py card`), then `enqueue` returns at once while a detached runner (`utilities/session_tidy_runner.py`) starts one registered memory worker (`ops/session-tidy-memory`), applies its closed `add`/`supersede`/`reinforce` proposal through `mem tidy-apply`, and leaves one result line (with the undo command) for the seat's next session. The runner reads the conversation tail first: the newest unread part of each record goes to the worker, only a successful apply marks that part as read, and an older unread front stays pending for the next tidy; a record that was only partly read says so (`일부만 읽음(범위)`) and is never reported as "nothing new". The same run also refreshes the card's "참고할 기억" list (record ids and titles from what the batch really wrote plus the related existing records, at most 8 and 1,200 bytes), which the next session at the seat receives once with the card or, if the tidy ends later, at its next prompt. `handoff <target>` delivers the card to a peer session through `utilities/peer-steward.py prompt` only and reports its typed verdict. There is no required input and no confirmation step; a failure keeps the card and the watermarks unchanged and leaves one warning line, which counts any writes that had landed or may have landed and then carries the undo command.

Clearing and continuing are part of the default flow. Inside herdr, `enqueue` also books the calling window's clear (`utilities/session_tidy_clear.py`, a detached helper that never waits for the memory tidy): once the turn has ended and the window is idle, `utilities/peer-steward.py clear` types the harness's own new-conversation command once (Claude `/clear`, Codex `/clear`, OpenCode `/new`), only when no prompt was submitted after the card, no form is open and the input box reads as empty. A clear counts as confirmed only with evidence the conversation changed (the new session's start hook, its session id from the pane's process or herdr, a new thread id on Codex's status line — hearting's Codex status line shows `thread-title`, the thread id until the thread has a title — or OpenCode's home screen). After a confirmed clear the same helper asks `utilities/peer-steward.py continue` once, which types the continue prompt `이어서해` (no trailer) into the new session only when it is the session the clear started and has had no message yet (Claude: the pane's process names it and its start hook took the card; Codex: its status line shows it and no rollout exists for it, so this prompt is its first message and its start hook delivers the card; OpenCode: its home screen), it is idle, no form is open, the input box is empty, no prompt was submitted since the card and the card is still the booked one and was not handed to a peer. It claims the booking (`pending` → `sending`) right before the one send and never re-sends or retries an Enter. Anything either step cannot decide leaves the window alone and one result line (`cleared=…`, `continued=true|skipped|failed|unverified`; a success is silent, and a continue skipped because the person typed first, left a draft or wrote a new card is silent too). `정리만` (`enqueue --no-clear`) keeps the window and cancels a pending booking; `enqueue --no-continue` (the user asked to stop after tidying) clears without the continue prompt. Outside herdr the session prints the harness's manual command instead (Claude `/clear`, Codex `/clear`, OpenCode `/new`). A handoff clears only the calling window, never the target. Confirmed or retained delivery marks the card as handed off so that window does not continue from it; failed delivery preserves the card for normal continuation.

Running work follows the window. `enqueue` also snapshots the registered depth-1 owner/frame attempts the tidying session answers for (the runtime reads them from the dispatch registry; a card's text grants nothing). When the next session at the same herdr pane starts through a confirmed clear (a `clear` start source or the clear booking's own observation; for OpenCode, its first message in a new session), `session_tidy.py hook` appends one handover row (old session → new session, with the bindings) to the seat ledger. From then on the new session may resume those attempts (`work_start`, replacement authority, the Claude wake re-arm, the interactive frame handback) and receives their pending completions (Claude/Codex prompt sweeps, the Codex queue target), while the registry row keeps its registered `parent_sid`, receipt, digest and gate identity and the pending records stay stored, claimed and acked under that registered parent. One direction (old → new), the same pane and harness only, never old → two sessions at once; a further clear hands over only through the new session's own next tidy. The card carries the verified route and the existing resume command in one line. Without a handover every call behaves as before. OpenCode direct owners still complete through the poll fallback: the new session can resume them, but there is no turn carrier for their completion.

An inherited pane label never grants access to that pane's card or input. A
runtime that provides an exact, live native session target may instead keep a
separate seat for that target and clear it through its official attachment
command in a private terminal. The booking binds the runtime process, start
time and native job identity; clear and continue revalidate those bindings,
the prompt count, card generation, idle state and empty input. A confirmed new
conversation at that same target receives the card and existing work handover.
Other adapters use this same evidence rule: without an implemented exact
target transport they keep their checked pane path and manual fallback.
Claude's messaging inbox carries text, not new-conversation commands; an inbox
message containing `/clear` is never evidence that the conversation cleared.

Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.

## Portable Procedure

Write the card yourself in the user's language, using the conversation you
already hold. Keep the four continuation fields first: work in progress,
decisions waiting, the next step, and related paths, PRs and sessions. Add
"today's work" and "user instructions" below them, keeping the whole card
within about 90 lines. Summarize today's work in a few lines and point to the
PR, installed version or result file. Quote each user instruction briefly and
mark it finished, remaining or deferred. The start preview remains the bounded
beginning of the card; read the full file for the rest. The memory worker's
"partly read" line describes its separate conversation input, not this card.

Example fields (use the user's language):

```text
진행 중: …
기다리는 결정: …
다음 할 일: …
관련: …
오늘 한 일: … (PR·설치 버전·결과 파일)
사용자 지시: "…" — 끝남 / 남음 / 보류
```

Continue with the existing enqueue and handoff flow.

## Artifact Ownership

Artifact root: `core/CONVENTIONS.md §5.1`; output placement: `§5`.

## Role Requirements

Use portable role names from `roles/README.md` and `core/CONVENTIONS.md`. Concrete model names, subagent frontmatter, and runtime-specific tool lists belong in adapter files.

## Guard Requirements

Adapters must preserve the portable invariants relevant to this capability:

- state lives under `${XDG_STATE_HOME:-~/.local/state}/hearting/session-tidy/` (directories 0700, files 0600), never in the artifact root. The one exception is the memory worker's exchange folder `<artifact root>/.runtime/session-tidy/<batch>/` (0700; the input copy, the prompt and the one `actions.json` it writes): its sandbox allows the artifact root the checked wrapper launches it with and not the state folder. The runner reads that file back without following a link, checks it, copies the checked result into state and deletes the folder (a failed batch keeps only the worker's answer until it is pruned); sandboxes and permissions are never widened for it;
- terminal input goes only through `utilities/peer-steward.py` (the clear is its `clear` command and the continue prompt its `continue` command, each typed at most once and never retried); a native attachment addresses the booked session itself and never types into an inherited pane;
- the memory worker never calls a memory write command; only `mem tidy-apply` writes, and it never deletes.

## Adapter Realization

| Adapter | Realization |
|---|---|
| Claude Code | `adapters/claude/skills/session-tidy/SKILL.md` and `skills/session-tidy/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/session-tidy/SKILL.md`, while `skills/session-tidy/SKILL.md` remains the compatibility reference kept for parity/drift checks. |
| Codex | Read this spec and run `adapters/codex/bin/preflight.sh capability-info session-tidy`. Use `adapters/codex/skills/session-tidy/SKILL.md` as the native Codex Skill projection; do not consume `skills/session-tidy/SKILL.md` or Claude command files as native Codex configuration. |
| OpenCode | Read this spec and run `adapters/opencode/bin/preflight.sh capability-info session-tidy`. Use `adapters/opencode/skills/session-tidy/SKILL.md` and `adapters/opencode/commands/session-tidy.md` as native OpenCode projections; do not consume `skills/session-tidy/SKILL.md` or Claude command files as native OpenCode configuration. |

## Compatibility Reference

`skills/session-tidy/SKILL.md` and `adapters/claude/skills/session-tidy/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/session-tidy/SKILL.md`, while `skills/session-tidy/SKILL.md` remains the compatibility reference kept for parity/drift checks.

The successor reads the handoff card and referenced documents, then invokes
`utilities/session_tidy.py handoff accept <ref>`. This is its explicit read-and-accept,
not user approval. One existing transfer processing schedules a retained acceptance
reply and the normal retire reservation, preserving exact original requester,
predecessor and successor lifetimes. Mere delivery never accepts the handoff.
