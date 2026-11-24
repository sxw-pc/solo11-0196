"""Traceable concurrent emergency dispatch — seed and hard-constraint tests.

Run with:
    python -B -m pytest -q -p no:cacheprovider
"""
import copy
import json
from pathlib import Path

import pytest

from taxiproof.safety_alert import (actionable, alert_state,
                                    candidate_points, location_radius_km,
                                    location_trust)
from taxiproof.safety_capture import (JOURNAL_ARRIVAL, JOURNAL_CANCEL,
                                      JOURNAL_DISPATCH, JOURNAL_HANDOVER,
                                      ReplanError, ResponseCapture)
from taxiproof.safety_response import (FIRST_RESPONSE_SLA_MINUTES, assign,
                                       can_serve, feasibility_evidence,
                                       plan_responses, qualification,
                                       worst_case_eta)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'safety-incident.json'
NOW = '2026-11-24T09:00'


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


@pytest.fixture
def incident():
    return load_fixture()


@pytest.fixture
def alerts(incident):
    return copy.deepcopy(incident['alerts'])


@pytest.fixture
def units(incident):
    return copy.deepcopy(incident['units'])


def route_map(plan):
    return {task['alert_id']: task['unit_id'] for task in plan['tasks']}


def task_for(plan, alert_id):
    return next(task for task in plan['tasks'] if task['alert_id'] == alert_id)


# --------------------------------------------------------------------------- #
# 1. Seed defect remains demonstrable through the legacy entry point
# --------------------------------------------------------------------------- #
class TestSeedDoubleDispatch:
    def test_legacy_pointwise_desk_double_books_u1_and_drops_p2(self, incident):
        # The measured baseline behaviour is pinned: the old logic hands U1 to
        # both alerts and reports only the single reported point.
        assert actionable(incident['alerts'][0])
        assert location_radius_km(incident['alerts'][0]) == 4
        assert location_trust(incident['alerts'][0]) == 'stale'
        assert candidate_points(incident['alerts'][0]) == ['P1', 'P2']
        assert assign(incident['alerts'], incident['units']) == [
            {'alert_id': 'A1', 'unit_id': 'U1', 'eta_minutes': 4},
            {'alert_id': 'A2', 'unit_id': 'U1', 'eta_minutes': 4},
        ]

    def test_joint_planner_never_double_books_and_keeps_p2(self, alerts, units):
        decision = plan_responses(alerts, units, NOW)
        assert decision['feasible'] is True
        for plan in decision['plans']:
            assigned = [task['unit_id'] for task in plan['tasks']]
            assert len(assigned) == len(set(assigned))
            a1 = task_for(plan, 'A1')
            # P2 must survive inside the committed response envelope.
            assert a1['candidate_points'] == ['P1', 'P2']
            assert a1['eta_by_candidate'] == {'P1': 9, 'P2': 8} or \
                a1['eta_by_candidate'] == {'P1': 11, 'P2': 12}
            # The promise is the worst-case hypothesis, not an average.
            assert a1['worst_case_eta_minutes'] == max(a1['eta_by_candidate'].values())
            assert all(task['within_sla'] for task in plan['tasks'])


# --------------------------------------------------------------------------- #
# 2. Both business routes are valid; neither is hard-coded as a priority table
# --------------------------------------------------------------------------- #
class TestTwoValidRoutes:
    def test_named_routes_with_worst_case_envelope(self, alerts, units):
        decision = plan_responses(alerts, units, NOW)
        routes = {tuple(sorted(route_map(plan).items())): plan
                  for plan in decision['plans']}

        route_u2 = routes[(('A1', 'U2'), ('A2', 'U1'))]
        assert task_for(route_u2, 'A1')['worst_case_eta_minutes'] == 9
        assert task_for(route_u2, 'A2')['worst_case_eta_minutes'] == 4
        assert route_u2['reserve_unit_ids'] == ['U3']

        route_u3 = routes[(('A1', 'U3'), ('A2', 'U1'))]
        assert task_for(route_u3, 'A1')['worst_case_eta_minutes'] == 12
        assert task_for(route_u3, 'A2')['worst_case_eta_minutes'] == 4
        assert route_u3['reserve_unit_ids'] == ['U2']

    def test_routes_are_derived_not_prioritised(self, alerts, units):
        # Change U2's P2 reachability: the U2 route must disappear and the
        # U3 route remain — evidence the search recomputes from constraints.
        units[1]['eta_minutes'] = {'P1': 9, 'P2': 16}
        decision = plan_responses(alerts, units, NOW)
        mappings = [route_map(plan) for plan in decision['plans']]
        assert all(mapping['A1'] != 'U2' for mapping in mappings)
        assert {'A1': 'U3', 'A2': 'U1'} in mappings

    def test_desk_may_commit_either_route_and_reserve_differs(self, alerts, units):
        capture = ResponseCapture(alerts, units, NOW)
        first = capture.dispatch(NOW, route_index=0)
        mapping_zero = {t['alert_id']: t['unit_id'] for t in first['tasks']}
        assert mapping_zero == {'A1': 'U2', 'A2': 'U1'}
        assert first['reserve_unit_ids'] == ['U3']

        capture_b = ResponseCapture(alerts, units, NOW)
        second = capture_b.dispatch(NOW, route_index=2)
        mapping_two = {t['alert_id']: t['unit_id'] for t in second['tasks']}
        assert mapping_two == {'A1': 'U3', 'A2': 'U1'}
        assert second['reserve_unit_ids'] == ['U2']


# --------------------------------------------------------------------------- #
# 3. Driver-only acknowledgement cannot close a passenger panic alert
# --------------------------------------------------------------------------- #
class TestDriverAckDoesNotClose:
    def test_actionable_stays_true_with_driver_ack(self, alerts):
        alerts[0]['signals']['driver_ack'] = True
        assert actionable(alerts[0]) is True
        assert alert_state(alerts[0]) == 'open'

    def test_dispatch_keeps_acknowledged_alert_in_joint_plan(self, alerts, units):
        alerts[0]['signals']['driver_ack'] = True
        decision = plan_responses(alerts, units, NOW)
        assert decision['feasible'] is True
        assert {task['alert_id'] for task in decision['plans'][0]['tasks']} == {'A1', 'A2'}

    def test_journal_records_ack_but_alert_remains_open(self, alerts, units):
        capture = ResponseCapture(alerts, units, NOW)
        capture.dispatch(NOW)
        still_actionable = capture.note_driver_ack('A1', '2026-11-24T09:02')
        assert still_actionable is True
        kinds = [entry['kind'] for entry in capture.history('A1')]
        assert 'driver_acknowledgement' in kinds
        assert 'trusted_revocation' not in kinds

    def test_trusted_human_callback_is_the_only_way_to_revoke(self, alerts, units):
        capture = ResponseCapture(alerts, units, NOW)
        capture.dispatch(NOW)
        state = capture.apply_trusted_callback(
            'A1', {'reviewer': 'human_callback', 'verdict': 'revoked',
                   'at': '2026-11-24T09:05'}, '2026-11-24T09:05')
        assert state == 'revoked'
        chain = capture.history('A1')
        assert any(e['kind'] == 'trusted_revocation' for e in chain)
        assert any(e['kind'] == JOURNAL_CANCEL for e in chain)
        # A2's dispatch is untouched by A1's revocation.
        assert capture.tasks['A2']['unit_id'] == 'U1'

    def test_untrusted_reviewer_cannot_revoke(self, alerts, units):
        alerts[0]['trusted_callbacks'] = [
            {'reviewer': 'driver_self_report', 'verdict': 'revoked',
             'at': '2026-11-24T09:03'}]
        assert actionable(alerts[0]) is True


# --------------------------------------------------------------------------- #
# 4. Late trusted P2 correction recomputes unarrived tasks with evidence
# --------------------------------------------------------------------------- #
class TestP2CorrectionReplan:
    def _committed_u2_route(self, alerts, units):
        capture = ResponseCapture(alerts, units, NOW)
        capture.dispatch(NOW, route_index=0)
        assert capture.tasks['A1']['unit_id'] == 'U2'
        return capture

    def test_correction_plus_eta_change_cancels_and_hands_over(self, alerts, units):
        capture = self._committed_u2_route(alerts, units)
        # Late trusted evidence: the taxi is conclusively at P2, and fresh
        # traffic data pushes U2's P2 ETA beyond the 15-minute window.
        result = capture.apply_location_correction(
            'A1', {'point': 'P2', 'candidate_points': ['P2'],
                   'reported_radius_km': 0, 'trust': 'trusted'},
            '2026-11-24T09:04')
        result = capture.apply_eta_update(
            'U2', {'P1': 9, 'P2': 17}, '2026-11-24T09:05')

        assert capture.tasks['A1']['unit_id'] == 'U3'
        assert capture.tasks['A1']['candidate_points'] == ['P2']
        assert capture.tasks['A1']['worst_case_eta_minutes'] == 12
        # A2 was never affected and keeps U1.
        assert capture.tasks['A2']['unit_id'] == 'U1'

        chain = capture.history('A1')
        cancels = [e for e in chain if e['kind'] == JOURNAL_CANCEL]
        handovers = [e for e in chain if e['kind'] == JOURNAL_HANDOVER]
        dispatches = [e for e in chain if e['kind'] == JOURNAL_DISPATCH]
        assert cancels and cancels[-1]['unit_id'] == 'U2'
        assert handovers and handovers[-1]['from_unit'] == 'U2'
        assert handovers[-1]['to_unit'] == 'U3'
        assert len(dispatches) == 2
        # Nothing is silently overwritten: the old dispatch stays in the log.
        assert dispatches[0]['unit_id'] == 'U2'
        assert dispatches[1]['unit_id'] == 'U3'
        assert result['revision'] > 1

    def test_arrived_task_is_frozen_through_replan(self, alerts, units):
        capture = self._committed_u2_route(alerts, units)
        capture.mark_arrived('A1', 'U2', '2026-11-24T09:06')
        chain_before = capture.evidence_chain('A1')
        assert any(entry['kind'] == JOURNAL_ARRIVAL for entry in chain_before)
        # A correction after arrival cannot reassign the on-scene responder.
        capture.apply_location_correction(
            'A1', {'point': 'P2', 'candidate_points': ['P2'], 'trust': 'trusted'},
            '2026-11-24T09:08')
        assert capture.tasks['A1']['unit_id'] == 'U2'
        assert capture.arrivals['A1']['mode'] == 'on_scene'
        assert all(e['kind'] != JOURNAL_CANCEL for e in capture.history('A1'))
        # The arrival evidence stays in the immutable chain.
        assert any(entry['kind'] == JOURNAL_ARRIVAL
                   for entry in capture.evidence_chain('A1'))

    def test_remote_contact_does_not_count_as_arrival(self, alerts, units):
        capture = self._committed_u2_route(alerts, units)
        record = capture.record_remote_contact(
            'A1', 'U2', '2026-11-24T09:03', note='voice call established')
        assert record['counts_as_arrival'] is False
        assert 'A1' not in capture.arrivals
        # A later correction can still replan A1 because nobody is on scene.
        capture.apply_eta_update('U2', {'P1': 9, 'P2': 17}, '2026-11-24T09:05')
        assert capture.tasks['A1']['unit_id'] == 'U3'

    def test_replan_that_cannot_meet_sla_escalates_with_evidence(self, alerts, units):
        capture = self._committed_u2_route(alerts, units)
        # Every responder now misses P2. Replan must not fabricate a plan.
        for unit in capture.units:
            unit['eta_minutes']['P2'] = 99
        with pytest.raises(ReplanError) as exc:
            capture.apply_location_correction(
                'A1', {'point': 'P2', 'candidate_points': ['P2'],
                       'trust': 'trusted'}, '2026-11-24T09:05')
        failure = exc.value.args[0]['result']
        assert failure['feasible'] is False
        unserved_ids = {row['alert_id'] for row in failure['unserved']}
        assert 'A1' in unserved_ids
        assert failure['escalation'][0]['channel'] == 'external_emergency_services'
        assert capture.journal[-1]['kind'] == 'escalation'


# --------------------------------------------------------------------------- #
# 5. Infeasible joint problem: named alerts + evidence, external escalation
# --------------------------------------------------------------------------- #
class TestNoSolutionEscalation:
    def test_third_alert_with_no_capacity_is_named_not_hidden(self, alerts, units):
        # A3 can only be served by U3 (authorization), while U3 is the sole
        # responder that still reaches A1's P2 hypothesis after U2's P2 ETA
        # degrades. The joint problem then has no solution: A1 and A3 both
        # need U3 exclusively. The planner must name A3 (and A1) with
        # evidence rather than emit a misleading single-point plan.
        alerts.append({
            'id': 'A3', 'trip_id': 'T3',
            'signals': {'panic_button': True, 'driver_ack': False},
            'gps': {'point': 'P1', 'reported_radius_km': 0,
                    'trust': 'trusted', 'candidate_points': ['P1']},
        })
        units[1]['eta_minutes'] = {'P1': 9, 'P2': 16}   # U2 loses A1 coverage
        units[0]['authorized_trips'] = ['T1', 'T2']     # U1 excluded from T3
        units[1]['authorized_trips'] = ['T1', 'T2']     # U2 excluded from T3
        units[2]['authorized_trips'] = ['T1', 'T3']     # U3 alone for A3/A1
        decision = plan_responses(alerts, units, NOW)
        assert decision['feasible'] is False
        unserved_ids = {row['alert_id'] for row in decision['unserved']}
        assert {'A1', 'A3'} <= unserved_ids
        escalation_channels = {item['alert_id']: item['channel']
                               for item in decision['escalation']}
        assert escalation_channels['A3'] == 'external_emergency_services'
        # No partial plan is offered for the alerts that happened to have options.
        assert all(channel == 'external_emergency_services'
                   for channel in escalation_channels.values())
        a3_item = next(item for item in decision['escalation']
                       if item['alert_id'] == 'A3')
        assert 'contention' in a3_item['reason'] or 'SLA' in a3_item['reason']
        # Evidence explains per responder why nobody could take A3.
        a3_evidence = next(row for row in decision['unserved']
                           if row['alert_id'] == 'A3')
        verdicts = {row['unit_id']: row['verdict']
                    for row in a3_evidence['units_considered']}
        assert verdicts['U1'] == 'not authorized for trip privacy scope'
        assert verdicts['U2'] == 'not authorized for trip privacy scope'

    def test_no_partial_pseudo_plan_is_emitted(self, alerts, units):
        for unit in units:
            unit['eta_minutes'] = {'P1': 99, 'P2': 99}
        decision = plan_responses(alerts, units, NOW)
        assert decision['feasible'] is False
        assert 'plans' not in decision
        assert len(decision['escalation']) == 2
        assert {item['alert_id'] for item in decision['escalation']} == {'A1', 'A2'}

    def test_capture_dispatch_escalates_instead_of_committing(self, alerts, units):
        for unit in units:
            unit['eta_minutes'] = {'P1': 99, 'P2': 99}
        capture = ResponseCapture(alerts, units, NOW)
        result = capture.dispatch(NOW)
        assert result['feasible'] is False
        assert capture.tasks == {}
        assert capture.public_status()['alerts'][0]['phase'] == \
            'escalated_external_emergency'


# --------------------------------------------------------------------------- #
# 6. Qualification: shift windows, permits and licences beat the on_duty flag
# --------------------------------------------------------------------------- #
class TestResponderQualification:
    def test_expired_shift_cannot_be_laundered_through_on_duty(self, alerts, units):
        units[0]['shift']['end'] = '2026-11-24T08:30'  # expired before NOW
        assert units[0]['on_duty'] is True
        qual = qualification(units[0], NOW)
        assert qual['eligible'] is False
        assert 'no active shift window at planning time' in qual['reasons']
        decision = plan_responses(alerts, units, NOW)
        for plan in decision['plans']:
            assert all(task['unit_id'] != 'U1' for task in plan['tasks'])

    def test_expired_permit_blocks_dispatch_even_on_duty(self, alerts, units):
        units[1]['shift']['permit_until'] = '2026-11-24T08:00'
        assert qualification(units[1], NOW)['eligible'] is False
        decision = plan_responses(alerts, units, NOW)
        for plan in decision['plans']:
            assert all(task['unit_id'] != 'U2' for task in plan['tasks'])

    def test_off_duty_unit_ignored_even_with_fresh_credentials(self, alerts, units):
        units[2]['on_duty'] = False
        assert qualification(units[2], NOW)['eligible'] is False
        assert not can_serve(units[2], alerts[0], NOW)

    def test_missing_first_aid_certification_blocks_first_aid_task(self, alerts, units):
        units[2]['first_aid_certified'] = False
        assert qualification(units[2], NOW)['eligible'] is False

    def test_qualification_uses_same_evidence_shape_as_shift_rules(self, units):
        # taxiproof.rules binds drivers via (start <= trip start, end >= trip
        # end) plus license_until/permit_until coverage. The responder check
        # mirrors that evidence instead of trusting the on_duty boolean.
        unit = units[0]
        assert qualification(unit, NOW)['eligible'] is True
        unit['shift']['start'] = '2026-11-24T09:30'  # starts after NOW
        assert qualification(unit, NOW)['eligible'] is False
        unit['shift']['start'] = '2026-11-24T08:00'
        unit['shift']['license_until'] = '2026-11-24T08:59'
        reasons = qualification(unit, NOW)['reasons']
        assert any('licence' in reason for reason in reasons)


# --------------------------------------------------------------------------- #
# 7. Privacy: location goes only to authorized on-shift responders
# --------------------------------------------------------------------------- #
class TestPrivacyViews:
    def test_unauthorized_unit_never_receives_trip_and_cannot_see_location(self, alerts, units):
        units[2]['authorized_trips'] = ['T2']  # U3 may only see A2's trip
        decision = plan_responses(alerts, units, NOW)
        for plan in decision['plans']:
            a1 = task_for(plan, 'A1')
            assert a1['unit_id'] != 'U3'
        evidence = feasibility_evidence(alerts[0], units, NOW)
        verdicts = {row['unit_id']: row['verdict']
                    for row in evidence['units_considered']}
        assert verdicts['U3'] == 'not authorized for trip privacy scope'

    def test_responder_view_is_scoped_to_authorization(self, alerts, units):
        units[1]['authorized_trips'] = ['T1']
        capture = ResponseCapture(alerts, units, NOW)
        capture.dispatch(NOW, route_index=0)
        u2_view = capture.responder_view('U2')
        assert {a['alert_id'] for a in u2_view['alerts']} == {'A1'}
        assert u2_view['alerts'][0]['candidate_points'] == ['P1', 'P2']
        u3_view = capture.responder_view('U3')
        assert {a['alert_id'] for a in u3_view['alerts']} == set()

    def test_public_status_hides_candidate_locations(self, alerts, units):
        capture = ResponseCapture(alerts, units, NOW)
        capture.dispatch(NOW)
        status = capture.public_status()
        serialized = json.dumps(status, ensure_ascii=False)
        assert 'P1' not in serialized
        assert 'P2' not in serialized
        for row in status['alerts']:
            assert row['phase'] == 'response_dispatched'
            assert row['response_window_minutes'] == FIRST_RESPONSE_SLA_MINUTES
            assert isinstance(row['review_version'], int)
            assert row['accepted'] is True

    def test_location_evidence_flows_only_into_authorized_dispatch(self, alerts, units):
        capture = ResponseCapture(alerts, units, NOW)
        result = capture.dispatch(NOW)
        dispatched = {t['alert_id']: t['unit_id'] for t in result['tasks']}
        for alert_id, unit_id in dispatched.items():
            unit = next(u for u in units if u['id'] == unit_id)
            alert = next(a for a in alerts if a['id'] == alert_id)
            assert alert['trip_id'] in unit['authorized_trips']


# --------------------------------------------------------------------------- #
# 8. Worst-case envelope mechanics and average-ETA rejection
# --------------------------------------------------------------------------- #
class TestWorstCaseEnvelope:
    def test_worst_case_takes_maximum_over_hypotheses(self, units, alerts):
        assert worst_case_eta(units[1], alerts[0]) == 9   # max(9, 8)
        assert worst_case_eta(units[0], alerts[0]) == 18  # U1 too slow at P2
        assert worst_case_eta(units[0], alerts[1]) == 4

    def test_u1_cannot_serve_a1_even_though_reported_point_is_four_minutes(self, alerts, units):
        # The reported point P1 is 4 minutes away for U1 but P2 is 18: the
        # average (11) would pass, the worst-case (18) correctly fails.
        assert not can_serve(units[0], alerts[0], NOW)
        assert can_serve(units[0], alerts[1], NOW)

    def test_missing_eta_for_a_hypothesis_is_infeasible(self, alerts, units):
        units[1]['eta_minutes'] = {'P1': 9}
        assert worst_case_eta(units[1], alerts[0]) is None
        assert not can_serve(units[1], alerts[0], NOW)


# --------------------------------------------------------------------------- #
# 9. Late third alert consumes reserve; decisions derive from current state
# --------------------------------------------------------------------------- #
class TestLateThirdAlertAndPhaseTransitions:
    def test_third_alert_uses_reserve_without_disturbing_dispatched_tasks(self, alerts, units):
        capture = ResponseCapture(alerts, units, NOW)
        capture.dispatch(NOW, route_index=0)  # U2->A1, U1->A2; U3 reserve
        capture.alerts.append({
            'id': 'A3', 'trip_id': 'T3',
            'signals': {'panic_button': True, 'driver_ack': False},
            'gps': {'point': 'P1', 'reported_radius_km': 0,
                    'trust': 'trusted', 'candidate_points': ['P1']},
        })
        for unit in capture.units:
            if unit['id'] == 'U3':
                unit['authorized_trips'] = ['T1', 'T2', 'T3']
        result = capture._replan('2026-11-24T09:03', trigger='new alert A3')
        assert capture.tasks['A1']['unit_id'] == 'U2'
        assert capture.tasks['A2']['unit_id'] == 'U1'
        assert capture.tasks['A3']['unit_id'] == 'U3'
        assert result['retained_assignments'] == {'A1': 'U2', 'A2': 'U1'}
        assert result['changes'] == [
            {'alert_id': 'A3', 'unit_id': 'U3', 'previous_unit_id': None}]

    def test_fourth_alert_with_no_reserve_left_is_escalated_not_force_fit(self, alerts, units):
        capture = ResponseCapture(alerts, units, NOW)
        capture.dispatch(NOW, route_index=0)
        for unit in capture.units:
            unit['authorized_trips'] = ['T1', 'T2', 'T3', 'T4']
        # A3 absorbs the only reserve unit U3.
        capture.alerts.append({
            'id': 'A3', 'trip_id': 'T3',
            'signals': {'panic_button': True, 'driver_ack': False},
            'gps': {'point': 'P1', 'reported_radius_km': 0,
                    'trust': 'trusted', 'candidate_points': ['P1']},
        })
        capture._replan('2026-11-24T09:03', trigger='new alert A3')
        assert capture.tasks['A3']['unit_id'] == 'U3'
        # A4 arrives with every responder locked: escalation, no fake plan.
        capture.alerts.append({
            'id': 'A4', 'trip_id': 'T4',
            'signals': {'panic_button': True, 'driver_ack': False},
            'gps': {'point': 'P1', 'reported_radius_km': 0,
                    'trust': 'trusted', 'candidate_points': ['P1']},
        })
        with pytest.raises(ReplanError) as exc:
            capture._replan('2026-11-24T09:04', trigger='new alert A4')
        failure = exc.value.args[0]['result']
        assert failure['feasible'] is False
        assert {item['alert_id'] for item in failure['escalation']} == {'A4'}
        # Existing non-interruptible tasks are not touched.
        assert {aid: task['unit_id'] for aid, task in capture.tasks.items()} == {
            'A1': 'U2', 'A2': 'U1', 'A3': 'U3'}

    def test_public_status_phases_track_lifecycle_and_never_leak_locations(self, alerts, units):
        capture = ResponseCapture(alerts, units, NOW)
        capture.dispatch(NOW)
        capture.mark_arrived('A2', 'U1', '2026-11-24T09:04')
        capture.apply_trusted_callback(
            'A1', {'reviewer': 'human_callback', 'verdict': 'revoked',
                   'at': '2026-11-24T09:05'}, '2026-11-24T09:05')
        phases = {row['alert_id']: row['phase']
                  for row in capture.public_status()['alerts']}
        assert phases == {'A1': 'closed_after_trusted_callback',
                          'A2': 'on_scene'}
        serialized = json.dumps(capture.public_status(), ensure_ascii=False)
        assert 'P1' not in serialized and 'P2' not in serialized

    def test_benign_evidence_update_keeps_assignments_and_logs_replan(self, alerts, units):
        capture = ResponseCapture(alerts, units, NOW)
        capture.dispatch(NOW, route_index=0)
        before = {aid: task['unit_id'] for aid, task in capture.tasks.items()}
        # A trusted fix that merely confirms the existing candidate set.
        result = capture.apply_location_correction(
            'A1', {'point': 'P1', 'candidate_points': ['P1', 'P2'],
                   'trust': 'trusted'}, '2026-11-24T09:03')
        assert result['changes'] == []
        assert {aid: task['unit_id'] for aid, task in capture.tasks.items()} == before
        assert result['retained_assignments'] == before
        assert any(entry['kind'] == 'replan' for entry in capture.history('A1'))
