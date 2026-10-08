#!/usr/bin/env bash
# Install only missing test tools. Runner package indexes normally suffice;
# a stale index or stalled Azure mirror gets one official Ubuntu retry.
set -euo pipefail

packages=()
commands=()
for package in "$@"; do
  case "$package" in
    ripgrep) command_name=rg ;;
    strace) command_name=strace ;;
    bubblewrap) command_name=bwrap ;;
    *) echo "Unknown CI fixture: $package" >&2; exit 2 ;;
  esac
  if ! command -v "$command_name" >/dev/null; then
    packages+=("$package")
    commands+=("$command_name")
  fi
done
if ((${#packages[@]} == 0)); then
  exit 0
fi

apt_options=(-o Acquire::Retries=2 -o Acquire::http::Timeout=15
             -o Acquire::https::Timeout=15 -o Acquire::Languages=none)
if ! sudo timeout 90 apt-get "${apt_options[@]}" install --yes --no-install-recommends "${packages[@]}"; then
  # Ignore unrelated third-party feeds and the runner's Azure mirror list.
  # Keep the usual Ubuntu components and signature verification.
  . /etc/os-release
  sources=$(mktemp)
  trap 'rm -f "$sources"' EXIT
  printf 'deb https://archive.ubuntu.com/ubuntu %s main universe\n' \
    "$VERSION_CODENAME" "${VERSION_CODENAME}-updates" > "$sources"
  printf 'deb https://security.ubuntu.com/ubuntu %s-security main universe\n' \
    "$VERSION_CODENAME" >> "$sources"
  apt_options+=(-o "Dir::Etc::sourcelist=$sources" -o Dir::Etc::sourceparts=-)
  sudo timeout 120 apt-get "${apt_options[@]}" update
  sudo timeout 90 apt-get "${apt_options[@]}" install --yes --no-install-recommends "${packages[@]}"
fi
for command_name in "${commands[@]}"; do
  command -v "$command_name" >/dev/null
done
