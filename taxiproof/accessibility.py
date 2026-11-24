"""Accessible-ride service chain: prove which vehicle served a booking, if any.

A booking is served only by a sequentially closed chain: a lift deployment
inside the booking window, on a vehicle whose driver shift and trip times
(connected from :mod:`taxiproof.rules`) actually cover it, independent
handover evidence that *this* passenger boarded, and wheelchair securement
completed in that order.  A single device deployment, a single-party
signature or another passenger's action does not close the chain, and a
vehicle number in telemetry is a claim, not a certificate.  Concurrent
bookings in the same period hold exclusive occupancy of vehicle and device;
late re-dispatch or maintenance evidence revises only the affected booking
through :class:`ServiceLedger`, which keeps the original record and the
revocation reason.
"""
from . import rules
from .lift_capture import ingest

CHAIN_LINKS = ('window', 'shift', 'boarding', 'securement', 'dispatch')
WITNESS_KINDS = ('camera', 'passenger_count', 'signoff')


def _norm(value, date):
    return value if 'T' in value or not date else f'{date}T{value}'


def _assignments(shifts):
    latest = rules.recent(list(shifts), 'shift')
    return [(shift['id'], assignment) for shift in latest.values()
            for assignment in shift.get('assignments', [])]


def _trips(trips):
    return list(rules.recent(list(trips), 'trip').values())


def _chain(booking, vehicle, actions, attestations, assignments, trips, window):
    """Check every link of the service chain for one candidate vehicle."""
    date = booking.get('date', '')
    ws, we = window

    def at(item):
        return _norm(item.get('at', ''), date)

    deploys = [a for a in actions
               if a['stage'] == 'deployed' and vehicle in a['vehicles']
               and ws <= at(a) <= we and not a['self_check'] and not a['purpose_conflict']]
    deploy = min(deploys, key=at, default=None)
    secure = None
    if deploy:
        secured = [a for a in actions
                   if a['stage'] == 'secured' and vehicle in a['vehicles']
                   and ws <= at(a) <= we and at(a) >= at(deploy)
                   and a['verified'] and not a['self_check']
                   and (not a['passengers'] or booking['passenger'] in a['passengers'])]
        secure = min(secured, key=at, default=None)
    low = at(deploy) if deploy else ws
    high = at(secure) if secure else we
    witnesses = [w for w in attestations
                 if w.get('vehicle') == vehicle and w.get('kind') in WITNESS_KINDS
                 and w.get('verified') and low <= at(w) <= high]
    usable = [w for w in witnesses if not w.get('passenger') or w['passenger'] == booking['passenger']]
    named = [w for w in usable if w.get('passenger') == booking['passenger']]
    boarding = bool(named) and len({w['kind'] for w in usable}) >= 2
    dispatch_evidence = []
    if vehicle == booking.get('vehicle'):
        dispatch = True
    else:
        limit = at(secure) if secure else we
        dispatch_evidence = [d['id'] for d in attestations
                             if d.get('kind') == 'dispatch' and d.get('booking') == booking['id']
                             and d.get('vehicle') == vehicle and d.get('verified') and at(d) <= limit]
        dispatch = bool(dispatch_evidence)
    shift = False
    shift_evidence = []
    if deploy:
        cover = at(secure) if secure else at(deploy)
        for shift_id, assignment in assignments:
            if (assignment['vehicle'] == vehicle and assignment['start'] <= at(deploy)
                    and assignment['end'] >= cover and assignment.get('license_until', '') >= cover
                    and assignment.get('permit_until', '') >= cover):
                shift = True
                shift_evidence = [shift_id]
                driver = assignment['driver']
                break
        if shift and trips:
            matched = [t for t in trips if t.get('vehicle') == vehicle
                       and t['start'] <= at(deploy) <= t['end'] and t.get('driver') == driver]
            shift = bool(matched)
            shift_evidence += [t['id'] for t in matched[:1]]
    claimed = [a for a in actions if vehicle in a['vehicles']]
    caveats = []
    if any(a['self_check'] for a in claimed):
        caveats.append('存在被可信来源判定为未载人自检的动作')
    if deploy and not deploy['verified']:
        caveats.append('部署动作缺少可信签名')
    if deploy and deploy['ambiguous_origin']:
        caveats.append('部署来源存在多种解释')
    evidence = [o['record'] for a in (deploy, secure) if a for o in a['observations']]
    evidence += [w['id'] for w in usable] + dispatch_evidence + shift_evidence
    links = {'window': bool(deploy), 'shift': shift, 'boarding': boarding,
             'securement': bool(secure), 'dispatch': dispatch}
    return {'links': links, 'closed': all(links.values()),
            'deploy': deploy['key'] if deploy else None,
            'secure': secure['key'] if secure else None,
            'devices': sorted({a['device'] for a in (deploy, secure) if a}),
            'witnesses': [w['id'] for w in usable],
            'caveats': caveats, 'evidence': sorted(set(evidence))}


def evaluate(booking, events, shifts=(), trips=(), registry=None):
    """Evaluate one booking: provable vehicle, unproven candidates, conflicts."""
    date = booking.get('date', '')
    window = (_norm(booking['window_start'], date), _norm(booking['window_end'], date))
    captured = ingest(events, registry)
    actions, attestations = captured['actions'], captured['attestations']
    assignments = _assignments(shifts)
    trip_events = _trips(trips)
    vehicles = sorted({v for a in actions for v in a['vehicles']}
                      | {a['vehicle'] for a in attestations if a.get('vehicle')})
    chains = {v: _chain(booking, v, actions, attestations, assignments, trip_events, window)
              for v in vehicles}
    proven = [v for v in vehicles if chains[v]['closed']]
    conflicts = []
    for action in actions:
        if len(action['vehicles']) > 1:
            conflicts.append({'kind': 'double_claim', 'action': action['key'],
                              'vehicles': action['vehicles']})
        origin_kinds = {origin.split(':')[0] for origin in action['origins']}
        if 'bench' in origin_kinds and 'gateway' in origin_kinds:
            conflicts.append({'kind': 'provenance_dispute', 'action': action['key'],
                              'origins': action['origins']})
        if action['self_check']:
            conflicts.append({'kind': 'self_check', 'action': action['key'],
                              'vehicles': action['vehicles']})
    ws, we = window
    for vehicle in vehicles:
        if chains[vehicle]['links']['window']:
            continue
        orphans = [w['id'] for w in attestations
                   if w.get('vehicle') == vehicle and w.get('kind') in WITNESS_KINDS
                   and w.get('verified') and w.get('passenger') == booking['passenger']
                   and ws <= _norm(w.get('at', ''), date) <= we]
        if orphans:
            conflicts.append({'kind': 'orphan_witnesses', 'vehicle': vehicle, 'witnesses': orphans})
    if len(proven) > 1:
        status, proven_vehicle = 'conflict', None
        conflicts.append({'kind': 'double_service', 'vehicles': proven})
    elif proven:
        status, proven_vehicle = 'proven', proven[0]
    else:
        status, proven_vehicle = 'unproven', None
    candidates = [{'vehicle': v, 'actions': [a['key'] for a in actions if v in a['vehicles']],
                   'missing': [link for link in CHAIN_LINKS if not chains[v]['links'][link]],
                   'caveats': chains[v]['caveats']}
                  for v in vehicles if v != proven_vehicle]
    if status == 'proven':
        reason = (f'{proven_vehicle} 服务链顺序闭合：窗口内部署、班次行程覆盖、'
                  f'{booking["passenger"]} 独立交接、轮椅固定完成')
    elif status == 'conflict':
        reason = '证据冲突，需授权调查裁定'
    else:
        missing = '; '.join(f"{c['vehicle']} 缺 {','.join(c['missing'])}" for c in candidates)
        reason = '未形成闭合服务链：' + (missing or '无候选车辆')
    return {'booking': booking['id'], 'passenger': booking['passenger'],
            'status': status, 'proven_vehicle': proven_vehicle,
            'chains': chains, 'candidates': candidates, 'conflicts': conflicts,
            'evidence': sorted({e for v in vehicles for e in chains[v]['evidence']}),
            'rejected': captured['rejected'], 'reason': reason}


def _overlaps(a, b):
    return a[0] == b[0] and a[1] < b[2] and b[1] < a[2]


def evaluate_all(bookings, events, shifts=(), trips=(), registry=None):
    """Evaluate concurrent bookings with exclusive vehicle/device occupancy."""
    bookings = list(bookings)
    evaluations = {b['id']: evaluate(b, events, shifts, trips, registry) for b in bookings}
    windows = {b['id']: (b.get('date', ''), b['window_start'], b['window_end']) for b in bookings}
    locks = {}
    for booking in sorted(bookings, key=lambda item: (item['window_start'], item['id'])):
        evaluation = evaluations[booking['id']]
        if evaluation['status'] != 'proven':
            continue
        vehicle = evaluation['proven_vehicle']
        resources = ['vehicle:' + vehicle]
        resources += ['device:' + d for d in evaluation['chains'][vehicle]['devices']]
        held = [r for r in resources if r in locks and _overlaps(windows[booking['id']], windows[locks[r]])]
        if held:
            evaluation['status'] = 'conflict'
            evaluation['proven_vehicle'] = None
            evaluation['reason'] = '车辆或设备被时段重叠的预约排他占用'
            for resource in held:
                holder = locks[resource]
                evaluation['conflicts'].append({'kind': 'occupancy', 'resource': resource,
                                                'held_by': holder})
                evaluations[holder]['conflicts'].append({'kind': 'occupancy', 'resource': resource,
                                                         'challenged_by': booking['id']})
        else:
            for resource in resources:
                locks[resource] = booking['id']
    return evaluations


def served(booking, events, shifts=(), trips=(), registry=None):
    """True only when the booking's accessible service chain is provably closed."""
    return evaluate(booking, events, shifts, trips, registry)['status'] == 'proven'


class ServiceLedger:
    """Append-only revision history per booking.

    Late evidence revises only the booking it concerns; the superseded
    record is kept together with the revocation reason and the evidence
    that caused it.
    """

    def __init__(self):
        self._history = {}

    def apply(self, evaluation, note='', revised_by=()):
        booking = evaluation['booking']
        history = self._history.setdefault(booking, [])
        previous = history[-1] if history else None
        if (previous and previous['status'] == 'proven' and not previous['revoked']
                and (evaluation['status'] != 'proven'
                     or evaluation['proven_vehicle'] != previous['vehicle'])):
            previous['revoked'] = True
            previous['revocation_reason'] = note or '后续证据修订'
            previous['revoked_by'] = list(revised_by)
        record = {'booking': booking, 'revision': len(history) + 1,
                  'status': evaluation['status'], 'vehicle': evaluation['proven_vehicle'],
                  'note': note, 'evidence': list(evaluation['evidence']),
                  'revoked': False, 'revocation_reason': None, 'revoked_by': []}
        history.append(record)
        return record

    def current(self, booking):
        history = self._history.get(booking, [])
        return history[-1] if history else None

    def history(self, booking):
        return [dict(record) for record in self._history.get(booking, [])]
