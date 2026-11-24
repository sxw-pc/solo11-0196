"""Capture raw lift telemetry into verified, ordered, deduplicated actions.

Raw records are claims, not certificates.  This layer normalises them into
*physical actions* keyed by an explicit ``action`` field or by
``(device, stage, at)``, so one action forwarded by a proxy under a second
vehicle number is still a single action with several observations.  Records
may carry an HMAC signature from a registered physical endpoint (vehicle
gateway, maintenance bench, the lift device itself); unsigned legacy records
are kept but flagged unverified, while records with a bad or unknown
signature are rejected.

Even with parsing, device signatures, per-vehicle filtering, clock ordering
and SQLite storage all done, one action can still have two true origins (a
maintenance-bench self-check and a vehicle-gateway report of the same BUS-7
deployment) and nothing here proves a passenger boarded — that needs the
independent handover evidence evaluated in :mod:`taxiproof.accessibility`.
"""
import hashlib
import hmac
import json


def _canonical(record):
    body = {key: value for key, value in record.items() if key != 'signature'}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sign(record, registry):
    """Return a copy of *record* signed by its ``source`` endpoint."""
    source = record.get('source')
    if source not in registry:
        raise ValueError(f'unknown signing endpoint: {source!r}')
    signed = dict(record)
    signed['signature'] = hmac.new(
        str(registry[source]).encode('utf-8'),
        _canonical(record).encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    return signed


def verify(record, registry):
    """Check a record's signature against the registered endpoint key."""
    source = record.get('source')
    signature = record.get('signature')
    if not source or not signature or source not in registry:
        return False
    expected = hmac.new(
        str(registry[source]).encode('utf-8'),
        _canonical(record).encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def action_key(record):
    """Physical identity of one lift action; proxy copies share one key."""
    return record.get('action') or '{}|{}|{}'.format(record['device'], record['stage'], record['at'])


def _summarise(action):
    observations = action['observations']
    action['vehicles'] = sorted({o['vehicle'] for o in observations if o['vehicle']})
    action['origins'] = sorted({o['source'] or 'unknown' for o in observations})
    action['verified'] = any(o['verified'] for o in observations)
    verified_purposes = {o['purpose'] for o in observations if o['verified']}
    action['verified_purposes'] = sorted(verified_purposes)
    action['self_check'] = 'self_check' in verified_purposes and 'passenger_service' not in verified_purposes
    action['purpose_conflict'] = {'self_check', 'passenger_service'} <= verified_purposes
    action['passengers'] = sorted({o['passenger'] for o in observations if o['passenger']})
    action['ambiguous_origin'] = len(action['origins']) > 1
    return action


def ingest(records, registry=None):
    """Parse, verify, clock-order and deduplicate raw records.

    Returns a dict with ordered physical ``actions``, ``attestations``
    (non-device evidence such as camera notes, signoffs or dispatch orders)
    and ``rejected`` records that failed validation or signature checks.
    """
    actions = {}
    attestations = []
    rejected = []
    for record in records:
        if not isinstance(record, dict) or not record.get('id'):
            rejected.append({'record': record.get('id') if isinstance(record, dict) else None,
                             'reason': '记录缺少 id'})
            continue
        verified = False
        if registry is not None and record.get('source'):
            if not verify(record, registry):
                rejected.append({'record': record['id'], 'reason': '签名无效或来源未登记'})
                continue
            verified = True
        if 'stage' in record:
            missing = [field for field in ('device', 'at') if field not in record]
            if missing:
                rejected.append({'record': record['id'], 'reason': '升降板事件缺字段: ' + ','.join(missing)})
                continue
            key = action_key(record)
            action = actions.get(key)
            if action is None:
                action = {'key': key, 'device': record['device'], 'stage': record['stage'],
                          'at': record['at'], 'observations': []}
                actions[key] = action
            elif (action['device'], action['stage'], action['at']) != (record['device'], record['stage'], record['at']):
                rejected.append({'record': record['id'], 'reason': '动作标识与设备/阶段/时间不一致'})
                continue
            action['observations'].append({
                'record': record['id'], 'source': record.get('source'),
                'vehicle': record.get('vehicle'), 'verified': verified,
                'purpose': record.get('purpose', 'unknown'), 'passenger': record.get('passenger')})
        elif 'kind' in record:
            attestation = dict(record)
            attestation['verified'] = verified
            attestations.append(attestation)
        else:
            rejected.append({'record': record['id'], 'reason': '无法识别的记录类型'})
    ordered = [_summarise(action) for action in actions.values()]
    ordered.sort(key=lambda action: (action['at'], action['key']))
    attestations.sort(key=lambda item: (item.get('at', ''), item['id']))
    return {'actions': ordered, 'attestations': attestations, 'rejected': rejected}
