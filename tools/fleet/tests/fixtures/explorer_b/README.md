# Explorer B canonical fixtures

These fixtures are a deterministic projection of the approved B design bundle's live capture
(2026-10-20 21:35 KST, main session `[3e]`). `$FIXTURE_ROOT` replaces the capture machine's home
paths; `explorer_fixture.materialize()` expands the token into a temporary directory and writes
real files, so the explorer's readers open actual paths.

`source.json`

| key | content | source |
|---|---|---|
| `registry` | latest `jobs.log` row of each attempt (41 rows, 40 of them for `[3e]`, one for another session) | captured `registry-history.jsonl`; metadata reduced to the fields the policy and the readers use |
| `decisions` | `decide_attempt` results, `manual=false` | captured `live/decisions-live.json` |
| `snapshot` | the 19 sessions (relation/message fields) and the 5 live jobs of `[3e]` with their `observed_liveness` | captured `live/snapshot.json` |
| `routes` | 16 route records, reduced to id/hash/digest/parent cycle/nodes | captured `routes/*.json` |
| `cycles` | 9 cycle records. Six are the captured cycle bindings with the frame `route_id`, `route_capability`, `title` and `state` added. Three (`reconstructed: true`) are frame-only requests (P2+P4, TUI 조사, steward) whose cycle record was not captured | captured `cycles/*.json` + reconstruction |
| `markers` | completion markers of the b6 route (captured) and a synthesized `plan` marker for the herdr route | captured `markers/*.json` |
| `obligations` | the 22 peer-steward duty records of `[3e]` | captured `ledgers/peer-steward/*.json` |
| `events` | event rows | earlier fixture |

What is not the captured bytes:

- Registry rows of the two attempts that were still `open` in the frozen history
  (`att-e6e42dfc…`, `att-6b4e40fb…`) carry their final `done` row, because the canonical decisions
  were taken at 21:37 after they finished.
- Route records keep the captured `route_hash` although their paths are tokenized, so the hash
  cannot be recomputed. Tests read them with `explorer_fixture.json_route_loader`; the default
  `route.load` is for real files.
- Gate evidence files are synthetic text. For every node but `execute` the marker's `sha256` is that
  text's digest (the gate is proven); `execute` keeps the captured digest, so its evidence does not
  match and the gate stays unproven (the canonical `— 판정 보류 · 근거 불일치`).
- Cycle `title` is the request's own name used by the design (`b6-report`, `herdr-tmux`, …).

`now` (1792499700.0) is the instant the remaining-work ages (`70분 전`, …) are evaluated at.

## ss-08 additions (status band, navigation, system page, events, screens)

`source.json` `snapshot` now also carries what the screen shows beyond sessions and the five `[3e]` jobs:

| key | content | source |
|---|---|---|
| `jobs` | all 16 live dispatch rows (the five `[3e]` rows keep their `state_evidence`; the other eleven are reduced to harness / liveness / status / parent / route fields; the one row without an attempt id has `attempt_id: ""` and a `key`) | captured `live/snapshot.json` `/jobs` |
| `sessions[].session_aliases` | the resumed-session alias of `[01]` (`4bf921ef…`) the GPU owner is matched through | captured `live/snapshot.json` |
| `session_order` | the legacy group's live order of the `hearting` sessions (`[b6] [c0] [7a] [3e] [d6]`); the product reads it as the existing live-order hint, then falls back to snapshot order | captured `current-group-168.txt` |
| `usage_snapshots` | `usage_snapshots` shape (`payload.rl_5h / rl_7d`) with the captured percentages CL 5h 29 / week 7, CX week 8, OC 5h 0 / week 1 (the `--no-usage-api` JSON capture holds no percentages, so these come from the group-view text) | `current-group-168.txt` rows 3–5 |
| `governor` | `{"active": 5, "cap": 30}` | captured |
| `compute_hosts`, `unregistered_gpu` | the five hosts reduced to cpu / memory / per-GPU memory and processes (owner kind+label, session owner), and the unregistered-GPU rows reduced to host / GPU / pid / owner | captured `live/snapshot.json` |
| `observations` | `jobs` / `herdr` age 0 s as captured; `compute` age 0.59 s = command end − `observed_at` as the design computes it | captured |

`events` is now the newest 14 `[3e]` rows of the captured `events.json`, with the harness / worker type of the registry row and `tag: "3e"` added. The hearting sessions' `cwd` is `$FIXTURE_ROOT/personal/hearting` so the repository name resolves to `hearting`.

`screens.json` is the approved screens themselves (`final/screens/B-*.txt`, 45 rows × 100 / 168 columns) for the nine B scenes. A scene names the viewer state it stands for (`request`, `tab`, `stage`, `region`, `history`, `keep`, `broken_tab`, `failed_source`). Only the `first` scene has a raw-output rectangle (`output`), which is blanked in the stored rows and excluded from the comparison; the two scenes that are mostly raw output (`inflight`, `output-oc`) compare only the bands listed in `compare_rows`. Every other row of every other scene is compared text for text. The only places where the product text may differ are the scene's `allowed` entries, each asserted by `test_explorer_screens.py`:

| id | scenes | difference |
|---|---|---|
| `successor-wording` | overview, source-failed | the approved `승계 ? · fixture에 확인 근거 없음` reads `승계 ? · 확인 근거 없음` (the product has no fixture) |
| `p4-duty-order` | overview, source-failed | the five remaining duties are the approved ones, listed oldest first instead of in directory-listing order |
| `history-replacement-section` | history | the approved scene appends the `감독 교체` section of the OpenRig 조사 request under b6-report; the product shows it only on the request whose supervisor was replaced (the test compares the section with that request's own lines; the cycle title is `openrig 조사`, the screen spells `OpenRig 조사`) |

## ss-10 additions (raw output)

`output_synthetic.json` holds the records behind the raw-output rectangle of three approved screens. It is
reduced, not captured bytes:

| key | content | source |
|---|---|---|
| `logs` | the Codex attempt logs of the b6 `test` stage (last 8 `item.completed` records, line 9–16 of the frozen file: the reader's tail window) and of the in-flight `material-strategy` stage (its first 2 records), written to the `log_file` paths of the registry rows | captured `live/output/*.jsonl` |
| `claude_owner` | the supervised Claude owner of fleet-explorer: a supervisor log announcing its session plus the 3 assistant text rows of its native transcript (written under the `runtime_home` this fixture now gives that registry row) | captured `output/claude-native.jsonl` |
| `cleaned_owner` | the b6 Claude owner: a supervisor log with its session announcement and no worker home, i.e. `감독 기록만 남음 · 원천 정리됨` | captured `live/output/sources.json` |
| `opencode` | the BC_ResNet OpenCode owner as two database rows (v1 `message`/`part`, v2 `session_message`), written by `explorer_fixture.materialize_opencode` | captured `output/opencode-native.json` |

Reduction rules: the first line of every command, output and message is the captured one; its further lines are
placeholders (`…`), so only the `(+k줄)` counts are real. The capture machine's 22-cell home
prefix is replaced by `/fixt/srv/usr/Fixtures` — the same width, so every clip position stays — in the
records and in the expected rows alike. OpenCode row ids are `1, 2` (v1) / `seq 1` (v2)
instead of the captured `190, 197`, in the approved order (message first, tool output second); the tool part
carries only its `output`, as in the approved screen.

`screens.json` scenes `first`, `inflight`, `output-oc` carry `output_rows`: the approved raw-output rectangle of each
width (`x`, `y`, `w`, `h`, `rows`, right-trimmed). `first` also pins its viewer state to the top of the pane
(`output_scroll: 0`, the approved 100-column rectangle shows the first 17 of 20 rows). `inflight` and `output-oc`
are drawn without a ledger column in the approved screens, so their rows are compared at the approved output width
with `frame.output_pane` instead of through a full frame; the full frame of `first` is compared row for row.
