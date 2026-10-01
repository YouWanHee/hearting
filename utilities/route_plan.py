"""The framed route's decision record (`route_decision_v1`) and the framed-shape predicate.

A framed route (capability `route-frame`, shape `framed`) ends in one runtime terminal,
`route-decision`, that no model runs. The runtime fixes one record as that terminal's
evidence: an immutable `decision` part with its canonical digest, and a separate,
monotonic `first_leg` part that may be added once and never changed. The digest covers
the `decision` part only, so binding a first leg never invalidates the record.

It also reads a frame brief's section 8 (`route_proposal_v1`), validates the proposal
through a caller-supplied memory compile, reads a `--route-plan <record>#<index>` argument,
and projects `next_leg`. This module has no CLI, no gate and no recovery command; it only
builds, renders, reads and checks, and none of its functions write a file.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import sys
import textwrap
from pathlib import Path

SCHEMA = "route_decision_v1"
CAPABILITY = "route-frame"
SHAPE = "framed"
NODE_IDS = ("frame", "frame-alternative", "route-decision")
TERMINAL_NODE = "route-decision"
RECORD_RELATIVE = "shards/frame/route-decision.json"
MAX_RECORD_BYTES = 262144
NONE = "none"
NO_PROPOSAL_READ = "proposal-not-read"
_DECISION_KEYS = frozenset((
    "frame_route", "selected", "reason", "proposal", "proposals", "briefs", "intent",
    "approvals", "first_leg_compose"))
_FRAME_NODES = ("frame", "frame-alternative")
ROOT = Path(__file__).resolve().parents[1]

PROPOSAL_SCHEMA = "route_proposal_v1"
MAX_BRIEF_BYTES = 262144
MAX_SECTION_BYTES = 16384
MAX_LEGS = 4
MAX_APPROVALS = 4
MAX_TEXT = 300
MAX_YAML_EVENTS = 600
SHAPES = ("direct", "solo", "staged")
APPROVAL_KEYS = ("full-run", "deploy", "handback")
VALID = "valid"
_SLUG = re.compile(r"[a-z][a-z0-9-]{0,31}")
_CAPABILITY = re.compile(r"[a-z][a-z0-9-]{0,63}")
_MODE = re.compile(r"[a-z][a-z0-9-]{0,31}")
_GRAPH_TOKEN = re.compile(r"[a-z][a-z0-9-]*(?::[a-z0-9][a-z0-9/_.-]*){0,2}")
_SECTION_HEAD = re.compile(r"^[ \t]{0,3}(?:#{1,6}[ \t]*)?(?:\*\*)?[ \t]*8[ \t]*[.):]?[ \t]*(?:\*\*)?[^\n]*경로 조립 제안", re.M)
_NEXT_SECTION = re.compile(r"^[ \t]{0,3}(?:#{1,6}[ \t]*)?(?:\*\*)?[ \t]*(?:9|1[0-9])[ \t]*[.):]", re.M)
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*([A-Za-z0-9_-]*)[ \t]*$")


def is_framed_route(route) -> bool:
    """The exact framed route shape: nothing else may take the model-less terminal path."""
    if not isinstance(route, dict) or route.get("capability") != CAPABILITY:
        return False
    if (route.get("selection") or {}).get("shape") != SHAPE or route.get("effective_intensity") != "standard":
        return False
    nodes = route.get("nodes")
    return (isinstance(nodes, list) and tuple(n.get("id") for n in nodes) == NODE_IDS
            and nodes[-1].get("kind") == "runtime-terminal" and nodes[-1].get("terminal") is True)


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def decision_digest(decision) -> str:
    """`sha256:<hex>` of the decision part's canonical bytes; `first_leg` is never part of it."""
    return "sha256:" + hashlib.sha256(_canonical(decision)).hexdigest()


def file_digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_decision(*, frame_route, selected, reason, briefs, intent, proposal=None, proposals=None,
                   approvals=None, first_leg_compose=None) -> dict:
    """The immutable `decision` part. `selected` is an option label or `none`."""
    if not isinstance(selected, str) or not selected:
        raise ValueError("route-decision-invalid:selected")
    if not isinstance(reason, str) or (selected == NONE and not reason):
        raise ValueError("route-decision-invalid:reason")
    return {
        "frame_route": dict(frame_route),
        "selected": selected,
        "reason": reason,
        "proposal": proposal,
        "proposals": proposals if proposals is not None else [],
        "briefs": [dict(item) for item in briefs],
        "intent": dict(intent),
        "approvals": approvals if approvals is not None else {},
        "first_leg_compose": first_leg_compose,
    }


def none_decision(*, frame_route, briefs, intent, reason=NO_PROPOSAL_READ) -> dict:
    """The minimal ending: no proposal was selected, so no leg starts and the main session composes next."""
    return build_decision(
        frame_route=frame_route, selected=NONE, reason=reason, briefs=briefs, intent=intent,
        proposals=[{"node": node, "proposal": None, "reason": reason} for node in _FRAME_NODES])


def build_record(decision, first_leg=None) -> dict:
    record = {"schema": SCHEMA, "decision": decision, "digest": decision_digest(decision)}
    if first_leg is not None:
        record["first_leg"] = first_leg
    return record


def render(record) -> bytes:
    """The record's exact file bytes; the same record always renders the same bytes."""
    return (json.dumps(record, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def validate_record(record) -> dict:
    """Return `record` when it is a well-formed `route_decision_v1`; else raise ValueError."""
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        raise ValueError("route-decision-invalid:schema")
    if set(record) - {"schema", "decision", "digest", "first_leg"} or {"decision", "digest"} - set(record):
        raise ValueError("route-decision-invalid:fields")
    decision = record["decision"]
    if not isinstance(decision, dict) or set(decision) != _DECISION_KEYS:
        raise ValueError("route-decision-invalid:decision")
    if record["digest"] != decision_digest(decision):
        raise ValueError("route-decision-invalid:digest")
    frame_route = decision["frame_route"]
    if not isinstance(frame_route, dict) or set(frame_route) != {"route_id", "route_hash", "cycle_id"}:
        raise ValueError("route-decision-invalid:frame_route")
    if not isinstance(decision["selected"], str) or not decision["selected"]:
        raise ValueError("route-decision-invalid:selected")
    if decision["selected"] == NONE and (not decision["reason"] or decision["proposal"] is not None):
        raise ValueError("route-decision-invalid:none")
    if decision["selected"] != NONE and (not isinstance(decision["proposal"], dict)
                                         or not isinstance(decision["first_leg_compose"], dict)):
        raise ValueError("route-decision-invalid:selected")
    if "first_leg" in record and (decision["selected"] == NONE or not isinstance(record["first_leg"], dict)):
        raise ValueError("route-decision-invalid:first_leg")
    return record


def read_record(path) -> dict:
    """Read and validate a record file; it must be a small regular, non-symlink file."""
    path = Path(path)
    try:
        meta = os.lstat(path)
        if not stat.S_ISREG(meta.st_mode) or meta.st_size > MAX_RECORD_BYTES:
            raise ValueError("route-decision-invalid:file")
        return validate_record(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("route-decision-invalid:unreadable") from exc


def bind_first_leg(record, first_leg) -> dict:
    """Add the `first_leg` part once, or confirm the same value again; any other value conflicts.

    The decision part and its digest are never touched. S2 fills `first_leg`; a `none`
    decision has no first leg.
    """
    validate_record(record)
    if record["decision"]["selected"] == NONE:
        raise ValueError("route-decision-invalid:first_leg")
    current = record.get("first_leg")
    if current is None:
        return {**record, "first_leg": first_leg}
    merged = {**current, **first_leg}
    if any(current.get(key) != value for key, value in first_leg.items() if key in current):
        raise ValueError("route-decision-conflict:first_leg")
    return {**record, "first_leg": merged}



# --- Section 8: the route proposal ------------------------------------------------------

class ProposalError(ValueError):
    """A reason the brief holds no usable proposal; the caller shows `proposal:none(<reason>)`."""


def none_text(reason) -> str:
    return f"proposal:none({reason})"


def _section8(text: str) -> str:
    head = _SECTION_HEAD.search(text)
    if head is None:
        raise ProposalError("section-missing")
    rest = text[head.end():]
    tail = _NEXT_SECTION.search(rest)
    section = rest[:tail.start()] if tail else rest
    if len(section.encode("utf-8")) > MAX_SECTION_BYTES:
        raise ProposalError("section-too-large")
    return section


def _proposal_blocks(section: str) -> list:
    blocks, marker, info, buffer = [], None, "", []
    for line in section.splitlines():
        fence = _FENCE.match(line)
        if marker is None:
            if fence:
                marker, info, buffer = fence.group(1), fence.group(2).lower(), []
            continue
        if fence and not fence.group(2) and fence.group(1)[0] == marker[0] and len(fence.group(1)) >= len(marker):
            if info in ("yaml", "yml") and PROPOSAL_SCHEMA in "\n".join(buffer):
                blocks.append("\n".join(buffer))
            marker = None
            continue
        buffer.append(line)
    return blocks


def _load_yaml(text: str):
    try:
        import yaml
    except ImportError as exc:
        raise ProposalError("yaml-unavailable") from exc

    class Strict(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=True)
            if key in seen:
                raise yaml.constructor.ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    try:
        count = 0
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            count += 1
            if count > MAX_YAML_EVENTS:
                raise ProposalError("yaml-too-large")
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise ProposalError("yaml-alias")
            tag = getattr(event, "tag", None)
            if tag and not tag.startswith("tag:yaml.org,2002:"):
                raise ProposalError("yaml-tag")
        return yaml.load(text, Loader=Strict)
    except yaml.YAMLError as exc:
        raise ProposalError("yaml-invalid") from exc


def _text(value, *, name, required=False):
    if value is None or value == "":
        if required:
            raise ProposalError(f"schema-invalid:{name}")
        return None
    if not isinstance(value, str) or len(value) > MAX_TEXT or not value.strip():
        raise ProposalError(f"schema-invalid:{name}")
    return value.strip()


def _normal_leg(raw, index):
    if not isinstance(raw, dict) or set(raw) - {"capability", "mode", "shape", "graph", "intensity", "why"}:
        raise ProposalError(f"schema-invalid:legs[{index}]")
    capability, shape = raw.get("capability"), raw.get("shape")
    if not isinstance(capability, str) or not _CAPABILITY.fullmatch(capability):
        raise ProposalError(f"schema-invalid:legs[{index}].capability")
    if shape not in SHAPES:
        raise ProposalError(f"schema-invalid:legs[{index}].shape")
    mode, intensity = raw.get("mode"), raw.get("intensity")
    if mode is not None and (not isinstance(mode, str) or not _MODE.fullmatch(mode)):
        raise ProposalError(f"schema-invalid:legs[{index}].mode")
    if intensity is not None and (not isinstance(intensity, str) or not _MODE.fullmatch(intensity)):
        raise ProposalError(f"schema-invalid:legs[{index}].intensity")
    graph = raw.get("graph")
    if graph is not None:
        if shape != "staged":
            raise ProposalError(f"schema-invalid:legs[{index}].graph-only-staged")
        if not isinstance(graph, list) or not 1 <= len(graph) <= 24:
            raise ProposalError(f"schema-invalid:legs[{index}].graph")
        for token in graph:
            if not isinstance(token, str) or not _GRAPH_TOKEN.fullmatch(token):
                raise ProposalError(f"schema-invalid:legs[{index}].graph")
            if set(token.split(":")[:2]) & set(_FRAME_NODES):
                raise ProposalError(f"leg-invalid:{index}:frame-stage-not-allowed")
    return {"capability": capability, "mode": mode, "shape": shape, "graph": list(graph) if graph else None,
            "intensity": intensity, "why": _text(raw.get("why"), name=f"legs[{index}].why")}


def parse_proposal(text: str) -> dict:
    """The one `route_proposal_v1` of a brief's section 8, normalized; ProposalError when none."""
    return parse_proposal_with_source(text)[0]


def parse_proposal_with_source(text: str) -> tuple:
    """`(proposal, source)`: the normalized proposal and the fenced block's own text."""
    if len(text.encode("utf-8")) > MAX_BRIEF_BYTES:
        raise ProposalError("brief-too-large")
    blocks = _proposal_blocks(_section8(text))
    if not blocks:
        raise ProposalError("block-missing")
    if len(blocks) > 1:
        raise ProposalError("block-multiple")
    source = textwrap.dedent(blocks[0])
    document = _load_yaml(source)
    body = document.get(PROPOSAL_SCHEMA) if isinstance(document, dict) and len(document) == 1 else None
    if not isinstance(body, dict) or set(body) - {"summary", "legs", "entry_approvals"}:
        raise ProposalError("schema-invalid:document")
    legs = body.get("legs")
    if not isinstance(legs, list) or not 1 <= len(legs) <= MAX_LEGS:
        raise ProposalError("schema-invalid:legs")
    approvals = body.get("entry_approvals") or []
    if not isinstance(approvals, list) or len(approvals) > MAX_APPROVALS:
        raise ProposalError("schema-invalid:entry_approvals")
    rows = []
    for raw in approvals:
        if (not isinstance(raw, dict) or set(raw) != {"key", "leg", "question"}
                or raw["key"] not in APPROVAL_KEYS or isinstance(raw["leg"], bool)
                or not isinstance(raw["leg"], int) or not 0 <= raw["leg"] < len(legs)
                or not isinstance(raw["question"], str) or not _SLUG.fullmatch(raw["question"])):
            raise ProposalError("schema-invalid:entry_approvals")
        rows.append({"key": raw["key"], "leg": raw["leg"], "question": raw["question"]})
    return ({"summary": _text(body.get("summary"), name="summary", required=True),
             "legs": [_normal_leg(raw, index) for index, raw in enumerate(legs)],
             "entry_approvals": rows}, source)


def leg_arguments(leg) -> dict:
    """The compose-shaped keyword arguments of one normalized proposal leg."""
    return {"capability": leg["capability"], "capability_mode": leg.get("mode"), "shape": leg["shape"],
            "graph": ",".join(leg["graph"]) if leg.get("graph") else None, "intensity": leg.get("intensity")}


def validate_proposal(proposal, *, compile_leg, start_approvals) -> dict:
    """Compile every leg through `compile_leg(leg, index) -> route` (a memory compile that writes,
    starts and records nothing) and reconcile `entry_approvals` with the parts the legs really carry.

    Returns `{"legs": [facts], "start_approvals": [{leg, key, part, node}]}`; raises ProposalError
    for the first invalid leg, so one bad leg makes the whole proposal none.
    """
    facts, approvals = [], []
    for index, leg in enumerate(proposal["legs"]):
        try:
            route = compile_leg(leg, index)
        except ValueError as exc:
            raise ProposalError(f"leg-invalid:{index}:{str(exc).strip().splitlines()[0][:120] if str(exc).strip() else 'rejected'}") from exc
        composed = (route.get("composed_recipe") or {}).get("compose") or {}
        facts.append({"capability": route["capability"], "mode": route["capability_mode"],
                      "shape": (route.get("selection") or {}).get("shape"),
                      "graph": list(composed.get("graph") or []) or None,
                      "intensity": route["effective_intensity"]})
        approvals.extend({"leg": index, **row} for row in start_approvals(route))
    for row in proposal["entry_approvals"]:
        if not any(item["leg"] == row["leg"] and item["start_approval"] == row["key"] for item in approvals):
            raise ProposalError(f"entry-approval-mismatch:{row['key']}@{row['leg']}")
    return {"legs": facts, "start_approvals": approvals}


def evaluate_brief(path, *, root, node, compile_leg, start_approvals) -> dict:
    """One brief's proposal row: `{node, proposal|None, reason, brief_path, sha256, facts?}`.

    `reason` is `valid` or the `proposal:none(<reason>)` reason. A missing or unreadable
    brief is none too; nothing here raises.
    """
    path = Path(path)
    row = {"node": node, "proposal": None, "reason": "", "brief_path": _relative(path, root), "sha256": ""}
    try:
        raw = path.read_bytes()
        row["sha256"] = hashlib.sha256(raw).hexdigest()
        if len(raw) > MAX_BRIEF_BYTES:
            raise ProposalError("brief-too-large")
        proposal, source = parse_proposal_with_source(raw.decode("utf-8"))
        facts = validate_proposal(proposal, compile_leg=compile_leg, start_approvals=start_approvals)
    except ProposalError as exc:
        row["reason"] = str(exc)
    except (OSError, UnicodeError):
        row["reason"] = "brief-unreadable"
    else:
        row.update(proposal=proposal, reason=VALID, facts=facts, source=source)
    return row


def _relative(path, root) -> str:
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return str(path)


def scope_key(row) -> list:
    """The approval scope a valid proposal row carries, in a comparable form."""
    return sorted((item["leg"], item["start_approval"], item["part"]) for item in row["facts"]["start_approvals"])


def proposals_equal(rows) -> bool:
    """Plan decision D5: two valid proposals are equal when their normalized legs and their
    start-approval scope match. Summary and `why` wording is not part of it."""
    if len(rows) != 2 or any(row["proposal"] is None for row in rows):
        return False
    first, second = rows
    return first["facts"]["legs"] == second["facts"]["legs"] and scope_key(first) == scope_key(second)


def wording_differs(rows) -> bool:
    if len(rows) != 2 or any(row["proposal"] is None for row in rows):
        return False
    first, second = (row["proposal"] for row in rows)
    return (first["summary"] != second["summary"]
            or [leg["why"] for leg in first["legs"]] != [leg["why"] for leg in second["legs"]])


def same_proposal(left, right) -> bool:
    """Whether two proposals name the same legs (capability, mode, shape, graph, intensity). The runtime
    matches an interview's copy to the proposal it validated itself this way; the copy's approval
    questions are the interview's own and are not compared."""
    try:
        return ([{key: leg.get(key) for key in ("capability", "mode", "shape", "graph", "intensity")} for leg in left["legs"]]
                == [{key: leg.get(key) for key in ("capability", "mode", "shape", "graph", "intensity")}
                    for leg in right["legs"]])
    except (KeyError, TypeError):
        return False


# --- `--route-plan <record>#<index>` ----------------------------------------------------

def parse_route_plan_arg(text) -> tuple:
    path, separator, index = str(text or "").rpartition("#")
    if not separator or not path or not index.isdigit() or len(index) > 2:
        raise ValueError("route-plan-invalid:argument")
    return path, int(index)


def read_route_plan(text, artifact_root) -> dict:
    """Read a `<record>#<index>` argument against `artifact_root`.

    Returns `{decision, digest, index, leg, legs, record}`; raises ValueError when the record is
    unreadable, is not a selected decision, lies outside the root, or `index` is out of range.
    The caller seals only `decision`, `digest` and `index`.
    """
    arg_path, index = parse_route_plan_arg(text)
    root = Path(artifact_root).resolve()
    path = Path(arg_path)
    path = (path if path.is_absolute() else root / path)
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("route-plan-invalid:path")
    record = read_record(path.resolve())
    decision = record["decision"]
    proposal = decision["proposal"]
    if decision["selected"] == NONE or not isinstance(proposal, dict):
        raise ValueError("route-plan-invalid:none")
    legs = proposal.get("legs")
    if not isinstance(legs, list) or not 0 <= index < len(legs):
        raise ValueError("route-plan-invalid:index")
    return {"decision": path.resolve().relative_to(root).as_posix(), "digest": record["digest"],
            "index": index, "leg": legs[index], "legs": legs, "record": record}


def sealed_form(binding) -> dict:
    return {"decision": binding["decision"], "digest": binding["digest"], "index": binding["index"]}


def validate_sealed(value) -> dict:
    """The format of a route's own `route_plan` field, never the record it names."""
    if (not isinstance(value, dict) or set(value) != {"decision", "digest", "index"}
            or not isinstance(value["decision"], str) or not value["decision"] or value["decision"].startswith("/")
            or ".." in Path(value["decision"]).parts
            or not isinstance(value["digest"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value["digest"])
            or isinstance(value["index"], bool) or not isinstance(value["index"], int)
            or not 0 <= value["index"] < MAX_LEGS):
        raise ValueError("route-plan-invalid:sealed")
    return value


def display_plan(legs) -> list:
    """Fleet's `--plan` display: the legs' capability order, adjacent repeats shown once."""
    names = []
    for leg in legs:
        name = leg["capability"]
        name = name[len("autopilot-"):] if name.startswith("autopilot-") else name
        if not names or names[-1] != name:
            names.append(name)
    return names


_PIN_TARGETS = ("owner", "frame", "worker")


def pin_tokens(pins) -> list:
    """`target=harness[:model[@effort]]` for each pin of a sealed `selection_pins` map, in target order.

    What `compose --pin` reads back; `contract_version` and anything malformed are left out, so a
    route without pins gives `[]` and the printed command gains no token.
    """
    tokens = []
    for target in _PIN_TARGETS:
        pin = pins.get(target) if isinstance(pins, dict) else None
        if not isinstance(pin, dict) or not isinstance(pin.get("harness"), str):
            continue
        model, effort = pin.get("model"), pin.get("effort")
        token = f"{target}={pin['harness']}"
        if isinstance(model, str) and model:
            token += f":{model}" + (f"@{effort}" if isinstance(effort, str) and effort else "")
        tokens.append(token)
    return tokens


def frame_selection_pins(decision, artifact_root) -> dict:
    """The pins the decision's frame route was composed with, or `{}`.

    Read from that frame's own route file and trusted only when its id and hash are the ones the
    decision recorded and its bytes still hash to them; nothing is written or rewritten.
    """
    try:
        import route_identity
        reference = decision["frame_route"]
        path = Path(artifact_root).resolve() / ".runtime" / "routes" / f"{reference['route_id']}.json"
        if path.is_symlink():
            return {}
        frame = json.loads(path.read_text(encoding="utf-8"))
        if ((frame.get("route_id"), frame.get("route_hash")) != (reference["route_id"], reference["route_hash"])
                or route_identity.route_hash(frame) != reference["route_hash"]):
            return {}
        pins = frame.get("selection_pins")
        return {target: dict(pins[target]) for target in _PIN_TARGETS if isinstance(pins, dict) and target in pins}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def compose_argv(leg, *, context, route_plan_arg, parent_cycle, slug, pins=None) -> list:
    """The `capability-route.py compose` argv for one leg; `--start` is left to the main session."""
    argv = [sys.executable, str(ROOT / "utilities/capability-route.py"), "compose", "--slug", slug,
            "--shape", leg["shape"], "--capability", leg["capability"]]
    if leg.get("mode"):
        argv += ["--capability-mode", leg["mode"]]
    if leg.get("graph"):
        argv += ["--graph", ",".join(leg["graph"])]
    if leg.get("intensity"):
        argv += ["--intensity", leg["intensity"]]
    argv += ["--cwd", context["cwd"], "--artifact-root", context["artifact_root"],
             "--prompt-file", context["prompt_file"]]
    if context.get("spec_read") and context["spec_read"] != "auto":
        argv += ["--spec-read", context["spec_read"]]
    argv += ["--route-plan", route_plan_arg]
    for token in pin_tokens(pins):
        argv += ["--pin", token]
    if context.get("campaign_key"):
        argv += ["--campaign-key", context["campaign_key"]]
    argv += ["--parent-cycle", parent_cycle]
    return argv


def project_next_leg(route, completed_cycle_id):
    """`{index, leg, compose_command}` for the leg after this route's, or None.

    Information only: it is never a launch and never enters `parent_next`, `parent_next_command`
    or `required_action`. None when the route carries no `route_plan`, the record is unreadable or
    no longer matches the sealed digest, the route was the last leg, or the reusable prompt file is gone.
    """
    try:
        sealed = validate_sealed(route.get("route_plan"))
        root = Path(route["artifact_root"]).resolve()
        binding = read_route_plan(f"{root / sealed['decision']}#{sealed['index']}", root)
        if binding["digest"] != sealed["digest"] or not completed_cycle_id:
            return None
        index = sealed["index"] + 1
        if index >= len(binding["legs"]):
            return None
        context = binding["record"]["decision"]["first_leg_compose"]["context"]
        if not Path(context["prompt_file"]).is_file():
            return None
        leg = binding["legs"][index]
        # The leg's own sealed pins; a leg sealed before pins reached legs takes its frame's.
        pins = route.get("selection_pins") or frame_selection_pins(binding["record"]["decision"], root)
        argv = compose_argv(
            leg, context={**context, "campaign_key": route.get("campaign_key") or context.get("campaign_key")},
            route_plan_arg=f"{root / sealed['decision']}#{index}", parent_cycle=completed_cycle_id,
            slug=f"{context['slug']}-leg{index}", pins=pins)
        return {"index": index, "leg": leg, "compose_command": shlex.join(argv)}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def completed_cycle(route):
    """The sealed producer cycle this route ran in, or None.

    A continuation attaches to its source route's cycle, so the source chain is walked
    (bounded) through the canonical route files.
    """
    import artifact_producer as producer
    root = Path(route["artifact_root"]).resolve()
    ids, current = [route["route_id"]], route
    for _ in range(8):
        source = current.get("source_route_id")
        if not source or source in ids:
            break
        ids.append(source)
        try:
            current = json.loads((root / ".runtime" / "routes" / f"{source}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            break
    for record in producer.list_cycle_records(root):
        if record.get("route_id") in ids and record.get("state") == "sealed":
            return record
    return None


def next_leg_for_route(route):
    """`project_next_leg` for a finished route, found from its own sealed cycle; None otherwise."""
    if not isinstance(route, dict) or route.get("route_plan") is None:
        return None
    try:
        record = completed_cycle(route)
    except (OSError, ValueError, KeyError):
        return None
    return project_next_leg(route, record["cycle_id"]) if record else None
