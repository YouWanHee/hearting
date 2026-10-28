# v3.53.2 resource-consumer fixture

This fixture keeps the SD-39 legacy consumer regression runnable from the
repository without an installed release. It contains the complete old wait
consumer and only the old runner entry/launch source needed by the ordinary
supervised start path.

## Source provenance

Read-only source: the Hearting v3.53.2 release, utilities directory.

- dispatch_resource_wait.py: exact source bytes, SHA256
  5c116261d15614b09e00f1d7a4c1cd7962607efc8b97ef6e20db50ede9777053
  (30,221 bytes).
- resource-runner.py: source slices from the v3.53.2 module whose complete
  source SHA256 is
  392a8d830093250ff33da3f8f253b75eb734fa333fdd30bb66f78452aef62a3d
  (93,889 bytes). The fixture retains the original module header/constants and
  exact definitions main, _main, start_verified, start_watch,
  controller_argv, launch_request, LaunchDeferred, fail,
  gpu_payload_environment, launch_error, locked_update,
  publish_verified_run, read_sentinel, settle,
  settle_queued_validation_failure, and verify_launch_input.
- The old workflow-supervisor.read_armed source is retained in
  resource_sequence.test.py as V3532_READ_ARMED_SOURCE; that test checks its
  recorded SHA256 before compiling it.

The fixture runner's compute-run adoption and observer-only branches fail
closed. The regression exercises ordinary queued resource launch only.

## Test seams and limits

workflow-supervisor.py delegates ledger, route, observation, and watch helpers
to the current test runtime after explicit injection. Its poll adapter invokes
the current operation observer and current evaluator in the temporary ledger,
which supplies the intermediate state expected by the old per-run waiter; it
does not settle a current aggregate or open a successor. The old wait module
still runs its own context, admit_controller_launch, wait, and acknowledge functions;
the old runner's main, _main, and start_verified source runs.
The legacy runner launch stubs GPU lease admission and observer startup, then
runs a local CPU payload. Its runner module path is pointed at the worktree utilities
directory so the old source resolves the existing smoke verifier and current
normal supervisor CLI. No current wait/controller/runner function stands in for
an old consumer function.

This fixture does not establish behavior for old compute-run adoption,
observer-only launches, a live cached application, GPU admission, or remote
compute execution.
