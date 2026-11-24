"""Airport curb queue: evidence-captured reservation and actual pickup duty.

Covers the four hard constraints:

1. a vehicle/driver can never be double-occupied across carrying, empty return,
   holding and the next pickup; shift, permit and seating must cover the real
   service region and headcount;
2. curb readiness is landing + baggage + attested flight correction, no pickup
   is recorded before the passenger truly arrives, and a late flight can only
   create a new evidenced promise — it never silently moves an issued plate/time;
3. the latest pickup minute is binding; queue tickets, standby authorizations
   and requeue order are verifiable and one ticket can never buy priority twice;
4. no-show, plate change and driver pull cancel the affected binding with
   separate driver/passenger notices, and a completed trip can never re-enter
   the dispatchable pool because of a late flight update.
"""
import copy
import json
from pathlib import Path

import pytest

from taxiproof import rules
from taxiproof.curb_capture import DispatchError
from taxiproof.curb_dispatch import (
    assign, build_desk, choose_plan, enumerate_plans, replan,
)
from taxiproof.flight_arrival import arrival_evidence, clock, ready_minute

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'airport-queue.json'


def load():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def desk_with(data, **overrides):
    requests = overrides.pop('requests', data['requests'])
    cabs = overrides.pop('cabs', data['cabs'])
    standby = overrides.pop('standby', data['standby_authorizations'])
    return build_desk(copy.deepcopy(requests), copy.deepcopy(cabs),
                      copy.deepcopy(standby))


# ---------------------------------------------------------------------------
# 1. The seeded double-dispatch bug is gone
# ---------------------------------------------------------------------------

def test_seed_double_dispatch_is_impossible_under_hard_constraints():
    data = load()
    # P1 reaches the curb at 06:30, P2 at 06:45; CAB-1 needs 20 empty-return
    # minutes before it can legally take a second pickup.
    assert ready_minute(data['requests'][0]) == 390
    assert ready_minute(data['requests'][1]) == 405

    rows = assign(data['requests'], data['cabs'], data['standby_authorizations'])
    p1, p2 = rows
    assert p1['cab'] == 'CAB-1' and p1['pickup_minute'] == 390
    # The second order is never given the same cab at the overlapping 06:45.
    assert not (p2['cab'] == 'CAB-1' and p2['pickup_minute'] == 405)

    desk = desk_with(data)
    chosen = choose_plan(enumerate_plans(desk))
    replan_materialize(desk, chosen)
    for cab_id, cab in desk.cabs.items():
        intervals = sorted(cab['occupancy'])
        for earlier, later in zip(intervals, intervals[1:]):
            # half-open [start, end): next pickup cannot start before return
            assert later[0] >= earlier[1]
    # CAB-1's 20-minute return bound is actually consulted by plan A.
    plan_a = next(p for p in enumerate_plans(build_desk(data['requests'],
                                                        data['cabs'],
                                                        data['standby_authorizations']))
                  if not p.uses_standby)
    p2_a = plan_a.assignment_for('P2')
    assert p2_a.cab == 'CAB-1'
    assert p2_a.pickup_minute == 390 + data['cabs'][0]['return_to_curb_minutes'] == 410


def test_arrival_evidence_sums_landing_baggage_and_flight_correction():
    data = load()
    p1 = data['requests'][0]
    evidence = arrival_evidence(p1)
    assert evidence['ready_minute'] == 390
    assert [part['component'] for part in evidence['components']] == [
        'landing', 'baggage', 'flight_correction']
    assert sum(part['minutes'] for part in evidence['components']) == 390
    delayed = {**p1, 'flight_delay_minutes': 20}
    assert ready_minute(delayed) == 410
    versioned = arrival_evidence(delayed, {'id': 'FLIGHT-P1-V2',
                                           'delay_minutes': 20, 'source': 'AODB'})
    assert versioned['ready_minute'] == 410
    assert 'FLIGHT-P1-V2' in versioned['evidence']
    assert clock(410) == '06:50'


# ---------------------------------------------------------------------------
# 2. The same two-order/two-cab input has exactly the two compliant promises
# ---------------------------------------------------------------------------

def test_both_compliant_promises_are_enumerated():
    data = load()
    plans = enumerate_plans(build_desk(data['requests'], data['cabs'],
                                       data['standby_authorizations']))
    keys = {tuple(sorted((c.request, c.cab, c.pickup_minute)
                         for c in plan.commitments)) for plan in plans}
    # A: CAB-1 06:30 -> back 06:50 -> P2 at 06:50, reserve untouched
    # B: CAB-1 06:30 -> P1, CAB-2 06:45 -> P2 using the standby authorization
    assert (('P1', 'CAB-1', 390), ('P2', 'CAB-1', 410)) in keys
    assert (('P1', 'CAB-1', 390), ('P2', 'CAB-2', 405)) in keys
    assert len(plans) == 2

    plan_a = next(p for p in plans if not p.uses_standby)
    plan_b = next(p for p in plans if p.uses_standby)
    assert plan_a.assignment_for('P2').pickup_minute == 410
    assert plan_b.assignment_for('P2').pickup_minute == 405
    assert plan_a.total_wait_minutes > plan_b.total_wait_minutes
    assert plan_b.cabs_used == 2 and plan_a.cabs_used == 1
    # both promises still beat P2's 07:00 latest pickup
    for plan in plans:
        assert plan.assignment_for('P2').pickup_minute <= 420


def test_policy_chooses_reserve_by_default_and_min_wait_on_request():
    data = load()
    default_rows = assign(data['requests'], data['cabs'],
                          data['standby_authorizations'])
    assert {r['request']: (r['cab'], r['pickup_minute']) for r in default_rows} == {
        'P1': ('CAB-1', 390), 'P2': ('CAB-1', 410)}
    fast_rows = assign(data['requests'], data['cabs'],
                       data['standby_authorizations'], policy='minimize_wait')
    assert {r['request']: (r['cab'], r['pickup_minute']) for r in fast_rows} == {
        'P1': ('CAB-1', 390), 'P2': ('CAB-2', 405)}


def test_plan_a_and_plan_b_consume_different_tickets_and_notify_differently():
    data = load()

    desk_a = build_desk(data['requests'], data['cabs'],
                        data['standby_authorizations'])
    plan_a = next(p for p in enumerate_plans(desk_a) if not p.uses_standby)
    replan_materialize(desk_a, plan_a)
    p2_a = desk_a.commitments['P2']
    assert p2_a.ticket_id != 'CAB-1-T1'          # fresh requeue ticket
    assert p2_a.requeue_ticket_id == p2_a.ticket_id
    assert desk_a.standby[0].status == 'active'  # reserve preserved
    drivers_notified = {n.subject for n in desk_a.notices if n.audience == 'driver'}
    assert drivers_notified == {'D1'}

    desk_b = build_desk(data['requests'], data['cabs'],
                        data['standby_authorizations'])
    plan_b = next(p for p in enumerate_plans(desk_b) if p.uses_standby)
    replan_materialize(desk_b, plan_b)
    p2_b = desk_b.commitments['P2']
    assert p2_b.standby_id == 'STANDBY-CAB2'
    assert desk_b.standby[0].status == 'released'
    drivers_notified_b = {n.subject for n in desk_b.notices
                          if n.audience == 'driver' and n.type == 'dispatch'}
    assert drivers_notified_b == {'D1', 'D2'}


def replan_materialize(desk, plan):
    for planned in plan.commitments:
        desk.commit(planned.request, planned.cab, planned.pickup_minute,
                    release_standby=planned.standby_id is not None,
                    requeue_at=planned.requeue_at)


# ---------------------------------------------------------------------------
# 3. Flight delay: evidenced new promise, never a silent time shift; breach
# ---------------------------------------------------------------------------

def test_late_flight_breaches_latest_pickup_instead_of_silent_move():
    data = load()
    desk = desk_with(data)
    desk.commit('P1', 'CAB-1', 390)
    desk.mark_picked_up('P1', 390)
    desk.commit('P2', 'CAB-1', 410, requeue_at=410)
    original = desk.commitments['P2'].pickup_minute

    outcome = desk.add_flight_version(
        'P2', {'id': 'FLIGHT-P2-V2', 'delay_minutes': 20, 'source': 'AODB'})
    # 06:45 + 20 -> 07:05 which is past the 07:00 latest pickup
    assert outcome['outcome'] == 'breach'
    assert outcome['ready_minute'] == 425
    assert desk.commitments['P2'].status == 'breached'
    # the issued plate/time promise was not rewritten to 07:05
    assert outcome['superseded']['pickup_minute'] == original == 410
    assert desk.breaches[0]['reason'] == 'late_beyond_latest_pickup'
    audiences = {n.audience for n in desk.notices if n.type == 'breach'}
    assert audiences == {'driver', 'passenger'}


def test_late_flight_within_deadline_creates_new_evidenced_promise():
    data = load()
    desk = desk_with(data)
    desk.commit('P1', 'CAB-1', 390)
    desk.commit('P2', 'CAB-2', 405, release_standby=True)
    outcome = desk.add_flight_version(
        'P2', {'id': 'FLIGHT-P2-V2', 'delay_minutes': 10, 'source': 'AODB'})
    assert outcome['outcome'] == 'new_commitment_required'
    assert outcome['ready_minute'] == 415
    # original promise remains frozen at 06:45, marked superseded
    assert outcome['superseded']['pickup_minute'] == 405
    assert desk.commitments['P2'].status == 'superseded'
    # fresh evidenced commitment under the new version, on a fresh ticket
    desk.commit('P2', 'CAB-2', 415)
    current = desk.commitments['P2']
    assert current.pickup_minute == 415
    assert current.flight_version == 'FLIGHT-P2-V2'
    assert current.ticket_id != 'CAB-2-T1'


def test_pickup_cannot_be_recorded_before_passenger_reaches_curb():
    data = load()
    desk = desk_with(data)
    desk.commit('P1', 'CAB-1', 390)
    with pytest.raises(DispatchError) as exc:
        desk.mark_picked_up('P1', 389)
    assert exc.value.reason == 'passenger_not_at_curb'
    desk.mark_picked_up('P1', 390)
    assert desk.commitments['P1'].status == 'picked_up'


def test_completed_trip_is_immune_to_later_flight_updates():
    data = load()
    desk = desk_with(data)
    desk.commit('P1', 'CAB-1', 390)
    desk.mark_picked_up('P1', 390)
    protected = list(desk.cabs['CAB-1']['occupancy'])
    outcome = desk.add_flight_version(
        'P1', {'id': 'FLIGHT-P1-V9', 'delay_minutes': 40, 'source': 'AODB'})
    assert outcome['outcome'] == 'immutable_completed'
    assert desk.commitments['P1'].status == 'picked_up'
    assert desk.cabs['CAB-1']['occupancy'] == protected


# ---------------------------------------------------------------------------
# 4. Return delay: full recomputation, not a sort-key reshuffle
# ---------------------------------------------------------------------------

def test_return_delay_past_deadline_falls_back_to_standby_or_reports_unmet():
    data = load()
    # CAB-1 return pushed to 07:05, past P2's 07:00 deadline -> only the
    # standby CAB-2 at 06:45 keeps the promise set feasible.
    desk = desk_with(data)
    desk.commit('P1', 'CAB-1', 390)
    desk.mark_picked_up('P1', 390)
    desk.commit('P2', 'CAB-1', 410, requeue_at=410)
    affected = desk.report_return_delay('CAB-1', 425, 'CONGESTION-EVIDENCE')
    assert affected == ['P2']
    assert desk.commitments['P2'].status == 'cancelled'
    plan = replan(desk)
    assert [(c.request, c.cab, c.pickup_minute) for c in plan.commitments] == [
        ('P2', 'CAB-2', 405)]


def test_mild_return_delay_keeps_both_structural_options_open():
    data = load()
    desk = desk_with(data)
    desk.commit('P1', 'CAB-1', 390)
    desk.mark_picked_up('P1', 390)
    desk.commit('P2', 'CAB-1', 410, requeue_at=410)
    desk.report_return_delay('CAB-1', 415, 'CONGESTION-EVIDENCE')
    options = {tuple((c.cab, c.pickup_minute) for c in p.commitments)
               for p in enumerate_plans(desk)}
    assert (('CAB-1', 415),) in options      # wait out the delayed return
    assert (('CAB-2', 405),) in options      # release reserve instead


def test_no_cab_can_meet_deadline_is_reported_as_unmet():
    data = load()
    # shrink P2's window so neither a delayed CAB-1 nor CAB-2 can make it
    tight = copy.deepcopy(data)
    tight['requests'][1]['latest_pickup_minute'] = 400
    desk = desk_with(tight)
    desk.commit('P1', 'CAB-1', 390)
    desk.mark_picked_up('P1', 390)
    plans = enumerate_plans(desk)
    assert plans and plans[0].breaches[0]['request'] == 'P2'
    assert plans[0].breaches[0]['reason'] == 'no_feasible_assignment'
    rows = assign(tight['requests'], tight['cabs'], tight['standby_authorizations'])
    p2_row = next(row for row in rows if row['request'] == 'P2')
    assert p2_row['status'] == 'unmet'
    assert p2_row['cab'] is None and p2_row['pickup_minute'] is None
    p1_row = next(row for row in rows if row['request'] == 'P1')
    assert p1_row['cab'] == 'CAB-1' and p1_row['status'] == 'promised'


# ---------------------------------------------------------------------------
# 5. Driver pull, plate change and no-show cancel bindings with notices
# ---------------------------------------------------------------------------

def test_driver_pull_revokes_bindings_and_replans_with_separate_notices():
    data = load()
    desk = desk_with(data)
    desk.commit('P1', 'CAB-1', 390)
    desk.mark_picked_up('P1', 390)
    desk.commit('P2', 'CAB-2', 405, release_standby=True)
    affected = desk.pull_driver('CAB-2', 400, 'temporary withdrawal', 'PULL-1')
    assert affected == ['P2']
    assert desk.cabs['CAB-2']['status'] == 'pulled'
    assert desk.commitments['P2'].status == 'cancelled'
    plan = replan(desk)
    assert [(c.request, c.cab, c.pickup_minute) for c in plan.commitments] == [
        ('P2', 'CAB-1', 410)]
    driver_types = {n.type for n in desk.notices_for(cab_id='CAB-2')}
    passenger_types = {n.type for n in desk.notices_for(request_id='P2')}
    assert 'driver_pulled' in driver_types
    assert 'commitment_cancelled' in passenger_types


def test_plate_change_rebinds_legally_and_notifies_both_sides():
    data = load()
    desk = desk_with(data)
    desk.commit('P1', 'CAB-1', 390)
    desk.mark_picked_up('P1', 390)
    desk.mark_returned('CAB-1', 410)
    desk.commit('P2', 'CAB-2', 405, release_standby=True)
    new_commitment = desk.change_plate('P2', 'CAB-1', 410, 'PLATE-SWAP-1')
    assert new_commitment.cab == 'CAB-1'
    assert new_commitment.pickup_minute == 410
    assert desk.cabs['CAB-2']['occupancy'] == []  # unstarted binding released
    passenger_types = {n.type for n in desk.notices_for(request_id='P2')}
    assert {'commitment_cancelled', 'promise'} <= passenger_types


def test_no_show_releases_binding_and_requeues_on_new_ticket():
    data = load()
    desk = desk_with(data)
    desk.commit('P1', 'CAB-1', 390)
    with pytest.raises(DispatchError) as exc:
        desk.mark_no_show('P1', 405)      # not yet past 06:50 latest
    assert exc.value.reason == 'not_yet_overdue'
    ticket = desk.mark_no_show('P1', 415)
    assert ticket.id != 'CAB-1-T1'
    assert desk.ticket('CAB-1-T1').status == 'consumed'
    assert desk.commitments['P1'].cancel_reason == 'passenger_no_show'


# ---------------------------------------------------------------------------
# 6. Authority comes from rules.py, never from the fixture region whitelist
# ---------------------------------------------------------------------------

def test_region_permit_shift_and_seats_are_judged_via_rules_module():
    data = load()

    # The fixture deliberately gives CAB-2 an OUTER whitelist while its shift
    # assignment authorizes CITY: dispatch must follow the shift evidence.
    assert data['cabs'][1]['regions'] == ['OUTER']
    desk = desk_with(data)
    assignment = rules.driver_assignment(
        desk.cabs['CAB-2']['spec']['shift']['assignments'], 'D2', 405, 440)
    assert rules.assignment_authority(assignment, 'CITY', 405, 440) is True

    def cab2_eligible(cabs):
        d = build_desk(data['requests'], cabs, data['standby_authorizations'])
        from taxiproof.curb_capture import evaluate_cab
        return evaluate_cab(d.cabs['CAB-2'], d.requests['P2'], 405)[0]

    assert cab2_eligible(data['cabs']) is True

    no_region = copy.deepcopy(data)
    no_region['cabs'][1]['shift']['assignments'][0]['regions'] = ['OUTER']
    assert cab2_eligible(no_region['cabs']) is False

    expired_permit = copy.deepcopy(data)
    expired_permit['cabs'][1]['shift']['assignments'][0]['permit_until'] = 400
    d = build_desk(expired_permit['requests'], expired_permit['cabs'],
                   expired_permit['standby_authorizations'])
    from taxiproof.curb_capture import evaluate_cab
    assert evaluate_cab(d.cabs['CAB-2'], d.requests['P2'], 405)[0] is False

    over_seated = copy.deepcopy(data)
    over_seated['requests'][1]['passengers'] = 9
    d2 = build_desk(over_seated['requests'], over_seated['cabs'],
                    over_seated['standby_authorizations'])
    assert evaluate_cab(d2.cabs['CAB-2'], d2.requests['P2'], 405) == (False, 'capacity')


# ---------------------------------------------------------------------------
# 7. Tickets and standby authorizations are single-use and fully verifiable
# ---------------------------------------------------------------------------

def test_one_ticket_never_buys_two_dispatch_priorities():
    data = load()
    desk = desk_with(data)
    desk.commit('P1', 'CAB-1', 390)
    desk.mark_picked_up('P1', 390)
    desk.mark_returned('CAB-1', 410)
    desk.commit('P2', 'CAB-1', 410)
    ticket_ids = [t.id for t in desk.tickets]
    assert len(ticket_ids) == len(set(ticket_ids))
    consumed = [(t.id, t.consumed_by) for t in desk.tickets if t.status == 'consumed']
    assert ('CAB-1-T1', 'P1') in consumed
    assert all(cid == 'P1' for tid, cid in consumed if tid == 'CAB-1-T1')
    # requeue ticket is strictly later than the original
    original = desk.ticket('CAB-1-T1')
    requeue = desk.ticket(desk.commitments['P2'].ticket_id)
    assert requeue.issued_minute >= original.issued_minute
    assert requeue.id != original.id


def test_standby_authorization_is_single_use_and_time_bounded():
    data = load()
    desk = desk_with(data)
    desk.commit('P1', 'CAB-1', 390)
    desk.commit('P2', 'CAB-2', 405, release_standby=True)
    auth = desk.standby[0]
    assert auth.status == 'released' and auth.used_by == 'P2'
    # a second release attempt finds no active authorization
    with pytest.raises(DispatchError) as exc:
        desk.commit('P1', 'CAB-2', 405, release_standby=True)
    assert exc.value.reason in ('standby_not_authorized', 'overlapping_occupancy')
    # authorization outside its validity window cannot be used
    assert desk.standby_pool(460) == []
