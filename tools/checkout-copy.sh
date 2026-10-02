#!/usr/bin/env sh
# A private copy of the checkout for suites that rewrite repository files
# (2026-10-02).
#
# Every suite in a run shares one working tree, and product code reads it
# live: a route's launch identity hashes the development checkout's
# `git status` and code anchors such as `harness-manifest.json`, and many
# suites parse that manifest. `adaptation-guard.test.sh` rewrote four tracked
# files for minutes and `generated-projections.test.sh` rewrote the manifest
# and every projection, so a peer suite compiled one route twice and got two
# route ids, failed worker-route-guard validation, or parsed a half-written
# manifest. A shared lock serialized only the suites that took it. A suite
# that changes repository files now changes its own copy, and the live
# checkout stays read-only for the whole run.
#
# `checkout_copy <root> <dest>` copies the working tree of <root> -- tracked
# and untracked-but-not-ignored files, uncommitted edits included -- into
# <dest> and records it as one commit of a fresh repository, so git-aware
# checks see the same tracked set and ignore rules. Scratch files go beside
# <dest> (`<dest>.*`), so its parent should be a private temp directory.
# Returns non-zero on failure.

_checkout_copy_existing() {
  # NUL-separated paths on stdin -> those that exist (a path deleted in the
  # working tree is still listed by `ls-files --cached`).
  xargs -0 sh -c \
    'for f; do if [ -e "$f" ] || [ -L "$f" ]; then printf "%s\0" "$f"; fi; done' sh
}

checkout_copy() {
  _cc_root=$1
  _cc_dest=$2
  git -C "$_cc_root" rev-parse --is-inside-work-tree >/dev/null || return 1
  mkdir -p "$_cc_dest" || return 1
  # Each step is checked on its own: a pipeline would hide a failed listing
  # or archive behind a successful extract and leave a partial copy.
  git -C "$_cc_root" ls-files -z --cached --others --exclude-standard \
    > "$_cc_dest.all" || return 1
  git -C "$_cc_root" ls-files -z --cached > "$_cc_dest.tracked" || return 1
  (cd "$_cc_root" && _checkout_copy_existing < "$_cc_dest.all" > "$_cc_dest.files" \
    && _checkout_copy_existing < "$_cc_dest.tracked" > "$_cc_dest.tracked-existing") || return 1
  (cd "$_cc_root" && tar --null --no-recursion -T "$_cc_dest.files" -cf "$_cc_dest.tar") || return 1
  (cd "$_cc_dest" && tar -xf "$_cc_dest.tar") || return 1
  rm -f "$_cc_dest.all" "$_cc_dest.tracked" "$_cc_dest.files" "$_cc_dest.tar"
  git -C "$_cc_dest" init -q || return 1
  git -C "$_cc_dest" add -A || return 1
  # Keep a tracked file tracked even if an ignore rule matches it.
  git -C "$_cc_dest" add -f --pathspec-from-file="$_cc_dest.tracked-existing" \
    --pathspec-file-nul || return 1
  rm -f "$_cc_dest.tracked-existing"
  git -C "$_cc_dest" -c user.name=checkout-copy -c user.email=checkout-copy@localhost \
    -c commit.gpgsign=false commit -q --no-verify -m "checkout copy" || return 1
}
