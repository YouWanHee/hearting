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

The calling main session writes its own handoff card (`utilities/session_tidy.py card`), then `enqueue` returns at once while a detached runner (`utilities/session_tidy_runner.py`) starts one registered memory worker (`ops/session-tidy-memory`), applies its closed `add`/`supersede`/`reinforce` proposal through `mem tidy-apply`, and leaves one result line (with the undo command) for the seat's next session. The runner reads the conversation tail first: the newest unread part of each record goes to the worker, only a successful apply marks that part as read, and an older unread front stays pending for the next tidy; a record that was only partly read says so (`일부만 읽음(범위)`) and is never reported as "nothing new". The same run also refreshes the card's "참고할 기억" list (record ids and titles from what the batch really wrote plus the related existing records, at most 8 and 1,200 bytes), which the next session at the seat receives once with the card or, if the tidy ends later, at its next prompt. `handoff <target>` delivers the card to a peer session through `utilities/peer-steward.py prompt` only and reports its typed verdict. There is no required input and no confirmation step; a failure keeps the card and the watermarks unchanged and leaves one warning line, which counts any writes that had landed or may have landed and then carries the undo command.

Clearing is part of the default flow. Inside herdr, `enqueue` also books the calling window's clear (`utilities/session_tidy_clear.py`, a detached helper that never waits for the memory tidy): once the turn has ended and the window is idle, `utilities/peer-steward.py clear` types the harness's own new-conversation command once (Claude `/clear`, Codex `/clear`, OpenCode `/new`), only when no prompt was submitted after the card, no form is open and the input box reads as empty. Anything it cannot decide leaves the window alone and one result line (`cleared=true|skipped|failed|unverified`; a success is silent). `정리만` (`enqueue --no-clear`) keeps the window and cancels a pending booking. Outside herdr the session prints the harness's manual command instead (Claude `/clear`, Codex `/clear`, OpenCode `/new`). A handoff clears only the calling window, never the target, and its failure is separate from the clear.

Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.

## Artifact Ownership

Use the shared artifact root rule: prefer `.agent_reports/`; use legacy `.claude_reports/` only when it already exists and `.agent_reports/` does not. Capability-specific output placement follows `core/CONVENTIONS.md` section 5 until this spec is expanded with a stricter per-capability artifact map.

## Role Requirements

Use portable role names from `roles/README.md` and `core/CONVENTIONS.md`. Concrete model names, subagent frontmatter, and runtime-specific tool lists belong in adapter files.

## Guard Requirements

Adapters must preserve the portable invariants relevant to this capability:

- state lives under `${XDG_STATE_HOME:-~/.local/state}/hearting/session-tidy/` (directories 0700, files 0600), never in the artifact root. The one exception is the memory worker's exchange folder `<artifact root>/.runtime/session-tidy/<batch>/` (0700; the input copy, the prompt and the one `actions.json` it writes): its sandbox allows the artifact root the checked wrapper launches it with and not the state folder. The runner reads that file back without following a link, checks it, copies the checked result into state and deletes the folder (a failed batch keeps only the worker's answer until it is pruned); sandboxes and permissions are never widened for it;
- pane input goes only through `utilities/peer-steward.py` (the clear is its `clear` command, typed at most once and never retried);
- the memory worker never calls a memory write command; only `mem tidy-apply` writes, and it never deletes.

## Adapter Realization

| Adapter | Realization |
|---|---|
| Claude Code | `adapters/claude/skills/session-tidy/SKILL.md` and `skills/session-tidy/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/session-tidy/SKILL.md`, while `skills/session-tidy/SKILL.md` remains the compatibility reference kept for parity/drift checks. |
| Codex | Read this spec and run `adapters/codex/bin/preflight.sh capability-info session-tidy`. Use `adapters/codex/skills/session-tidy/SKILL.md` as the native Codex Skill projection; do not consume `skills/session-tidy/SKILL.md` or Claude command files as native Codex configuration. |
| OpenCode | Read this spec and run `adapters/opencode/bin/preflight.sh capability-info session-tidy`. Use `adapters/opencode/skills/session-tidy/SKILL.md` and `adapters/opencode/commands/session-tidy.md` as native OpenCode projections; do not consume `skills/session-tidy/SKILL.md` or Claude command files as native OpenCode configuration. |

## Compatibility Reference

`skills/session-tidy/SKILL.md` and `adapters/claude/skills/session-tidy/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/session-tidy/SKILL.md`, while `skills/session-tidy/SKILL.md` remains the compatibility reference kept for parity/drift checks.
