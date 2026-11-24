import json
from pathlib import Path

from taxiproof.accessibility import ServiceLedger, evaluate, evaluate_all, served
from taxiproof.lift import deployments, physical_actions, provenance_graph
from taxiproof.lift_capture import ingest, sign

DATA = json.loads((Path(__file__).parents[1] / 'fixtures' / 'lift-test-vs-ride.json').read_text(encoding='utf-8'))
REGISTRY = DATA['registry']
BOOKING = DATA['booking']
BOOKING_2 = DATA['concurrent']['booking']
SHIFTS = DATA['shifts']
TRIPS = DATA['trips']


def signed(events):
    return [sign(e, REGISTRY) if e.get('source') else dict(e) for e in events]


def scenario(*names):
    events = list(DATA['events'])
    for name in names:
        events += DATA['investigations'][name]['events']
    return signed(events)


def test_unattributed_lift_event_is_not_passenger_service():
    events = signed(DATA['events'])
    assert DATA['events'][0]['purpose'] == 'unknown'
    assert 'passenger' not in DATA['events'][0]
    assert len(deployments(events, 'TAXI-1', REGISTRY)) == 1  # 车号只给候选，不构成证书
    assert served(BOOKING, events, SHIFTS, TRIPS, REGISTRY) is False
    result = evaluate(BOOKING, events, SHIFTS, TRIPS, REGISTRY)
    assert result['status'] == 'unproven'
    taxi1 = next(c for c in result['candidates'] if c['vehicle'] == 'TAXI-1')
    assert set(taxi1['missing']) == {'boarding', 'securement'}
    action = physical_actions(events, REGISTRY)[0]
    assert action['verified'] is False and action['origins'] == ['unknown']
    graph = provenance_graph(events, REGISTRY)
    assert {'device:BUS-7', 'vehicle:TAXI-1', 'unknown'} <= {n['id'] for n in graph['nodes']}


def test_vehicle_string_alone_is_not_a_service_certificate():
    flipped = [dict(e, vehicle='TAXI-2') for e in DATA['events']]
    assert deployments(flipped, 'TAXI-1') == []
    assert len(deployments(flipped, 'TAXI-2')) == 1
    assert served(BOOKING, flipped, SHIFTS, TRIPS, REGISTRY) is False
    assert evaluate(BOOKING, flipped, SHIFTS, TRIPS, REGISTRY)['status'] == 'unproven'


def test_investigation_confirms_taxi1_with_independent_witnesses():
    events = scenario('taxi1_onboard')
    result = evaluate(BOOKING, events, SHIFTS, TRIPS, REGISTRY)
    assert result['status'] == 'proven'
    assert result['proven_vehicle'] == 'TAXI-1'
    assert all(result['chains']['TAXI-1']['links'].values())
    assert served(BOOKING, events, SHIFTS, TRIPS, REGISTRY) is True


def test_investigation_redispatch_proves_taxi2_and_dedupes_proxy():
    events = scenario('taxi2_redispatch')
    bus7 = [a for a in physical_actions(events, REGISTRY)
            if a['device'] == 'BUS-7' and a['stage'] == 'deployed']
    assert len(bus7) == 1  # 代理转发的同一动作不得在两车各算一次
    action = bus7[0]
    assert {o['record'] for o in action['observations']} == {'L1', 'L1-BENCH', 'L1-FWD'}
    assert action['vehicles'] == ['TAXI-1', 'TAXI-2']
    assert action['self_check'] is True  # 维修台原件判定 L1 为未载人自检
    graph = provenance_graph(events, REGISTRY)
    assert {'device:BUS-7', 'bench:BENCH-9', 'gateway:TAXI-2'} <= {n['id'] for n in graph['nodes']}
    reported = [e for e in graph['edges'] if e['action'] == action['key'] and e['relation'] == 'reported_by']
    assert len(reported) == 3
    result = evaluate(BOOKING, events, SHIFTS, TRIPS, REGISTRY)
    assert result['status'] == 'proven'
    assert result['proven_vehicle'] == 'TAXI-2'
    assert served(BOOKING, events, SHIFTS, TRIPS, REGISTRY) is True
    kinds = {c['kind'] for c in result['conflicts']}
    assert {'double_claim', 'provenance_dispute', 'self_check'} <= kinds
    taxi1 = next(c for c in result['candidates'] if c['vehicle'] == 'TAXI-1')
    assert 'window' in taxi1['missing']


def test_self_check_proof_without_handover_evidence_is_not_service():
    events = scenario('taxi2_redispatch_insufficient')
    result = evaluate(BOOKING, events, SHIFTS, TRIPS, REGISTRY)
    assert result['status'] == 'unproven'
    assert result['proven_vehicle'] is None
    assert served(BOOKING, events, SHIFTS, TRIPS, REGISTRY) is False
    taxi2 = next(c for c in result['candidates'] if c['vehicle'] == 'TAXI-2')
    assert {'boarding', 'securement'} <= set(taxi2['missing'])


def test_late_redispatch_revises_only_that_booking():
    ledger = ServiceLedger()
    first = evaluate(BOOKING, scenario('taxi1_onboard'), SHIFTS, TRIPS, REGISTRY)
    assert first['proven_vehicle'] == 'TAXI-1'
    ledger.apply(first, note='初始认定 TAXI-1 履约')
    other = evaluate(BOOKING_2, signed(DATA['concurrent']['events']), SHIFTS, TRIPS, REGISTRY)
    ledger.apply(other, note='并发预约登记')
    late = evaluate(BOOKING, scenario('taxi1_onboard', 'taxi2_redispatch'), SHIFTS, TRIPS, REGISTRY)
    assert late['status'] == 'proven' and late['proven_vehicle'] == 'TAXI-2'
    assert any(c['kind'] == 'orphan_witnesses' and c['vehicle'] == 'TAXI-1'
               for c in late['conflicts'])
    ledger.apply(late, note='维修台原件证明 L1 为未载人自检，TAXI-2 转派履约',
                 revised_by=['L1-BENCH', 'W-DSP-2'])
    history = ledger.history('BOOK-1')
    assert len(history) == 2
    assert history[0]['vehicle'] == 'TAXI-1'
    assert history[0]['revoked'] is True
    assert '自检' in history[0]['revocation_reason']
    assert history[0]['revoked_by'] == ['L1-BENCH', 'W-DSP-2']
    assert ledger.current('BOOK-1')['vehicle'] == 'TAXI-2'
    other_history = ledger.history('BOOK-2')
    assert len(other_history) == 1 and other_history[0]['revoked'] is False


def test_concurrent_bookings_keep_exclusive_occupancy():
    events = signed(DATA['events'] + DATA['investigations']['taxi1_onboard']['events']
                    + DATA['concurrent']['events'])
    results = evaluate_all([BOOKING, BOOKING_2], events, SHIFTS, TRIPS, REGISTRY)
    first, second = results['BOOK-1'], results['BOOK-2']
    assert first['status'] == 'proven' and first['proven_vehicle'] == 'TAXI-1'
    assert second['status'] == 'conflict' and second['proven_vehicle'] is None
    assert any(c['kind'] == 'occupancy' and c['held_by'] == 'BOOK-1' for c in second['conflicts'])
    assert any(c['kind'] == 'occupancy' and c.get('challenged_by') == 'BOOK-2'
               for c in first['conflicts'])
    bus7_deploys = [a for a in physical_actions(events, REGISTRY)
                    if a['device'] == 'BUS-7' and a['stage'] == 'deployed']
    assert len(bus7_deploys) == 2  # 09:05 与 09:20 是两个不同的物理动作


def test_other_passenger_actions_do_not_close_the_chain():
    events = signed(DATA['events'] + DATA['concurrent']['events'])
    result = evaluate(BOOKING, events, SHIFTS, TRIPS, REGISTRY)
    assert result['status'] == 'unproven'
    assert served(BOOKING, events, SHIFTS, TRIPS, REGISTRY) is False


def test_tampered_signature_is_rejected_and_breaks_the_chain():
    events = scenario('taxi2_redispatch')
    forged = [dict(e, vehicle='TAXI-9') if e['id'] == 'W-SGN-2' else e for e in events]
    captured = ingest(forged, REGISTRY)
    assert any(r['record'] == 'W-SGN-2' and '签名' in r['reason'] for r in captured['rejected'])
    result = evaluate(BOOKING, forged, SHIFTS, TRIPS, REGISTRY)
    assert result['status'] == 'unproven'
    assert served(BOOKING, forged, SHIFTS, TRIPS, REGISTRY) is False
