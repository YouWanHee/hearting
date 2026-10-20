# Explorer B canonical fixtures

These fixtures are a minimal, deterministic projection of the approved B design bundle:
`source.json` carries the selected snapshot, decision, registry, route, cycle, marker and event
evidence; `screens.json` preserves the nine B scenes at 100 and 168 columns and their region
boundaries. `$FIXTURE_ROOT` replaces the capture machine's home path. The captures were scrubbed
by the source bundle's documented masking rules; synthetic presentation notes are marked in
`screens.json`.

PR-1 owns the frame, ledger and existing attempt summary. Therefore the captured raw-output
pane is represented by the explicit `PR-1 summary` expectation; raw output text is not asserted
until the later shared formatter slice. The source capture is a frozen fixture, not live data.
