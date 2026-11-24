"""Physical lift actions and the device provenance graph.

``deployments`` only lists candidate actions that *claim* a vehicle — a
vehicle number in telemetry is not a service certificate.  Proof of an
accessible ride is built in :mod:`taxiproof.accessibility`.
"""
from .lift_capture import ingest


def physical_actions(events, registry=None):
    """Deduplicated, clock-ordered physical lift actions with provenance."""
    return ingest(events, registry)['actions']


def provenance_graph(events, registry=None):
    """Graph of devices, reporting endpoints and claimed vehicles per action."""
    nodes = {}
    edges = []
    for action in physical_actions(events, registry):
        device = 'device:' + action['device']
        nodes[device] = {'id': device, 'kind': 'device'}
        for observation in action['observations']:
            source = observation['source'] or 'unknown'
            kind = source.split(':')[0] if ':' in source else 'unknown'
            nodes.setdefault(source, {'id': source, 'kind': kind})
            edges.append({'from': device, 'to': source, 'relation': 'reported_by',
                          'action': action['key'], 'record': observation['record']})
            if observation['vehicle']:
                vehicle = 'vehicle:' + observation['vehicle']
                nodes.setdefault(vehicle, {'id': vehicle, 'kind': 'vehicle'})
                edges.append({'from': source, 'to': vehicle, 'relation': 'claims',
                              'action': action['key'], 'record': observation['record']})
    return {'nodes': [nodes[key] for key in sorted(nodes)], 'edges': edges}


def deployments(events, vehicle, registry=None):
    """Candidate deployments attributed to *vehicle* — claims, not proof."""
    return [action for action in physical_actions(events, registry)
            if action['stage'] == 'deployed' and vehicle in action['vehicles']]
