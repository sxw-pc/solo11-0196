"""Duty-relay evidence tests.

Five acceptance themes from the dispatch desk:

1. ``固定误禁派`` -- the seeded naive sum still pins the false 600'/False ban,
   while evidence merge to 300' flips the *same* threshold rule to allowed.
2. Two authorised forensic paths over the identical two 300' originals yield
   300'/可派/剩余180 and 600'/禁派 respectively.
3. Shared logins preserve feasible physical-driver assignments and conflict
   witnesses.
4. A late calibration bumps only D1's matching shift qualification revision
   and revokes the old dispatch notice (notification history retained);
   other drivers are byte-for-byte untouched.
5. Capture mechanics: hash de-dup, same-id/different-body transaction
   rollback, UTC conversion and SQLite persistence.
"""
import copy
import json
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

import pytest

from taxiproof import rules
from taxiproof.duty import (
    AmbiguousDuty,
    DutyInvestigation,
    bounds,
    identity_assignments,
    merge_minutes,
    minutes,
    reconstruct,
)
from taxiproof.duty_capture import DutyCapture, to_utc, utc_iso
from taxiproof.fatigue import (
    DispatchLedger,
    adjudicate,
    apply_late_calibration,
    dispatchable,
)

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / 'fixtures' / 'duty-relay.json'


@pytest.fixture()
def dossier():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


@pytest.fixture()
def logs(dossier):
    return copy.deepcopy(dossier['logs'])


# --------------------------------------------------------------------------- #
# 1. Pinned false ban + evidence merge flip                                   #
# --------------------------------------------------------------------------- #
def test_seed_naive_sum_pins_false_ban(dossier):
    d = dossier
    assert minutes(d['logs'], d['driver']) == 600
    assert dispatchable(d['logs'], d['driver'], d['maximum_minutes']) is False
    assert d['logs'][0]['start'] < d['logs'][1]['end']


def test_merged_upstream_minutes_300_flips_dispatchable_to_true():
    # "若上游分钟数经证据归并为 300，fatigue.dispatchable 会变 True"
    merged_logs = [{'id': 'DUTY-MERGED-1', 'driver': 'D1', 'minutes': 300,
                    'start': '08:00', 'end': '13:00'}]
    assert minutes(merged_logs, 'D1') == 300
    assert dispatchable(merged_logs, 'D1', 480) is True


def test_direct_minute_and_boundary_thresholds():
    assert adjudicate(300).dispatchable is True
    assert adjudicate(300).remaining_minutes == 180
    assert adjudicate(480).dispatchable is True
    assert adjudicate(480).remaining_minutes == 0
    assert adjudicate(481).status == 'blocked'
    assert adjudicate((300, 600)).status == 'hold'
    assert adjudicate((400, 500)).status == 'hold'
    assert adjudicate((481, 600)).status == 'blocked'
    assert adjudicate((300, 480)).status == 'dispatchable'
    # Hold still means "no new dispatch now".
    hold = adjudicate((300, 600))
    assert hold.dispatchable is False and hold.can_dispatch is False
    assert hold.result == '暂停'


# --------------------------------------------------------------------------- #
# Unresolved original messages: bounds + order independence                   #
# --------------------------------------------------------------------------- #
def test_unresolved_messages_hold_between_300_and_600(logs):
    rec = reconstruct(logs, driver='D1')
    assert rec.ambiguous is True and rec.proven is False
    assert rec.bounds == (300, 600)
    assert bounds(logs, driver='D1') == (300, 600)
    assert merge_minutes(logs, driver='D1') == (300, 600)
    with pytest.raises(AmbiguousDuty):
        merge_minutes(logs, driver='D1', strict=True)
    verdict = adjudicate(rec)
    assert verdict.status == 'hold'
    assert verdict.dispatchable is False
    assert '480' in verdict.reason


def test_decision_does_not_depend_on_message_return_order(logs):
    forward = adjudicate(reconstruct(logs, driver='D1'))
    reversed_logs = list(reversed(copy.deepcopy(logs)))
    backward = adjudicate(reconstruct(reversed_logs, driver='D1'))
    assert forward.bounds == backward.bounds == (300, 600)
    assert forward.status == backward.status == 'hold'
    assert forward.result == backward.result


def test_order_chain_alone_proves_relay_without_id_equality():
    logs = [
        {'id': 'METER-1', 'driver': 'D1', 'minutes': 300, 'source': 'meter',
         'start': '08:00', 'end': '13:00'},
        {'id': 'PLATFORM-9', 'driver': 'D1', 'minutes': 300,
         'source': 'dispatch', 'start': '08:02', 'end': '13:02'},
    ]
    orders = [{'id': 'ORDER-7', 'kind': 'order',
               'meter_report': 'METER-1', 'platform_report': 'PLATFORM-9'}]
    rec = reconstruct(logs, evidence=orders)
    assert rec.minutes == 300 and len(rec.intervals) == 1
    assert any(r.kind == 'same' for r in rec.relations)


def test_raw_timestamp_union_is_302_but_does_not_resolve_identity():
    from taxiproof.duty import union_minutes
    # Naive same-zone union of the two overlapping originals is 302', which is
    # exactly why UTC conversion + raw union alone cannot decide the shift.
    assert union_minutes([
        {'start': '08:00', 'end': '13:00'},
        {'start': '08:02', 'end': '13:02'}]) == 302
    rec = reconstruct([
        {'id': 'METER-1', 'driver': 'D1', 'minutes': 300, 'source': 'meter',
         'start': '08:00', 'end': '13:00'},
        {'id': 'PLATFORM-9', 'driver': 'D1', 'minutes': 300,
         'source': 'dispatch', 'start': '08:02', 'end': '13:02'}])
    assert rec.bounds == (300, 600) and rec.ambiguous


def test_different_vehicles_force_distinct_even_with_similar_ids():
    logs = [
        {'id': 'R1', 'driver': 'D1', 'minutes': 120, 'start': '08:00',
         'end': '10:00', 'vehicle': 'TAXI-1'},
        {'id': 'R2', 'driver': 'D1', 'minutes': 120, 'start': '10:00',
         'end': '12:00', 'vehicle': 'TAXI-2'},
    ]
    rec = reconstruct(logs)
    assert any(r.kind == 'distinct' for r in rec.relations)
    assert rec.bounds[0] == 240 and rec.minutes == 240


def test_identity_does_not_infer_two_shifts_from_different_system_ids(logs):
    # Seed ids differ (METER-1 vs PLATFORM-9) but with no separation evidence
    # the merged/relay scenario must remain feasible.
    rec = reconstruct(logs)
    assert any(s.label == 'relay' for s in rec.scenarios)
    assert any(s.label == 'separate' for s in rec.scenarios)


# --------------------------------------------------------------------------- #
# 2a. Forensic path A: platform origin chain + continuous vehicle pulses      #
# --------------------------------------------------------------------------- #
def test_relay_path_proves_single_300_shift(dossier, logs):
    evidence = dossier['forensic_paths']['relay']['evidence']
    rec = reconstruct(logs, driver='D1', evidence=evidence,
                      vehicle=dossier['vehicle'])

    assert rec.status == 'proven_single_shift'
    assert rec.proven is True
    assert rec.minutes == 300 and rec.bounds == (300, 300)

    same = [r for r in rec.relations if r.kind == 'same']
    assert any({r.a, r.b} == {'METER-1', 'PLATFORM-9'} for r in same)

    assert len(rec.intervals) == 1
    interval = rec.intervals[0]
    assert (interval.start, interval.end) == ('08:00', '13:00')
    assert interval.minutes == 300
    # UTC conversion: +08:00 local -> 00:00-05:00 UTC.
    assert interval.start_utc == '2026-11-24T00:00:00Z'
    assert interval.end_utc == '2026-11-24T05:00:00Z'

    # Continuous vehicle pulses corroborate the relay reading.
    assert rec.pulse_summaries
    assert all(p.continuous is True for p in rec.pulse_summaries)
    assert any('PROV-CHAIN-9' in r.evidence for r in rec.relations)

    verdict = adjudicate(rec, maximum_minutes=dossier['maximum_minutes'])
    assert verdict.status == 'dispatchable'
    assert verdict.dispatchable is True
    assert verdict.remaining_minutes == 180
    assert verdict.max_next_shift_minutes == 180


# --------------------------------------------------------------------------- #
# 2b. Forensic path B: independent timing anchor + two signed segments        #
# --------------------------------------------------------------------------- #
def test_second_device_path_proves_600_minutes_and_blocks(dossier, logs):
    bundle = dossier['forensic_paths']['second_device']
    rec = reconstruct(logs, driver='D1', evidence=bundle['evidence'])

    assert rec.status == 'proven_two_shifts'
    assert rec.minutes == 600 and rec.bounds == (600, 600)

    distinct = [r for r in rec.relations if r.kind == 'distinct']
    assert any({r.a, r.b} == {'METER-1', 'PLATFORM-9'} for r in distinct)

    assert len(rec.intervals) == 2
    first, second = rec.intervals
    assert (first.start, first.end, first.minutes) == ('08:00', '13:00', 300)
    assert (second.start, second.end, second.minutes) == ('13:02', '18:02', 300)
    assert first.start_utc == '2026-11-24T00:00:00Z'
    assert second.start_utc == '2026-11-24T05:02:00Z'
    assert second.end_utc == '2026-11-24T10:02:00Z'

    # Both signed segments are physically acknowledged by D1.
    identity = rec.identity
    assert identity.assignments
    assert all(a.driver in (None, 'D1') for a in identity.assignments)
    segment_drivers = identity.assignments[0].drivers
    assert set(segment_drivers.values()) == {'D1'}

    verdict = adjudicate(rec)
    assert verdict.status == 'blocked'
    assert verdict.dispatchable is False
    assert verdict.remaining_minutes == 0


def test_identical_originals_support_two_different_authorized_decisions(dossier, logs):
    path_a = dossier['forensic_paths']['relay']['evidence']
    path_b = dossier['forensic_paths']['second_device']['evidence']

    verdict_a = adjudicate(reconstruct(logs, evidence=path_a, vehicle='TAXI-1'))
    verdict_b = adjudicate(reconstruct(logs, evidence=path_b))

    assert (verdict_a.worked_minutes, verdict_a.status,
            verdict_a.remaining_minutes) == (300, 'dispatchable', 180)
    assert (verdict_b.worked_minutes, verdict_b.status) == (600, 'blocked')

    # Same bytes for the two original messages in both worlds.
    again = json.loads(FIXTURE.read_text(encoding='utf-8'))['logs']
    assert again == logs


# --------------------------------------------------------------------------- #
# Global per-device offset / drift                                            #
# --------------------------------------------------------------------------- #
def test_one_device_epoch_offset_is_shared_by_all_its_records():
    from taxiproof.duty import build_epoch
    epoch = build_epoch({'id': 'CAL', 'device': 'D2', 'offset_minutes': 300})
    assert epoch.correct_wall('08:02').strftime('%H:%M') == '13:02'
    assert epoch.correct_wall('13:02').strftime('%H:%M') == '18:02'
    # Same epoch object, same correction function for every reading.
    assert epoch.correction_minutes == 300


def test_slow_minutes_semantics_and_drift_are_global():
    from taxiproof.duty import build_epoch
    slow = build_epoch({'device': 'D2', 'slow_minutes': 300})
    assert slow.correct_wall('08:00').strftime('%H:%M') == '13:00'

    drifting = build_epoch({
        'device': 'D9', 'device_at': '08:00',
        'authoritative_at': '2026-11-24T08:00:00+08:00',
        'drift_seconds_per_minute': 6,  # +0.1 minute per elapsed minute
    })
    # 300 device minutes later -> base anchor mapping + 30 minutes drift.
    corrected = drifting.to_utc('13:00', date='2026-11-24', timezone='+08:00')
    local = corrected.astimezone(dt_timezone(timedelta(hours=8)))
    assert local.strftime('%H:%M') == '13:30'


def test_global_offset_cannot_be_chosen_per_record():
    # Two D2 reports; one anchor must shift both by the same +300'.
    logs = [
        {'id': 'M1', 'driver': 'D1', 'minutes': 300, 'source': 'meter',
         'start': '08:00', 'end': '13:00'},
        {'id': 'D2-A', 'driver': 'D1', 'minutes': 120, 'source': 'd2',
         'start': '08:10', 'end': '10:10'},
        {'id': 'D2-B', 'driver': 'D1', 'minutes': 120, 'source': 'd2',
         'start': '10:20', 'end': '12:20'},
    ]
    anchors = [{'id': 'ANC', 'kind': 'calibration', 'device': 'd2',
                'offset_minutes': 300}]
    rec = reconstruct(logs, evidence=anchors)
    corrected = {iv['reports'][0]: (iv['start'], iv['end'])
                 for iv in rec.intervals}
    # Both D2 intervals are shifted wholesale into the afternoon.
    assert corrected['D2-A'] == ('13:10', '15:10')
    assert corrected['D2-B'] == ('15:20', '17:20')


# --------------------------------------------------------------------------- #
# 3. Shared login identity assignments + conflict witnesses                   #
# --------------------------------------------------------------------------- #
def test_shared_login_keeps_both_feasible_drivers(dossier):
    shared = dossier['shared_login']
    result = identity_assignments(
        logs=shared['logs'], logins=shared['logins'], driver='D1')
    drivers = [a.drivers['SHARED-R1'] for a in result.assignments]
    assert sorted(drivers) == ['D1', 'D2']
    assert result.shared_logins and result.shared_logins[0].drivers == ['D1', 'D2']
    assert result.witnesses and result.witnesses[0].kind == 'shared_login'


def test_conflicting_signatures_are_preserved_as_witnesses(dossier):
    shared = dossier['shared_login']
    result = identity_assignments(
        logs=shared['logs'], logins=shared['logins'],
        signatures=shared['conflicting_signatures'])
    # Both identity readings survive ...
    assert sorted(a.drivers['SHARED-R1'] for a in result.assignments) == ['D1', 'D2']
    assert all(a.contested for a in result.assignments)
    # ... and the contradiction is witnessed with its evidence ids.
    conflicts = result.conflicts
    assert len(conflicts) == 1
    witness = conflicts[0]
    assert witness.kind == 'conflicting_identity_claims'
    assert set(witness.drivers) == {'D1', 'D2'}
    assert set(witness.evidence) == {'ACK-SHARED-D1', 'ACK-SHARED-D2'}


def test_handover_locks_physical_identity_before_and_after():
    segments = [
        {'id': 'S1', 'driver': 'D1',
         'start': '2026-11-24T08:00:00+08:00',
         'end': '2026-11-24T12:00:00+08:00'},
        {'id': 'S2', 'driver': 'D1',
         'start': '2026-11-24T12:00:00+08:00',
         'end': '2026-11-24T18:00:00+08:00'},
    ]
    handovers = [{'id': 'H1', 'vehicle': 'TAXI-1', 'at': '2026-11-24T12:00:00+08:00',
                  'from': 'D1', 'to': 'D2'}]
    result = identity_assignments(segments=segments, handovers=handovers)
    assert len(result.assignments) == 1
    assert result.assignments[0].drivers == {'S1': 'D1', 'S2': 'D2'}


def test_rules_handover_state_records_real_driver_and_trip_evidence():
    # Same overlapping-assignment + valid handover shape as fixtures/shift-*.
    shift = {
        'id': 'SHIFT-H', 'kind': 'shift', 'case_id': 'TRIP-H', 'version': 1,
        'effective_at': '2026-11-24T08:00',
        'assignments': [
            {'driver': 'D1', 'vehicle': 'TAXI-9',
             'start': '2026-11-24T08:00', 'end': '2026-11-24T14:00',
             'license_until': '2026-12-31T00:00',
             'permit_until': '2026-12-31T00:00', 'regions': ['CENTRAL']},
            {'driver': 'D2', 'vehicle': 'TAXI-9',
             'start': '2026-11-24T12:00', 'end': '2026-11-24T18:00',
             'license_until': '2026-12-31T00:00',
             'permit_until': '2026-12-31T00:00', 'regions': ['CENTRAL']},
        ],
        'handover': {'from': 'D1', 'to': 'D2',
                     'at': '2026-11-24T12:00', 'receipt': 'signed'},
    }
    trip = {
        'id': 'TRIP-H-1', 'kind': 'trip', 'case_id': 'TRIP-H', 'version': 1,
        'effective_at': '2026-11-24T12:20',
        'start': '2026-11-24T12:10', 'end': '2026-11-24T13:00',
        'driver': 'D2', 'vehicle': 'TAXI-9', 'region': 'CENTRAL', 'paid': 30,
        'segments': [
            {'start': '2026-11-24T12:10', 'end': '2026-11-24T13:00',
             'distance_km': 12, 'stationary': False,
             'timestamp_trusted': True, 'wait_minutes': 0}],
    }
    decisions, states = rules.calculate([shift, trip])
    key = 'TRIP-H/同车班次重叠由谁承运'
    assert decisions[key]['result'] == '已交班'
    state = {entity: state_ for subsystem, entity, state_, version in states
             if subsystem == '司机车辆营运班次'}
    assert state['TRIP-H'] == '可派'


# --------------------------------------------------------------------------- #
# 4. Late calibration: D1 matching shift only + old notice revocation         #
# --------------------------------------------------------------------------- #
def _seed_ledger_with_other_driver(dossier):
    late = dossier['late_calibration']
    ledger = DispatchLedger(maximum_minutes=dossier['maximum_minutes'])
    ledger.issue(late['driver'], shift_id=late['shift_id'],
                 minutes=late['initial_minutes'], at='2026-11-24T13:15:00+08:00',
                 notice_id=late['initial_notice_id'], evidence=['PROV-CHAIN-9'])
    other = late['other_driver']
    ledger.issue(other['driver'], shift_id=other['shift_id'],
                 minutes=other['minutes'], at='2026-11-24T13:16:00+08:00',
                 notice_id=other['notice_id'])
    # A second D1 shift must also stay untouched by the targeted revision.
    ledger.issue('D1', shift_id='SH-D1-OTHER-DAY', minutes=200,
                 at='2026-11-23T13:00:00+08:00', notice_id='DN-D1-OTHER')
    return ledger


def test_late_calibration_revises_only_d1_matching_shift_and_notice(dossier):
    late = dossier['late_calibration']
    ledger = _seed_ledger_with_other_driver(dossier)
    before = ledger.snapshot()

    outcome = apply_late_calibration(
        ledger,
        logs=dossier['logs'],
        calibration=late['calibration'],
        evidence=late['evidence'],
        driver=late['driver'],
        shift_id=late['shift_id'],
        at=late['arrives_at'])

    assert outcome.status == 'blocked'
    assert outcome.minutes == 600
    assert outcome.revision == 2 and outcome.previous_revision == 1
    assert outcome.revoked == [late['initial_notice_id']]

    # Qualification version bump for exactly D1's matching shift.
    assert ledger.qualification_version('D1', late['shift_id']) == 2
    q = ledger.qualification('D1', late['shift_id'])
    assert [v['revision'] for v in q['versions']] == [1, 2]
    assert q['status'] == '禁派'

    # Old dispatch notice is revoked but remains in notification history.
    old = ledger.notifications[late['initial_notice_id']]
    assert old['status'] == 'revoked'
    assert old['revoked_at'] == late['arrives_at']
    timeline = ledger.notification_history(late['initial_notice_id'])
    assert [h['action'] for h in timeline] == ['issued', 'revoked']

    # The other driver's qualification/notice are untouched.
    other = late['other_driver']
    assert ledger.qualification_version(other['driver'], other['shift_id']) == 1
    other_notice = ledger.notifications[other['notice_id']]
    assert other_notice['status'] == 'issued' and other_notice['revision'] == 1
    assert ledger.qualifications[f"{other['driver']}::{other['shift_id']}"] == \
        before['qualifications'][f"{other['driver']}::{other['shift_id']}"]

    # Another D1 shift is likewise untouched.
    assert ledger.qualification_version('D1', 'SH-D1-OTHER-DAY') == 1
    assert ledger.notifications['DN-D1-OTHER']['status'] == 'issued'

    # No history entry for any driver other than the targeted D1 shift.
    assert {h['driver'] for h in ledger.history
            if h['shift_id'] == late['shift_id']} == {'D1'}


def test_revoked_dispatch_notice_survives_sqlite_roundtrip(dossier, tmp_path):
    late = dossier['late_calibration']
    db_path = tmp_path / 'duty.sqlite'
    book = DutyCapture(path=db_path)
    ledger = _seed_ledger_with_other_driver(dossier)
    apply_late_calibration(
        ledger, logs=dossier['logs'], calibration=late['calibration'],
        evidence=late['evidence'], driver='D1', shift_id=late['shift_id'],
        at=late['arrives_at'])
    book.save_ledger(ledger)
    book.db.commit()
    book.close()

    reopened = DutyCapture(path=db_path)
    restored = reopened.load_ledger()
    assert restored.qualification_version('D1', late['shift_id']) == 2
    assert restored.notifications[late['initial_notice_id']]['status'] == 'revoked'
    assert len(restored.notification_history(late['initial_notice_id'])) == 2
    assert restored.qualification_version(
        late['other_driver']['driver'], late['other_driver']['shift_id']) == 1
    reopened.close()


# --------------------------------------------------------------------------- #
# 5. Capture: hashing, idempotence, transaction collision, UTC                #
# --------------------------------------------------------------------------- #
def test_utc_conversion_uses_case_zone():
    assert to_utc('08:00', date='2026-11-24', timezone='+08:00') == datetime(
        2026, 11, 24, 0, 0, tzinfo=dt_timezone.utc)
    assert utc_iso('13:02', date='2026-11-24', timezone='+08:00') == \
        '2026-11-24T05:02:00Z'


def test_hash_dedup_idempotent_and_same_id_collision_rolls_back():
    book = DutyCapture()
    payload = {'id': 'E-1', 'kind': 'pulse', 'at': '2026-11-24T08:00+08:00'}
    first = book.ingest(payload)
    assert first['inserted'] == 1 and first['idempotent'] is False
    second = book.ingest(payload)
    assert second['idempotent'] is True and second['inserted'] == 0
    assert len(book.evidence()) == 1

    changed = {'id': 'E-1', 'kind': 'pulse', 'at': '2026-11-24T09:00+08:00'}
    with pytest.raises(ValueError):
        book.ingest(changed)
    # Transaction rolled back: neither the body nor extra rows leaked.
    rows = book.evidence()
    assert len(rows) == 1
    assert rows[0]['at'] == '2026-11-24T08:00+08:00'


def test_fixture_file_hash_replay_is_idempotent(tmp_path):
    book = DutyCapture(path=tmp_path / 'capture.sqlite')
    receipt = book.ingest_file(FIXTURE)
    # The two original messages plus every nested forensic evidence record.
    assert receipt['inserted'] >= 2
    replay = book.ingest_file(FIXTURE)
    assert replay['idempotent'] is True
    assert len(book.sources()) == 1
    log_ids = {row['id'] for row in book.evidence('log')}
    assert {'METER-1', 'PLATFORM-9'} <= log_ids
    book.close()

    reopened = DutyCapture(path=tmp_path / 'capture.sqlite')
    assert len(reopened.sources()) == 1
    seed_logs = [e for e in reopened.evidence('log')
                 if e['id'] in {'METER-1', 'PLATFORM-9'}]
    assert len(seed_logs) == 2
    reopened.close()


def test_full_dossier_fixture_is_valid_json_and_keeps_seed_values(dossier):
    assert dossier['driver'] == 'D1'
    assert dossier['maximum_minutes'] == 480
    assert [row['id'] for row in dossier['logs']] == ['METER-1', 'PLATFORM-9']
    assert all(row['minutes'] == 300 for row in dossier['logs'])
    assert set(dossier['forensic_paths']) == {'relay', 'second_device'}
