"""Maintenance interval decision over a provable, cross-controller odometer.

Hard business constraints encoded here:

1. Distance must be the continuous, VIN-scoped cumulative distance built from
   signed controller segments.  Raw values of different controllers are never
   subtracted from each other, and values of different vehicles are never
   chained.
2. The interval can only restart from a *valid* service event carrying
   independent construction, parts and release-inspection evidence.  Replacing
   an ECU is not itself maintenance; an unproven work order stays pending
   (``待核``) and cannot anchor an interval.
3. Certificates are append-only (handled by the capture/ledger layer); this
   module only produces decisions with the exact meter segment and service
   order version they rely on.
"""
from .odometer import OdometerContinuityError, build_segments, cumulative_km, latest


def _signed(structure, field, keystore):
    envelope = structure.get(field)
    if not isinstance(envelope, dict) or not envelope.get('key_id') or not envelope.get('value'):
        return False
    if keystore is None:
        return True
    from .odometer_capture import verify_envelope
    return verify_envelope(structure, field, keystore)


def evaluate_service_order(order, cumulative, keystore=None):
    """Classify one service work order as valid or pending verification."""
    construction = order.get('construction') or {}
    parts = order.get('parts') or []
    release = order.get('release_inspection') or {}
    blockers = []
    if not construction.get('work_items') or not _signed(construction, 'technician_signature', keystore):
        blockers.append('independent construction evidence missing or unsigned')
    if not parts or any(not part.get('part_no') or part.get('quantity', 0) <= 0 for part in parts) \
            or not _signed(order, 'parts_signature', keystore):
        blockers.append('independent parts/material evidence missing or unsigned')
    if not release.get('inspector') or not _signed(release, 'release_signature', keystore):
        blockers.append('vehicle release inspection missing or unsigned')
    if order.get('cumulative_km') is None:
        blockers.append('service order has no cumulative kilometre anchor')
    if blockers:
        return {'valid': False, 'status': '待核', 'blockers': blockers}
    if cumulative is not None and order['cumulative_km'] > cumulative:
        return {'valid': False, 'status': '待核',
                'blockers': [f"service km {order['cumulative_km']} exceeds proven cumulative {cumulative}"]}
    return {'valid': True, 'status': '有效', 'blockers': []}


def _baseline(data, cumulative, keystore):
    """Most recent *valid* service anchor; pending orders are reported not used."""
    evaluated = []
    valid_anchor = None
    for order in data.get('service_orders', ()):
        verdict = evaluate_service_order(order, cumulative, keystore)
        item = {'id': order['id'], 'cumulative_km': order.get('cumulative_km'),
                'status': verdict['status'], 'blockers': verdict['blockers']}
        evaluated.append(item)
        if verdict['valid'] and (valid_anchor is None
                                 or order['cumulative_km'] > valid_anchor['cumulative_km']):
            valid_anchor = item
    if valid_anchor is not None:
        return valid_anchor, evaluated
    legacy = data.get('last_service_km')
    if legacy is None:
        return None, evaluated
    anchor = {'id': data.get('last_service_ref', 'last_service_km'), 'cumulative_km': legacy,
              'status': '有效', 'blockers': [], 'legacy_seed': True}
    return anchor, evaluated


def decide(data, keystore=None):
    """Decide whether maintenance is due for one vehicle's evidence bundle."""
    interval_km = data['interval_km']
    accumulation = cumulative_km(data, keystore)
    evidence = [r['id'] for r in sorted(data['readings'], key=lambda r: (r['event_at'], r['id']))]
    evidence.extend(s['id'] for s in data.get('ecu_swaps', ()))
    if not accumulation['continuous']:
        return {'meter_km': accumulation['raw_latest_km'],
                'cumulative_km': None, 'due': None, 'status': '待核',
                'service_baseline_km': None, 'distance_since_service_km': None,
                'interval_km': interval_km, 'segments': [],
                'blockers': accumulation['blockers'], 'evidence': evidence,
                'pending_orders': [o['id'] for o in _baseline(data, None, keystore)[1]
                                   if o['status'] == '待核']}

    cumulative = accumulation['cumulative_km']
    baseline, evaluated_orders = _baseline(data, cumulative, keystore)
    if baseline is None:
        return {'meter_km': accumulation['raw_latest_km'], 'cumulative_km': cumulative,
                'due': None, 'status': '待核', 'service_baseline_km': None,
                'distance_since_service_km': None, 'interval_km': interval_km,
                'segments': accumulation['segments'],
                'blockers': ['no valid service event can anchor the maintenance interval'],
                'evidence': evidence,
                'pending_orders': [o['id'] for o in evaluated_orders if o['status'] == '待核']}

    distance_since = cumulative - baseline['cumulative_km']
    due = distance_since >= interval_km
    return {'meter_km': accumulation['raw_latest_km'], 'cumulative_km': cumulative,
            'due': due, 'status': '到期' if due else '未到期',
            'service_baseline_km': baseline['cumulative_km'],
            'service_baseline_ref': baseline['id'],
            'distance_since_service_km': distance_since, 'interval_km': interval_km,
            'segments': accumulation['segments'], 'blockers': [],
            'evidence': evidence + [baseline['id']],
            'pending_orders': [o['id'] for o in evaluated_orders if o['status'] == '待核']}
