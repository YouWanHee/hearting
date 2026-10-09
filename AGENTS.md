# Hearting Maintenance Principles

These principles apply to every agent that develops or changes Hearting itself,
equally under Claude, Codex, and OpenCode. They do not change how agents work in
other projects that use Hearting.

## The standard before detailed procedure

**The harness must not become a burden on users or agents.** Work is done when
the requested task finishes without needless intervention in normal, failure,
and interruption cases — not when more checks or PASS markers exist.

- If an existing workflow, recipe, or adapter procedure repeatedly blocks
  approved work, fix, merge, or remove that procedure too. Existing structure
  is not a fixed premise.
- Do not answer each failure with another gate, proof, seal, approval, required
  input, recovery command, or owner chain. "Internal validation" or "no new
  flag" does not excuse added burden.
- When the same kind of blockage recurs, stop stacking exceptions; remove the
  duplicated decisions and shorten the path itself. Do not route back through a
  mechanism you are removing just to repair it first.
- Treat rigid recovery paths, hard cross-harness parity, and a slow or empty
  Fleet as structural problems rather than one-off fixes: follow
  [core/LOOP_ENGINEERING.md](core/LOOP_ENGINEERING.md) §7–§10 (recovery follows
  lineage, capabilities follow real work, hard parity moves outside the
  adapters, delegated work stays visible).
- Protect user approvals, data, and running work within existing normal
  behavior, without shifting extra proof or manual recovery onto agents.
- Do not add a checklist, hook, or approval step to enforce these principles.

This standard overrides detailed procedural defaults when maintaining Hearting.
Do not re-enable a gate the user has turned off. Start code changes in `core/`,
`capabilities/`, and `roles/`, which own the shared meaning, and treat the
three adapters equally. The development philosophy is
[core/LOOP_ENGINEERING.md](core/LOOP_ENGINEERING.md).
