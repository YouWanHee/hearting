---
# GENERATED METADATA — edit harness-manifest.json, then run tools/generate.py.
name: session-tidy
description: "Use when context fills, before compact, on handoff to another session, after a large task, or on request. Not for primary routing or memory writes."
argument-hint: "[정리] | 인계 <받을 세션>"
metadata:
  group: ops
  fam: ops
  invocation_class: model-support
  modes: []
  blurb: "Write a handoff card; tidy memory."
  use_when: "Use when context fills, before compact, on handoff to another session, after a large task, or on request."
  not_for: "Not for primary routing or memory writes."
---

# session-tidy (정리)

Write the handoff card yourself, queue the memory tidy, and tell the user the session
can be cleared or closed. Nothing here is required input and nothing waits for a
confirmation.

Modes: `정리` (default) and `인계 <받을 세션>` (same work, then pass the card to a peer
session). The tool is `python3 $AGENT_HOME/utilities/session_tidy.py`.

## Steps

1. **Card.** Write four short fields in the user's language and pipe them in:
   what is in progress, the decisions still waiting, what to do next, and the related
   paths, PRs and sessions. Point to artifacts; do not summarize them again.

   ```bash
   python3 "$AGENT_HOME/utilities/session_tidy.py" card <<'CARD'
   진행 중: …
   기다리는 결정: …
   다음 할 일: …
   관련: …
   CARD
   ```

2. **Queue the tidy.** Do not wait for it.

   ```bash
   python3 "$AGENT_HOME/utilities/session_tidy.py" enqueue
   ```

   It prints one line and returns. A detached runner reads the conversation after the
   last tidy (plus recent untidied sessions of this seat), starts one registered memory
   worker, applies its proposal through `mem tidy-apply`, and leaves one result line for
   the next session of this seat — with the command that undoes it. A failure leaves the
   card as it was and says so in one line; if some writes had already landed, that line
   counts them and keeps the undo command.

3. **Handoff only.** For `인계 <받을 세션>`:

   ```bash
   python3 "$AGENT_HOME/utilities/session_tidy.py" handoff <target>
   ```

   Report its one line as printed (`prompted=true|failed|queued|unverified` and the exit
   code); do not retry or send the card any other way.

4. **Report one line:** "이제 /clear 하거나 닫아도 됩니다."

The next session at this seat receives the latest card once, at start or on its first
prompt, together with any pending result line.
