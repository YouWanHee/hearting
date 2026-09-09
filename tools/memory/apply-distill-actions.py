#!/usr/bin/env python3
"""Apply memory distillation JSON-lines actions.

The distiller model only proposes JSON objects. This script owns shape checks,
snapshot membership checks, and argv-only calls into mem.py.
"""
import argparse
import json
import os
import subprocess
import sys

AUTOMATIC_TYPES = {
    "decision", "user-correction", "unresolved-obligation", "artifact-pointer",
}
CAPSULE_LISTS = {
    "aliases": "--alias", "entities": "--entity", "topics": "--topic",
    "artifact_refs": "--artifact-ref",
}


def _load_snapshot_ids(path):
    if not path:
        return set()
    try:
        with open(path, encoding="utf-8") as fh:
            return set(fh.read().split())
    except OSError:
        return set()


def apply_actions(out_path, mem_path, mode="increment", snapshot_ids_path="",
                  deny_reattribute=False, strict_output=False):
    # In curate mode this is a destructive allowlist, not every id printed in the
    # snapshot. `curate-snapshot` deliberately omits PROTECTED PENDING handoff/
    # thread ids, so model output cannot prune or merge them through this layer.
    destructive_ids = _load_snapshot_ids(snapshot_ids_path)

    def member(rid):
        return (mode != "curate") or (rid in destructive_ids)

    # D-37: mode=curate is the D-18 session-end curator path. Attribute its
    # journal actor deterministically as curator rather than distiller, even
    # when the parent runs with MEM_DISTILL=1.
    mem_env = os.environ.copy()
    if mode == "curate":
        mem_env["MEM_ACTOR"] = "curator"

    class InvalidOutput(ValueError):
        pass

    def reject(message):
        if strict_output:
            # Model text and record identifiers never enter strict diagnostics.
            raise InvalidOutput("invalid-output")
        sys.stderr.write(message)

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise InvalidOutput("invalid-output")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise InvalidOutput("invalid-output")

    try:
        with open(out_path, "r", encoding="utf-8",
                  errors="strict" if strict_output else "replace") as fh:
            lines = fh.readlines()
    except (OSError, UnicodeError):
        if strict_output:
            sys.stderr.write("[distill-parse] output-unavailable\n")
            return 2
        lines = []

    commands = []
    nonempty_count = sum(bool(line.strip()) for line in lines)
    allowed_keys = {
        "add": {"action", "tier", "type", "body", "headline", *CAPSULE_LISTS},
        "reinforce": {"action", "id"}, "prune": {"action", "id"},
        "graduate": {"action", "id", "to"}, "reattribute": {"action", "id"},
        "merge": {"action", "ids", "canonical"},
        "supersede": {"action", "id", "by"},
    }

    try:
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("```"):
                if strict_output:
                    reject("code-fence")
                continue
            try:
                rec = json.loads(line, object_pairs_hook=unique_keys,
                                 parse_constant=invalid_constant) if strict_output else json.loads(line)
            except Exception:
                reject(f"[distill-parse] skip malformed: {line[:120]!r}\n")
                continue
            if not isinstance(rec, dict):
                reject("[distill-parse] skip non-object\n")
                continue

            action = rec.get("action")
            if action is None and rec.get("tier") and rec.get("type") and isinstance(rec.get("body"), str):
                action = "add"

            if strict_output:
                if action == "noop":
                    if rec != {"action": "noop"} or nonempty_count != 1:
                        reject("invalid-noop")
                    continue
                if (not isinstance(action, str) or action not in allowed_keys
                        or set(rec) - allowed_keys[action]):
                    reject("unsupported-shape")
                if action == "graduate" and rec.get("to", "durable") != "durable":
                    reject("unsupported-graduation-tier")

            # `mem delete` is a user-controlled path and is never a curator action.
            # Keep this explicit so a future mem.py delete surface cannot accidentally
            # become reachable from untrusted distiller output.
            if action == "delete":
                reject("[distill-parse] skip delete: unsupported destructive action\n")
                continue

            # increment = add-only, enforced (not merely prompted). The turn-nudge/fast
            # tier reads untrusted transcript delta with no snapshot whitelist, so a
            # prompt-injected model could name id-mutations (prune/merge/graduate/...)
            # that member() would wave through under mode != "curate" (always True).
            # Reject id-mutations outside curate mode so only the snapshot-grounded deep
            # curator can ever delete/merge/graduate. Closes the P-25 whitelist bypass
            # for every adapter at the shared applier (deterministic, §0.5).
            if action in ("reinforce", "prune", "graduate", "reattribute", "merge", "supersede") and mode != "curate":
                reject(f"[distill-parse] skip {action}: id-mutation not allowed in {mode} mode (add-only)\n")
                continue

            # Periodic curation runs without conversation evidence; adopting orphan
            # records under that blindness is guesswork (2026-08-13 field run:
            # 40 foreign records absorbed). The dispatcher passes --deny-reattribute
            # for that mode so the denial is enforced on untrusted worker output,
            # not merely requested in the prompt.
            if action == "reattribute" and deny_reattribute:
                reject("[distill-parse] skip reattribute: denied in periodic curation\n")
                continue

            if action == "add":
                tier = rec.get("tier")
                rtype = rec.get("type")
                body = rec.get("body")
                if tier not in ("working", "durable"):
                    reject(f"[distill-parse] skip bad tier: {tier!r}\n")
                    continue
                if not isinstance(rtype, str) or rtype not in AUTOMATIC_TYPES:
                    reject(f"[distill-parse] skip unsupported automatic type: {rtype!r}\n")
                    continue
                if not isinstance(body, str) or not body:
                    reject("[distill-parse] skip missing/empty body\n")
                    continue
                if len(body) > 2000:
                    reject(f"[distill-parse] skip body too long ({len(body)})\n")
                    continue
                headline = rec.get("headline")
                if not isinstance(headline, str) or not headline.strip() or len(headline) > 240:
                    reject("[distill-parse] skip missing/invalid headline\n")
                    continue
                capsule = {}
                capsule_ok = True
                for field in CAPSULE_LISTS:
                    value = rec.get(field, [])
                    if (not isinstance(value, list) or len(value) > 24
                            or not all(isinstance(item, str) and item.strip() and len(item) <= 160
                                       for item in value)):
                        reject(f"[distill-parse] skip invalid {field}\n")
                        capsule_ok = False
                        break
                    capsule[field] = value
                if not capsule_ok:
                    continue
                if rtype == "artifact-pointer" and not capsule["artifact_refs"]:
                    reject("[distill-parse] skip artifact-pointer without artifact_refs\n")
                    continue
                argv = ["python3", mem_path, "add", tier, rtype, body, "--headline", headline]
                for field, option in CAPSULE_LISTS.items():
                    for value in capsule[field]:
                        argv.extend([option, value])
                commands.append(argv)

            elif action in ("reinforce", "prune", "graduate", "reattribute"):
                rid = rec.get("id")
                if not isinstance(rid, str) or not rid:
                    reject(f"[distill-parse] skip {action}: missing id\n")
                    continue
                if not member(rid):
                    reject(f"[distill-parse] skip non-destructive-allowlist id ({action}): {rid!r}\n")
                    continue
                if action == "graduate":
                    commands.append(["python3", mem_path, "graduate", rid, "--to", "durable"])
                else:
                    commands.append(["python3", mem_path, action, rid])

            elif action == "merge":
                ids = rec.get("ids")
                canonical = rec.get("canonical")
                if (not isinstance(ids, list) or len(ids) < 2
                        or not all(isinstance(i, str) and i for i in ids)):
                    reject("[distill-parse] skip merge: bad ids\n")
                    continue
                if not isinstance(canonical, str) or canonical not in ids:
                    reject("[distill-parse] skip merge: bad canonical\n")
                    continue
                if not all(member(i) for i in ids):
                    reject("[distill-parse] skip merge: id outside destructive allowlist\n")
                    continue
                commands.append(["python3", mem_path, "merge", "--canonical", canonical, *ids])

            elif action == "supersede":
                rid = rec.get("id")
                by_rid = rec.get("by")
                if not all(isinstance(value, str) and value for value in (rid, by_rid)):
                    reject("[distill-parse] skip supersede: missing id/by\n")
                    continue
                if not member(rid) or not member(by_rid):
                    reject("[distill-parse] skip supersede: id outside destructive allowlist\n")
                    continue
                commands.append(["python3", mem_path, "supersede", rid, "--by", by_rid])

            else:
                reject(f"[distill-parse] skip unknown action: {action!r}\n")
    except InvalidOutput:
        sys.stderr.write("[distill-parse] invalid-output\n")
        return 2

    # No strict action can run before all output and allowlist checks succeed.
    for command in commands:
        try:
            result = subprocess.run(command, env=mem_env)
        except OSError:
            if not strict_output:
                raise
            sys.stderr.write("[distill-apply] memory-command-unavailable\n")
            return 1
        if strict_output and result.returncode:
            sys.stderr.write("[distill-apply] memory-command-failed\n")
            return 1

    return 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("out_path")
    parser.add_argument("mem_path")
    parser.add_argument("--mode", choices=("increment", "curate"), default="increment")
    parser.add_argument("--snapshot-ids", default="")
    parser.add_argument("--deny-reattribute", action="store_true",
                        help="Reject reattribute actions (periodic curation)")
    parser.add_argument("--strict-output", action="store_true",
                        help="Reject invalid automatic batches before applying; preserve failed captures")
    args = parser.parse_args(argv)
    return apply_actions(args.out_path, args.mem_path, args.mode, args.snapshot_ids,
                         deny_reattribute=args.deny_reattribute,
                         strict_output=args.strict_output)


if __name__ == "__main__":
    raise SystemExit(main())
