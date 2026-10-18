#!/usr/bin/env sh
# Shared bounded rendering for one failed preflight check. Input is the exact
# captured output stream; no command is retried here.
set -eu
[ "$#" -eq 1 ] || exit 64
name=$1
exec python3 -c '
import re, sys

name = sys.argv[1]
raw = sys.stdin.buffer.read().replace(b"\x00", b"").replace(b"\r", b"")
lines = [line.decode("utf-8", "replace") for line in raw.splitlines() if line.strip()]
total = len(lines)
if not lines:
    print(f"cause_{name}_1=no-output-captured")
    print(f"cause_{name}_lines=0/0")
    raise SystemExit(0)
pattern = re.compile(r"(^check=[^ ]*:(failed|review-needed))|(^status=failed)|(^[a-z_]*reason=)|(Traceback)|([Ee]rror)")
selected = [line for line in lines if pattern.search(line)][:5]
if not selected:
    selected = lines[-5:]
for index, line in enumerate(selected, 1):
    encoded = line.encode("utf-8")
    if len(encoded) > 200:
        line = encoded[:197].decode("utf-8", "replace") + "..."
    print(f"cause_{name}_{index}={line}")
print(f"cause_{name}_lines={len(selected)}/{total}")
' "$name"
