# Campaign workflow groups v1

The producer may declare a small, evidence-backed view of a subgoal within one
campaign. It does not create a cycle, change route lineage, or alter a manifest.
The only current file is
`campaigns/<current-campaign-locator>/workflow-groups.json`.

## Stored declaration

```json
{
  "schema_version": 1,
  "contract": "artifact-workflow-groups/v1",
  "artifact_root_id": "root_<32 lowercase hex>",
  "repository_id": "repo_<32 lowercase hex>",
  "campaign_id": "camp_<32 lowercase hex>",
  "groups": [{
    "group_id": "wgrp_<32 lowercase hex>",
    "title": "Human subgoal",
    "members": [{"cycle_id": "cyc_<32 lowercase hex>", "stage_label": "Stage"}],
    "relations": [{
      "from_cycle_id": "cyc_<32 lowercase hex>",
      "to_cycle_id": "cyc_<32 lowercase hex>",
      "kind": "precedes",
      "rationale": "The documented input actually used in the next cycle.",
      "evidence_refs": [{
        "path": "campaigns/<campaign-locator>/<cycle-locator>/artifacts/<bucket>/<file>",
        "artifact_id": "art_<32 lowercase hex>",
        "artifact_revision_id": "arev_<32 lowercase hex>",
        "sha256": "sha256:<64 lowercase hex>"
      }]
    }]
  }]
}
```

Objects have exactly these keys; duplicate JSON keys and unknown versions are
invalid. Group IDs use 128 random bits, never a title/date/path hash. A member
is in at most one group in its campaign and must occur in the current
`campaign.json.cycles`, producer cycle record, and physical `.cycle.json` with
the same ID and campaign binding. `campaigns/INDEX.json` is an atomically
replaced ID-to-path *derived cache*; those records, rather than the index alone,
are authoritative. An index mismatch makes a reader ignore this one campaign's
declaration.

Member array order is display order only. `precedes` says that material or a
criterion from the source was actually used as target input. `followup` says a
result or handoff was continued. `retry` says an unsuccessful result was
retried. None claims cycle completion order. These directed edges must form a
DAG. `parallel` needs explicit concurrent-work evidence; its two IDs are
lexically ordered and it is not a DAG edge. A pair cannot have two relations,
including reversed directions. A parallel pair cannot be directly or
indirectly reachable in the directed DAG. No relation means unspecified,
never inferred parallelism. Route `parent_cycle_id` and `depends_on` keep their
own meanings and do not imply a group relation.

Evidence paths are root-relative POSIX paths beneath the `artifacts/` of that
relation's source or target cycle, at most 512 UTF-8 bytes. Empty, `.`, `..`,
backslash and absolute segments or symlinks are invalid. Each ref names a
regular file in the current sealed/open manifest. Its artifact ID, revision
ID and `content_digest` must match the observed file bytes when newly
declared. The IDs and digest record historical evidence; they do not assert
the cycle's current success. Checkpoint updates unrelated to the evidence
file do not invalidate the group. Readers take status, title and primary
artifact from the current manifest/list result. A changed or missing evidence
file makes that evidence `stale`, while an exceeded read budget or a file that
changes during verification is `unverified` and cannot prove a current edge.
Missing open cycles in a materialized list remain member placeholders.

Titles have 1–120 Unicode code points, stage labels 1–40, rationale 1–280;
all must be NFC, one line, with no surrounding space or control characters.
Each group has 1–64 members and 0–128 relations. Limits per file are 32 groups,
256 members, 256 relations, 512 evidence refs, 8 evidence refs per relation,
256 KiB of declaration bytes, 16 MiB per evidence file, and 64 MiB of unique
evidence file bytes. A path is hashed once per request. The producer rejects
oversized new declarations. A reader that exhausts its 64 MiB request budget
keeps structurally valid groups and marks unexamined evidence `unverified`
(`read-budget`, `size-limit`, or `changed-during-read`). A structural, identity
or unsafe-path failure ignores only this campaign's declaration and leaves the
flat cycle list available.

`artifact_workflow_groups.py prepare` is read-only for the artifact root. It
binds current evidence and metadata preimage bytes; the default merges new
groups, members and relations without removing existing ones. `--replace`
explicitly replaces or withdraws them. `apply` takes the producer admission
lock, rechecks preimage and newly bound evidence, then atomically replaces one
metadata file. An identical replay verifies; a different concurrent successor
is a conflict. `verify` checks current structure/identity and reports stale or
unverified evidence separately. An explicitly carried same-campaign group
context can add a new cycle as a member during the existing producer `begin`;
the parent cycle alone cannot do so. `compose` preserves that explicit campaign
and group identity in the existing sealed work request. A later `start`/resume
restores it through the normal producer preparation, even in a fresh process;
a different campaign or conflicting explicit group is rejected. Routes without
this optional context retain their existing behavior. No new workflow gate or
user input is required.
The final recheck covers newly authored evidence immediately before apply;
independent writers can still alter artifact bytes afterward, so the declaration
does not claim an atomic snapshot of external artifact writes. Historical stale
evidence remains readable and does not block an unrelated merge.
