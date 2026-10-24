# First journey measurement

This opt-in operator tool records installation, a focused first task, and
available usage evidence for `claude-only` and `all` profiles. It does not
change release CI. The `all` profile has separate demo repositories for Claude,
Codex, and OpenCode.

## Commands

```sh
python3 tools/release/first-journey.py login --harness claude
python3 tools/release/first-journey.py login --harness codex
python3 tools/release/first-journey.py login --harness opencode
python3 tools/release/first-journey.py run --source published --output /tmp/journey
python3 tools/release/first-journey.py run --source published --model-free --output /tmp/journey-check
```

Login is performed in an isolated environment and retains only the selected
harness native credential file under `/tmp/hearting-measure-auth-UID`. Run copies
that file into the fresh world and back under an exclusive lock. A hard kill
during credential rotation can require login again. Never point `--auth-root`
at a real profile.

`--model-free` skips authentication and model calls. On Linux, `bwrap` is
required; if it is unavailable, the run reports `isolation-unavailable` and
does not fall back to a weaker model run. macOS supports only environment and
HOME separation, so its report marks filesystem protection and d3 unproven.

Reports contain per-step durations, turns, canned answers, session token counts
when available, account usage (`unknown` when unavailable), and failures. Shared
account usage may include other work. Raw transcripts and credentials stay in
the local experiment world; only JSON and Markdown summaries are exported.
Worlds are removed on success and retained on failure by default; use
`--keep-world never|always` to change this.

The demo starts with `Helo` and a test expecting `Hello`. The runner records
the failing baseline. Headless transport or account failures are recorded as
journey failures and are not retried. Published release checksums are handled
by the normal installer. `checkout` builds release assets from a committed ref.
