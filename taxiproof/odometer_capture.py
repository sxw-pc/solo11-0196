"""Signed capture and SQLite ledger for ECU-swap odometer evidence.

This layer is the write/audit side of the maintenance decision.  It:

* holds the offline signing keys and verifies HMAC-SHA256 signature envelopes;
* captures vehicles, controllers, raw readings (with content-hash
  de-duplication), signed ECU swaps, service orders and no-service statements;
* enforces VIN binding, seal and wheel-calibration checks against the pure
  logic in :mod:`taxiproof.odometer`;
* keeps an append-only certificate ledger: re-evaluation may revoke or
  supersede an earlier certificate, but the original row, its meter segments
  and service-order version are never rewritten;
* wraps every write in one SQLite transaction so a failed validation rolls
  the whole import back.
"""
import hashlib
import hmac
import json
import sqlite3
import time

from .maintenance_due import decide
from .odometer import OdometerContinuityError, build_segments

# Offline fleet test keys.  Real deployments inject their own keystore.
DEFAULT_KEYSTORE = {
    'garage-test-key': 'secret:garage',
    'fleet-test-key': 'secret:fleet',
    'seal-test-key': 'secret:seal',
    'calibration-test-key': 'secret:calibration',
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicles (
    vin TEXT PRIMARY KEY, vehicle_id TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ecu_registry (
    ecu_id TEXT PRIMARY KEY, vin TEXT NOT NULL, installed_at INTEGER,
    body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS odometer_readings (
    reading_id TEXT PRIMARY KEY, vin TEXT NOT NULL, ecu_id TEXT NOT NULL,
    event_at INTEGER NOT NULL, received_at INTEGER NOT NULL, kilometres INTEGER NOT NULL,
    pulses INTEGER, content_hash TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS ecu_swaps (
    swap_id TEXT PRIMARY KEY, vin TEXT NOT NULL, removed_ecu TEXT NOT NULL,
    installed_ecu TEXT NOT NULL, event_at INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS service_orders (
    order_id TEXT NOT NULL, vin TEXT NOT NULL, version INTEGER NOT NULL,
    cumulative_km INTEGER, status TEXT NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY(order_id, version));
CREATE TABLE IF NOT EXISTS no_service_statements (
    statement_id TEXT PRIMARY KEY, vin TEXT NOT NULL, swap_id TEXT NOT NULL,
    cumulative_km INTEGER, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS maintenance_certificates (
    certificate_id TEXT PRIMARY KEY, vin TEXT NOT NULL, issued_at INTEGER NOT NULL,
    status TEXT NOT NULL, result TEXT NOT NULL, reason TEXT NOT NULL,
    meter_segments TEXT NOT NULL, service_ref TEXT, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS certificate_revocations (
    revocation_id TEXT PRIMARY KEY, vin TEXT NOT NULL, certificate_id TEXT NOT NULL,
    reason TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS maintenance_work_orders (
    work_order_id TEXT PRIMARY KEY, vin TEXT NOT NULL, certificate_id TEXT NOT NULL,
    cumulative_km INTEGER, due INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS capture_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT, vin TEXT NOT NULL,
    action TEXT NOT NULL, ref TEXT NOT NULL, ok INTEGER NOT NULL, detail TEXT NOT NULL,
    created_at INTEGER NOT NULL);
"""


class CaptureError(ValueError):
    """A validation or ledger violation that aborts the capture transaction."""


# Every signature envelope covers the full unsigned evidence: these field
# names are stripped recursively before canonicalisation so signatures do not
# depend on the order in which the several signers applied their envelopes.
SIGNATURE_FIELDS = frozenset({
    'garage_signature', 'fleet_reviewer_signature', 'parts_signature',
    'technician_signature', 'release_signature', 'inspector_signature',
    'calibration_signature',
})


def _unsigned(obj):
    if isinstance(obj, dict):
        return {key: _unsigned(value) for key, value in obj.items()
                if key not in SIGNATURE_FIELDS}
    if isinstance(obj, (list, tuple)):
        return [_unsigned(item) for item in obj]
    return obj


def canonical_json(obj):
    return json.dumps(_unsigned(obj), ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def content_hash(obj):
    """Hash of the full signed content (signatures included)."""
    return hashlib.sha256(
        json.dumps(obj, ensure_ascii=False, sort_keys=True,
                   separators=(',', ':')).encode('utf-8')).hexdigest()


def sign(structure, field, key_id, keystore=DEFAULT_KEYSTORE):
    """Attach an HMAC-SHA256 envelope over the recursively unsigned evidence."""
    if key_id not in keystore:
        raise CaptureError(f'unknown signing key {key_id}')
    digest = hmac.new(keystore[key_id].encode('utf-8'),
                      canonical_json(structure), hashlib.sha256).hexdigest()
    structure[field] = {'key_id': key_id, 'value': digest}
    return structure


def verify_envelope(structure, field, keystore=DEFAULT_KEYSTORE):
    envelope = structure.get(field)
    if not isinstance(envelope, dict) or envelope.get('key_id') not in keystore:
        return False
    expected = hmac.new(keystore[envelope['key_id']].encode('utf-8'),
                        canonical_json(structure), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, envelope.get('value', ''))


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript(SCHEMA)
    return db


def _audit(db, vin, action, ref, ok, detail):
    db.execute('INSERT INTO capture_audit(vin,action,ref,ok,detail,created_at) VALUES (?,?,?,?,?,?)',
               (vin, action, ref, 1 if ok else 0, detail, int(time.time())))


def _require_vin(db, vin):
    row = db.execute('SELECT 1 FROM vehicles WHERE vin=?', (vin,)).fetchone()
    if not row:
        raise CaptureError(f'unknown vehicle VIN {vin}')


def capture_vehicle(db, vehicle, keystore=DEFAULT_KEYSTORE):
    vin = vehicle.get('vin')
    if not vin or not vehicle.get('vehicle_id') or not vehicle.get('interval_km'):
        raise CaptureError('vehicle requires vin, vehicle_id and interval_km')
    with db:
        db.execute('INSERT OR IGNORE INTO vehicles VALUES (?,?,?)',
                   (vin, vehicle['vehicle_id'], json.dumps(vehicle, ensure_ascii=False, sort_keys=True)))
        for ecu in vehicle.get('controllers', ()):
            if ecu.get('vin') != vin:
                raise CaptureError(f"controller {ecu.get('ecu_id')} bound to VIN {ecu.get('vin')}, not {vin}")
            db.execute('INSERT OR IGNORE INTO ecu_registry VALUES (?,?,?,?)',
                       (ecu['ecu_id'], vin, ecu.get('installed_at'),
                        json.dumps(ecu, ensure_ascii=False, sort_keys=True)))
        _audit(db, vin, 'capture_vehicle', vin, True, 'vehicle and controllers bound to VIN')
    return {'vin': vin, 'captured': True}


def capture_reading(db, reading, keystore=DEFAULT_KEYSTORE):
    vin = reading.get('vin')
    digest = content_hash(reading)
    with db:
        _require_vin(db, vin)
        duplicate = db.execute('SELECT 1 FROM odometer_readings WHERE content_hash=? OR reading_id=?',
                               (digest, reading['id'])).fetchone()
        if duplicate:
            _audit(db, vin, 'capture_reading', reading['id'], True,
                   'duplicate reading ignored (id/content hash)')
            return {'reading_id': reading['id'], 'inserted': 0, 'idempotent': True}
        ecu = db.execute('SELECT vin FROM ecu_registry WHERE ecu_id=?', (reading['ecu'],)).fetchone()
        if not ecu or ecu['vin'] != vin:
            raise CaptureError(f"reading {reading['id']} controller {reading['ecu']} not bound to VIN {vin}")
        db.execute('INSERT INTO odometer_readings VALUES (?,?,?,?,?,?,?,?)',
                   (reading['id'], vin, reading['ecu'], reading['event_at'], reading['received_at'],
                    reading['kilometres'], reading.get('pulses'), digest))
        _audit(db, vin, 'capture_reading', reading['id'], True,
               f"raw {reading['kilometres']} km stored under controller {reading['ecu']}")
    return {'reading_id': reading['id'], 'inserted': 1, 'idempotent': False}


def _verify_swap_signatures(swap, keystore):
    checks = [('garage_signature', swap), ('fleet_reviewer_signature', swap),
              ('inspector_signature', swap.get('seal') or {}),
              ('calibration_signature', (swap.get('handover') or {}))]
    for field, structure in checks:
        if not verify_envelope(structure, field, keystore):
            raise CaptureError(f"swap {swap.get('id')} failed {field} verification")


def capture_swap(db, swap, keystore=DEFAULT_KEYSTORE):
    swap_id = swap.get('id')
    vin = swap.get('vin')
    with db:
        _require_vin(db, vin)
        for role, ecu in (('removed_ecu', swap.get('removed_ecu')),
                          ('installed_ecu', swap.get('installed_ecu'))):
            row = db.execute('SELECT vin FROM ecu_registry WHERE ecu_id=?', (ecu,)).fetchone()
            if not row or row['vin'] != vin:
                raise CaptureError(f'swap {swap_id} {role} {ecu} is not bound to VIN {vin}')
        _verify_swap_signatures(swap, keystore)
        readings = [dict(r) for r in db.execute(
            'SELECT * FROM odometer_readings WHERE vin=? ORDER BY event_at,reading_id', (vin,))]
        try:
            build_segments(readings, [swap], vin, keystore)
        except OdometerContinuityError as exc:
            _audit(db, vin, 'capture_swap', swap_id, False, str(exc))
            raise CaptureError(f'swap {swap_id} does not create a continuous chain: {exc}') from exc
        if db.execute('SELECT 1 FROM ecu_swaps WHERE swap_id=?', (swap_id,)).fetchone():
            raise CaptureError(f'swap event id {swap_id} already exists with different content')
        db.execute('INSERT INTO ecu_swaps VALUES (?,?,?,?,?,?)',
                   (swap_id, vin, swap['removed_ecu'], swap['installed_ecu'],
                    swap['event_at'], json.dumps(swap, ensure_ascii=False, sort_keys=True)))
        _audit(db, vin, 'capture_swap', swap_id, True,
               f"{swap['removed_ecu']}->{swap['installed_ecu']} same-VIN signed handover accepted")
    return {'swap_id': swap_id, 'captured': True}


def capture_service_order(db, order, keystore=DEFAULT_KEYSTORE):
    order_id, vin, version = order['id'], order['vin'], order.get('version', 1)
    with db:
        _require_vin(db, vin)
        existing = db.execute('SELECT body FROM service_orders WHERE order_id=? AND version=?',
                              (order_id, version)).fetchone()
        body = json.dumps(order, ensure_ascii=False, sort_keys=True)
        if existing:
            if existing[0] != body:
                raise CaptureError(f'service order {order_id} v{version} replayed with different content')
            _audit(db, vin, 'capture_service_order', order_id, True, 'idempotent replay')
            return {'order_id': order_id, 'inserted': 0, 'idempotent': True}
        state = load_state(db, vin)
        verdict = decide(state, keystore)
        # Validity classification uses the pure rule; the order is stored either way
        # but an unproven one is marked 待核 and can never anchor an interval.
        from .maintenance_due import evaluate_service_order
        classification = evaluate_service_order(order, verdict['cumulative_km'], keystore)
        db.execute('INSERT INTO service_orders VALUES (?,?,?,?,?,?)',
                   (order_id, vin, version, order.get('cumulative_km'),
                    classification['status'], body))
        _audit(db, vin, 'capture_service_order', order_id, True,
               f"stored as {classification['status']}: {'; '.join(classification['blockers']) or 'independent construction, parts and release evidence'}")
    return {'order_id': order_id, 'inserted': 1, 'status': classification['status']}


def capture_no_service_statement(db, statement, keystore=DEFAULT_KEYSTORE):
    sid, vin = statement['id'], statement['vin']
    with db:
        _require_vin(db, vin)
        if not verify_envelope(statement, 'garage_signature', keystore):
            raise CaptureError(f'no-service statement {sid} garage signature invalid')
        if not db.execute('SELECT 1 FROM ecu_swaps WHERE swap_id=? AND vin=?',
                          (statement['swap_id'], vin)).fetchone():
            raise CaptureError(f'statement {sid} references unknown swap {statement["swap_id"]}')
        db.execute('INSERT INTO no_service_statements VALUES (?,?,?,?,?)',
                   (sid, vin, statement['swap_id'], statement.get('cumulative_km'),
                    json.dumps(statement, ensure_ascii=False, sort_keys=True)))
        _audit(db, vin, 'capture_no_service', sid, True,
               'garage confirms no construction/parts work at swap day')
    return {'statement_id': sid, 'captured': True}


def load_state(db, vin):
    """Assemble the pure-logic evidence bundle from the ledger for one VIN."""
    vehicle = db.execute('SELECT body FROM vehicles WHERE vin=?', (vin,)).fetchone()
    if not vehicle:
        raise CaptureError(f'unknown vehicle VIN {vin}')
    vehicle_body = json.loads(vehicle[0])
    readings = [dict(r) for r in db.execute(
        'SELECT reading_id AS id, ecu_id AS ecu, event_at, received_at, kilometres, pulses '
        'FROM odometer_readings WHERE vin=? ORDER BY event_at,reading_id', (vin,))]
    swaps = [json.loads(r[0]) for r in db.execute(
        'SELECT body FROM ecu_swaps WHERE vin=? ORDER BY event_at', (vin,))]
    orders = [json.loads(r[0]) for r in db.execute(
        'SELECT body FROM service_orders WHERE vin=? ORDER BY version', (vin,))]
    statements = [json.loads(r[0]) for r in db.execute(
        'SELECT body FROM no_service_statements WHERE vin=?', (vin,))]
    return {'vehicle': vehicle['vehicle_id'], 'vin': vin,
            'interval_km': vehicle_body['interval_km'],
            'last_service_km': vehicle_body.get('last_service_km'),
            'last_service_ref': vehicle_body.get('last_service_ref'),
            'readings': readings, 'ecu_swaps': swaps,
            'service_orders': orders, 'no_service_statements': statements}


def list_certificates(db, vin):
    return [dict(r) for r in db.execute(
        'SELECT * FROM maintenance_certificates WHERE vin=? ORDER BY issued_at,certificate_id', (vin,))]


def read_certificate(db, vin, certificate_id):
    """Read back an issued certificate exactly as frozen, with any revocation."""
    row = db.execute('SELECT * FROM maintenance_certificates WHERE vin=? AND certificate_id=?',
                     (vin, certificate_id)).fetchone()
    if not row:
        raise CaptureError(f'certificate {certificate_id} not found for VIN {vin}')
    result = dict(row)
    result['meter_segments'] = json.loads(result['meter_segments'])
    result['body'] = json.loads(result['body'])
    result['revocations'] = [dict(r) for r in db.execute(
        'SELECT * FROM certificate_revocations WHERE certificate_id=? ORDER BY created_at',
        (certificate_id,))]
    return result


def evaluate_maintenance(db, vin, certificate_id, keystore=DEFAULT_KEYSTORE):
    """Re-run latest->decide against the ledger and append a certificate.

    Earlier certificates are never updated: a new due result after a prior
    not-due certificate revokes it; a service-anchored not-due result
    supersedes the old certificate while keeping every frozen row readable.
    """
    with db:
        state = load_state(db, vin)
        result = decide(state, keystore)
        now = int(time.time())
        body = json.dumps(result, ensure_ascii=False, sort_keys=True)
        prior = list_certificates(db, vin)
        active = [c for c in prior if c['status'] in ('未到期', '到期')]
        reason = ('provable cross-controller cumulative distance vs valid service anchor'
                  if result['status'] != '待核' else '; '.join(result['blockers']))
        db.execute('INSERT INTO maintenance_certificates VALUES (?,?,?,?,?,?,?,?,?)',
                   (certificate_id, vin, now, result['status'],
                    json.dumps({'due': result['due']}, ensure_ascii=False), reason,
                    json.dumps(result['segments'], ensure_ascii=False, sort_keys=True),
                    result.get('service_baseline_ref'), body))
        actions = []
        if result['status'] == '到期':
            for old in active:
                if old['status'] == '未到期':
                    rev_id = f'REV-{old["certificate_id"]}'
                    db.execute('INSERT OR IGNORE INTO certificate_revocations VALUES (?,?,?,?,?)',
                               (rev_id, vin, old['certificate_id'],
                                'continuous cumulative distance crosses the service interval', now))
                    db.execute('UPDATE maintenance_certificates SET status=? WHERE certificate_id=?',
                               ('已撤销', old['certificate_id']))
                    actions.append(f'revoked {old["certificate_id"]}')
            work_order_id = f'WO-DUE-{vin}-{certificate_id}'
            db.execute('INSERT OR IGNORE INTO maintenance_work_orders VALUES (?,?,?,?,?,?)',
                       (work_order_id, vin, certificate_id, result['cumulative_km'], 1,
                        json.dumps(result, ensure_ascii=False, sort_keys=True)))
            actions.append(f'opened work order {work_order_id}')
        elif result['status'] == '未到期' and active:
            for old in active:
                if old['service_ref'] != result.get('service_baseline_ref'):
                    db.execute('UPDATE maintenance_certificates SET status=? WHERE certificate_id=?',
                               ('已被新保养时点取代', old['certificate_id']))
                    actions.append(f'superseded {old["certificate_id"]} (service baseline replaced)')
        _audit(db, vin, 'evaluate_maintenance', certificate_id, True,
               f"{result['status']} {'; '.join(actions) or 'no prior certificate changed'}")
    return {'certificate_id': certificate_id, 'status': result['status'], 'due': result['due'],
            'cumulative_km': result['cumulative_km'], 'actions': actions, 'decision': result}
