---
description: "Run the portable session-tidy capability through the OpenCode adapter. Meaning: Write a handoff card; tidy memory."
---

Use the OpenCode adapter realization of portable capability `session-tidy`.
This is adapter-owned output generated from `capabilities/session-tidy.md`, not a runtime-specific command copy.

1. Read `capabilities/session-tidy.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info session-tidy` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before edits, run `adapters/opencode/bin/preflight.sh write <file> [session-id]`.
4. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability session-tidy [cwd] [session-id]`.
5. If the command receives arguments, map them to the portable argument shape:
   `[정리] | 인계 <받을 세션>`.

Portable contract excerpt:

- Invocation semantics: The calling main session writes its own handoff card (`utilities/session_tidy.py card`), then `enqueue` returns at once while a detached runner (`utilities/session_tidy_runner.py`) starts one registered memory worker (`ops/session-tidy-memory`), applies its closed `add`/`supersede`/`reinforce` proposal through `mem tidy-apply`, and leaves one result line (with the undo command) for the seat's next session. `handoff <target>` delivers the card to a peer session through `utilities/peer-steward.py prompt` only and reports its typed verdict. There is no required input and no confirmation step; a failure keeps the card and the watermarks unchanged and leaves one warning line, which counts any writes that had landed or may have landed and then carries the undo command. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
