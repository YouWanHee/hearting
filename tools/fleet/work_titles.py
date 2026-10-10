"""One readable work-name rule; metadata IO stays in Fleet's detail pass."""
import re
import unicodedata
from pathlib import Path
from types import SimpleNamespace


def _text(value, limit=400):
    if not isinstance(value, str):
        return ''
    value = unicodedata.normalize('NFC', value).strip()
    if not value or len(value) > limit or any(
            unicodedata.category(c) in ('Cc', 'Zl', 'Zp') for c in value):
        return ''
    return value


def readable(value):
    """Keep prose intact; shorten an internal locator without inventing a title."""
    value = _text(value)
    if not value:
        return ''
    if re.search(r'\s', value):
        return value
    value = re.sub(r'^(?:run:|job:)', '', value)
    value = re.sub(r'^\d{4}-\d{2}-\d{2}[_-]', '', value)
    value = re.sub(r'[-_]\d{8}[-_]\d{6}(?:[-_].*)?$', '', value)
    value = re.sub(r'[-_](?:20\d{6}|(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))(?:[-_]\d+)?$', '', value)
    value = re.sub(r'[-_](?:att|rt|cyc)\b.*$', '', value)
    return re.sub(r'[-_]+', ' ', value).strip()[:80]


def resource_name(child):
    return _text(getattr(child, 'display_title', None)) or readable(
        child.route_node or child.node or child.run_id)


def subject_command(subject, command):
    """Do not repeat a command already contained in the work name."""
    command = _text(command)
    return subject if not command or command.casefold() in subject.casefold() else subject + ' ' + command


def cycle_title(root, record, read_json):
    """Use existing verified inventory and metadata validation, without writes."""
    from .collectors import dispatch
    cid, camp = record.get('cycle_id'), record.get('campaign_id')
    cycle, campaign, declarations = {}, {}, {}
    if cid and camp and dispatch.artifact_reader is not None:
        try:
            import artifact_meta
            mapping, rows = dispatch.artifact_reader._scan_index(Path(root))
            rel = mapping.get(camp)
            if rel and rows.get(cid, {}).get('campaign') == camp:
                doc = read_json(str(Path(root) / rel / 'meta.json'))
                identity = read_json(str(Path(root) / '.runtime/artifact-admission/v1/root-identity.json')) or {}
                if isinstance(doc, dict) and identity.get('artifact_root_id'):
                    members = {key: {'campaign_id': row.get('campaign')} for key, row in rows.items()}
                    try:
                        foreign = artifact_meta._validate_meta_doc(doc, identity['artifact_root_id'], camp, members)
                        if not foreign:
                            cycle = doc.get('cycles', {}).get(cid, {})
                            campaign = doc.get('campaign', {})
                    except artifact_meta.MetaError:
                        pass
                for filename, kind, identifier in (
                        ('cycle-display-titles.json', 'cycle_id', cid),
                        ('campaign-display-titles.json', 'campaign_id', camp)):
                    sidecar = read_json(str(Path(root) / '.runtime/artifact-producer/v1' / filename)) or {}
                    if identity.get('artifact_root_id') and sidecar.get('artifact_root_id') == identity['artifact_root_id']:
                        declarations[kind] = next((_text(row.get('display_title'), 120) for row in sidecar.get('entries', ())
                                         if isinstance(row, dict) and row.get(kind) == identifier
                                         and row.get('campaign_id') == camp), '')
        except Exception:
            pass
    # An automatically derived title equal to the slug is not a human title.
    recorded = _text(record.get('title'), 120)
    if recorded in (record.get('slug'), record.get('locator')):
        recorded = ''
    return next((value for value in (
        _text(cycle.get('title'), 120), _text(cycle.get('summary')), declarations.get('cycle_id'),
        recorded, _text(campaign.get('title'), 120), _text(campaign.get('summary')),
        declarations.get('campaign_id'),
        readable(record.get('slug') or record.get('title'))) if value), '')


def annotate(sessions, jobs, resources):
    """Name exact historical routes as well as current jobs and resources."""
    from .collectors import dispatch
    targets, results = list(jobs), []
    children = {id(child): child for child in resources}
    for entity in list(sessions) + list(jobs):
        projection = getattr(entity, 'work_projection', None)
        values = [getattr(projection, 'result', None)]
        values.extend(node.get('result') for node in
                      (getattr(entity, 'route_chain', None) or {}).get('nodes', ()))
        for result in values:
            if isinstance(result, dict) and result.get('route_id') and result.get('artifact_root'):
                target = SimpleNamespace(route_id=result['route_id'], artifact_root=result['artifact_root'])
                targets.append(target)
                results.append((result, target))
        children.update((id(child), child) for child in getattr(entity, 'resource_children', ()))
    targets.extend(children.values())
    dispatch._campaign_labels(targets)
    for result, target in results:
        result['name'] = getattr(target, 'campaign_label', None) or readable(result.get('name'))
    for child in children.values():
        child.display_title = getattr(child, 'campaign_label', None) or None


def resource_identity(child):
    return tuple(getattr(child, field, None) for field in (
        'run_id', 'pid', 'starttime', 'command_hash', 'parent_attempt_id',
        'artifact_root', 'route_id', 'route_hash', 'route_node', 'node'))
