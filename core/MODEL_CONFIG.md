# Model Configuration

Portable model roles and profiles describe work quality and budget; each adapter maps them to its runtime. The shipped `adapters/<harness>/config/models.conf` is copied once to `<runtime-home>/agent-config/models.conf`. That copy is user-owned. A consumer selects a valid complete user file as one unit, or the shipped file as one unit. It never merges the files. When only the optional balanced profile is absent, consumers derive it in memory from that user's light operating point. They do not write the derived value back. Native runtime settings remain separate.

## Explicit user edits

`hearting model set <harness> <tier|role> <model>[@effort]` makes one explicit edit to the selected user model file. `<harness>` is `claude`, `codex`, or `opencode`. A missing user file starts from that harness's shipped seed. A present malformed, incomplete, unreadable, unsafe, or non-regular file is preserved and reported; it is never replaced with shipped defaults as a side effect of editing.

Targets use existing declarations and resolvers. A bare name that matches both a tier and another target means the tier. `tier/<name>`, `profile/<name>`, and `role/<name>` disambiguate when needed. Existing roles alias their configured shared tier: editing a role updates that tier for every consumer of it. It does not create a per-role override. A profile target updates only that profile's direct-model declaration, keeping its current resolved budget when the suffix is omitted. If the requested target is the previously absent balanced profile, only that requested profile is materialized from the existing balanced resolver's user-derived budget. Unrelated edits do not materialize optional profiles.

For tier and role targets, omitting `@effort` preserves the configured tier budget. An explicit suffix updates the runtime's existing budget field (`EFFORT` or `VARIANT`). For profile targets, an omitted suffix preserves that profile's currently resolved budget. Model identifiers are stored as data and are never executed by a shell. The command does not probe a remote runtime or claim that a saved identifier is available in a particular runtime version.

An invalid target or unsupported budget is rejected before user-file mutation. `--dry-run` and byte-identical no-op requests create no config directory, lock, or backup. A real edit validates the complete candidate, preserves other declarations, comments, ordering, line endings, native settings and unrelated user files, writes one exact preimage backup for an existing config, then atomically publishes the candidate under a canonical target lock. Concurrent invocations of this command serialize. The filesystem's final precheck-to-rename interval is not a kernel compare-and-swap against non-cooperating writers; callers report conflicts they observe and do not roll back a later successor.

The command configures future consumer reads. It does not rewrite generated adapter projections, active route seals, or process-local model selections. Use the existing runtime consumer and route policy as the authority for those surfaces.

## Shipped-default-only maintenance

A change that touches only the shipped default stays separate from the user-owned actual model set: edit `adapters/<harness>/config/models.conf`, then finish through the normal generation and existing fixture path as small `direct` work. It does not pick new concrete default values on its own, does not sync provider or user config, and does not clean up model-name literals across the repo.
