# Codex 0.162.1 hook review screen fixtures

`cap-*` files are owner-provided `tmux capture-pane` output from codex-cli
0.162.1 in a disposable `CODEX_HOME`, with two harmless untrusted hooks.
`alt` and `inline` identify alternate-screen and inline-screen launches; `80`
means 80x40 and `40` means 40x30. `.txt` is plain capture output, `.ansi` is
`tmux capture-pane -e` output. `cap-80x40-noalt.txt` is the no-alt 80x40
capture. The bodies retain the real trailing blank rows.
Each `cap-*` file ends with a `# capture fixture delimiter` line solely to keep
the repository file from ending in blank lines; tests remove that delimiter
before passing the captured body to the classifier.

`startup_hooks_*.snap` and `worktrees_*_choices.snap` are upstream Codex 0.162.1
TUI snapshots from the files named in each snapshot header. They cover hook
review progress/trust-error variants and the `/new`/`/fork` location popup.
