---
# GENERATED METADATA — edit harness-manifest.json, then run tools/generate.py.
description: "Use when context fills, before compact, on handoff to another session, after a large task, or on request. Not for primary routing or memory writes."
argument-hint: "[정리] | 정리만 | 인계 <받을 세션>"
metadata:
  group: ops
  fam: ops
  invocation_class: model-support
  modes: []
  blurb: "Write a handoff card; tidy memory; clear the window."
  use_when: "Use when context fills, before compact, on handoff to another session, after a large task, or on request."
  not_for: "Not for primary routing or memory writes."
---

# session-tidy (정리)

Write the handoff card yourself, queue the memory tidy, and let the window clear itself
when it is safe; the new session then picks the work up from the card by itself. Nothing
here is required input and nothing waits for a confirmation.

When receiving a seat card, start the first reply before any tool call with one summary
in the user's language, about five lines using the three headings
`하던 일 · 남은 일 · 다음 할 일`, then continue. If the user first gives a different
instruction instead of continuing, skip the summary and follow only that instruction.
The card's fixed injection header carries this guidance even when its body is clipped
or memory references accompany it; late memory-only injections and peer handoff
messages do not carry it.

Runtime compaction queues memory tidying for the main session. A nonempty automatic
Claude/OpenCode summary saved to its own verified seat can use the existing handover
snapshot → clear → `이어서해` flow after transcript evidence confirms turn completion. For
explicitly automatic Codex remote-V2 compaction, the same helper waits for that exact main
turn to end, then sends one claim-bound request through the verified pane so the main
session writes a real card and enqueues the matching operation. No empty or fabricated
summary is used. The helper requires matching card and durable queue evidence before clear.
Compact-origin flow keeps its existing continuation; idle-origin flow remains status-only
even if compaction occurs during it. Manual/unknown compaction, failed cards, project or
outside-herdr seats, workers/sub-sessions, changed input/target/card, and uncertain sends
never auto-clear. Do not enqueue a duplicate tidy for the same compaction; invoke this Skill
for separate work.

An idle tidy is separate from compaction and manual tidy. It is eligible only after a real
main-session turn-end/idle signal with known latest-request context tokens at or above the
configured threshold (default 200,000), followed by the configured wait (default 3,240
seconds). The wait is not a promise about provider cache lifetime. Current adapter payloads
do not expose a verified request cache expiry, so the configured wait is used and
`cache_margin_seconds` is recorded but not applied. Provider/request cache expiry stays
unknown unless the actual configuration and request evidence establish it.
When the exact idle or compact request arrives, answer it with the card itself between the
two marker lines the request names, with no tool call; the turn-end hook stores it and queues
the tidy. Do not resume the work: after clear, the successor is
asked for a brief card-based status and to wait for the next user input. New input, a draft,
form/approval, busy or unknown state, an unknown token count, or a different session cancels
or withholds the automatic request. Duplicate events keep the original due time. Only a real
user prompt after the status reply rearms another idle tidy.

Modes: `정리` (default), `정리만` (same, but keep this window) and `인계 <받을 세션>` (same
work, then pass the card to a peer session; only this calling window is cleared, never the
target). The tool is `python3 $AGENT_HOME/utilities/session_tidy.py`.

For a new successor at this seat, run `peer-steward.py start <name> --kind <harness>` (beside this pane by default), then the step-1 command with `--no-clear --handoff <name>`; after its ACK and the predecessor's final idle result, the successor uses `peer-steward.py retire <predecessor>` (§5.14).

## Steps

One tool call, then one short reply. Do not load anything else first.

1. **Card + queue, in one command.** Write six fields yourself from the conversation you
   already hold, in the user's language (about 90 lines at most). Keep these four
   continuation fields first: what is in progress, the decisions still waiting, what to do
   next, and the related paths, PRs and sessions (Fleet tag·pane, role in parentheses;
   herdr name·pane when untagged). Then add a few lines for today's work, pointing to its
   PR, installed version or result file, and short quotations of user instructions marked
   finished, remaining or deferred. Point to artifacts rather than copying their reports.

   ```bash
   python3 "$AGENT_HOME/utilities/session_tidy.py" tidy <<'CARD'
   진행 중: …
   기다리는 결정: …
   다음 할 일: …
   관련: …
   오늘 한 일: … (PR·설치 버전·결과 파일)
   사용자 지시: "…" — 끝남 / 남음 / 보류
   CARD
   ```

   Add `--no-clear` for `정리만`, `--no-continue` when the user asked to stop after tidying
   ("정리하고 끝"), and `--handoff <받을 세션>` for `인계` to an already-running target.
   The command writes the card and, only if the card was written, queues the memory tidy
   and books this window's clear (the same `card` then `enqueue` meaning; both stay
   available separately). It returns at once; nothing waits for the memory tidy.

2. **Reply with its `say=` line only** and start no more work after a scheduled clear.
   For `--handoff`, also report its `prompted=…` line as printed; do not retry or send the
   card any other way. A failed handoff does not change the clear of this window.

What happens behind it: a detached runner reads the conversation after the last tidy
(plus recent untidied sessions of this seat), starts one registered memory worker, applies
its proposal through `mem tidy-apply`, and leaves one result line — with its undo command —
for the next session of this seat. A long conversation is read from its recent end first;
the line says `일부만 읽음(범위)` when an older part still waits for the next tidy. Inside
herdr, once this turn has ended and the window is idle with no form and an empty input box,
the harness's own new-conversation command is typed once (Claude `/clear`, Codex `/clear`,
OpenCode `/new`), the new session receives the card, and `이어서해` is typed once into it.
A prompt typed after the card cancels the clear (`clear=skipped`); a prompt typed into the
new session first, a new card or a handoff cancels the `이어서해` quietly. Anything else the
helper cannot decide leaves the window as it is plus one result line at the next prompt.
A handoff clears only this calling window, never the target. Confirmed/retained delivery
marks this card handed off; failure retains it. Ordinary peer messages and cards have no
session-tidy accept command or successor authority. For a valid typed cross-seat handoff,
read the card and referenced documents, then run only the exact receiver command supplied
by shared guidance; delivery/queue/ACK alone never accepts.

A route that was still running when the window was cleared is taken over by the new session at
the same pane: its card carries one line with the verified route and the usual resume command
(`capability-route.py start --route <file> --jobs <registry>`), the new session may resume it,
and it receives the completion. The registered launch identity does not change.

The next session at this seat receives the latest card once, at start or on its first
prompt, together with any pending result line and a short "참고할 기억" list (ids and titles
only, read with `mem show <id>` when needed). If the tidy ends after that session started, the
list arrives once at its next prompt.
