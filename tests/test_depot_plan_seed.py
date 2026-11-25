import copy
import json
from pathlib import Path

import pytest

from taxiproof.depot_energy import required
from taxiproof.depot_plan import plan

ROOT = Path(__file__).resolve().parents[1]


def peak():
    return json.loads((ROOT / 'fixtures' / 'depot-peak.json').read_text())


def directive(*ids):
    auth = json.loads((ROOT / 'fixtures' / 'depot-auth.json').read_text())
    by_id = {d['id']: d for d in auth['directives']}
    return [copy.deepcopy(by_id[i]) for i in ids]


def dispatch_of(result, trip):
    return next(d for d in result['dispatch'] if d['trip'] == trip)


def test_required_prices_real_site_energy_and_gap():
    req = required(peak())
    cab4 = req['jobs']['CAB-4']
    assert cab4['kwh_battery'] == 20
    # 20 kWh to the battery costs 20/0.9 at the inlet plus session auxiliaries.
    assert cab4['kwh_site_min'] == pytest.approx(23.139, abs=1e-3)
    assert cab4['kwh_site_min'] > 20 / 0.9
    assert req['available_site_kwh'] == 30
    assert req['gap_kwh'] == pytest.approx(13.972, abs=1e-3)
    # The naive nameplate reading that the seed encoded: 22+22=44 kW > 30 kW.
    assert sum(c['kw'] for c in peak()['chargers']) == 44


def test_baseline_overload_refused_with_minimal_conflict():
    result = plan(peak())
    assert result['ready'] is False
    assert result['unfulfilled'] == ['T-92']
    # No silent overload: every interval stays inside the 30 kW contract.
    assert all(p['kw'] <= p['cap_kw'] + 1e-6 for p in result['load_curve'])
    assert max(p['kw'] for p in result['load_curve']) <= 30
    # Minimal proof: each order alone is feasible, together they are not.
    assert len(result['conflicts']) == 1
    conflict = result['conflicts'][0]
    assert set(conflict['jobs']) == {'T-91', 'T-92'}
    assert conflict['reason'] == 'energy'
    assert conflict['gap_kwh'] == pytest.approx(13.972, abs=1e-3)
    # The conflict gap is exactly the standing gap required() reports.
    assert conflict['gap_kwh'] == pytest.approx(result['required']['gap_kwh'])
    # The medical trip is still honoured with a verifiable reserve margin.
    t91 = dispatch_of(result, 'T-91')
    assert t91['ok'] and t91['soc_at_departure'] >= t91['min_soc']
    assert result['margins']['CAB-4']['reserve_margin_kwh'] >= 0


def test_capacity_grant_makes_both_trips():
    result = plan(peak(), directive('AUTH-48'))
    assert result['ready'] is True
    assert result['unfulfilled'] == []
    assert dispatch_of(result, 'T-91')['vehicle'] == 'CAB-4'
    assert dispatch_of(result, 'T-92')['vehicle'] == 'CAB-9'
    # Temporary 48 kW applies only inside the authorized window.
    curve = result['load_curve']
    assert {p['cap_kw'] for p in curve if p['start'] < '08:00'} == {48}
    assert {p['cap_kw'] for p in curve if p['start'] >= '08:00'} == {30}
    assert all(p['kw'] <= p['cap_kw'] + 1e-6 for p in curve)
    assert max(p['kw'] for p in curve) <= 48
    # Both vehicles leave with a verifiable SOC and emergency reserve.
    assert result['margins']['CAB-4']['margin_kwh'] == pytest.approx(2.0)
    assert result['margins']['CAB-9']['margin_kwh'] == pytest.approx(3.0)
    assert all(m['reserve_margin_kwh'] >= 0 for m in result['margins'].values())
    assert [a['id'] for a in result['approvals']] == ['AUTH-48']
    assert result['occupancy_ok'] is True


def test_retroactive_grant_cannot_legalize_past_overload():
    result = plan(peak(), directive('AUTH-48-LATE'))
    curve = result['load_curve']
    # Approved at 07:30: intervals before that stay on the 30 kW contract.
    assert [p['cap_kw'] for p in curve if p['start'] < '07:30'] == [30] * 6
    assert [p['cap_kw'] for p in curve if '07:30' <= p['start'] < '08:00'] == [48] * 6
    assert all(p['kw'] <= p['cap_kw'] + 1e-6 for p in curve)
    # Half a window of 48 kW is not enough: T-92 is honestly unfulfilled.
    assert result['ready'] is False
    assert result['unfulfilled'] == ['T-92']


def test_substitution_reassigns_t92_and_defers_cab9():
    result = plan(peak(), directive('SUB-T92'))
    assert result['ready'] is True
    t92 = dispatch_of(result, 'T-92')
    assert t92['vehicle'] == 'CAB-10' and t92['substitute'] is True
    assert t92['authorized_by'] == 'SUB-T92'
    # CAB-4 is prioritized inside the contract; CAB-9 charges after the peak.
    cab4 = next(s for s in result['slots'] if s['vehicle'] == 'CAB-4')
    assert (cab4['charger'], cab4['start'], cab4['kwh']) == ('C1', '07:00', 20.0)
    cab9 = next(s for s in result['slots'] if s['vehicle'] == 'CAB-9')
    assert cab9['start'] >= '08:00' and cab9['kwh'] == 18.0
    assert result['margins']['CAB-9']['deferred'] is True
    assert max(p['kw'] for p in result['load_curve']) <= 30
    # Deferring to the off-peak tariff is cheaper than the 48 kW grant plan.
    granted = plan(peak(), directive('AUTH-48'))
    assert result['cost'] < granted['cost']
    assert result['occupancy_ok'] is True


def test_late_substitution_is_rejected():
    result = plan(peak(), directive('SUB-T92-LATE'))
    assert result['ready'] is False
    assert result['unfulfilled'] == ['T-92']
    assert {'id': 'SUB-T92-LATE', 'reason': 'commit_window'} in result['rejected_directives']


def test_grant_preferred_over_substitute_when_both_authorized():
    result = plan(peak(), directive('AUTH-48', 'SUB-T92'))
    assert result['ready'] is True
    assert dispatch_of(result, 'T-92')['vehicle'] == 'CAB-9'
    assert [a['id'] for a in result['approvals']] == ['AUTH-48']


def test_efficiency_taper_soc_cap_and_connector_limit_delivery():
    granted = plan(peak(), directive('AUTH-48'))
    cab4 = next(s for s in granted['slots'] if s['vehicle'] == 'CAB-4')
    # Charging losses plus auxiliaries: site energy exceeds battery energy.
    assert cab4['kwh_site'] > cab4['kwh'] / 0.9
    assert cab4['kwh_site'] == pytest.approx(23.139, abs=1e-3)

    tapered = peak()
    tapered['vehicles']['CAB-9']['acceptance'] = [
        {'until_soc': 0.5, 'kw': 22}, {'until_soc': 1.0, 'kw': 6}]
    req = required(tapered)
    # Acceptance taper: 115 minutes best case, not the 49 the nameplate implies.
    assert req['jobs']['CAB-9']['min_minutes'] == 115
    result = plan(tapered, directive('AUTH-48'))
    assert result['ready'] is False
    assert result['unfulfilled'] == ['T-92']
    # A 22 kW gun does not put 22 kWh into the car in one hour.
    assert result['margins']['CAB-9']['kwh_delivered'] < 22

    capped = peak()
    capped['jobs'][1]['required_kwh'] = 50
    req = required(capped)
    assert 'soc_cap' in req['jobs']['CAB-9']['violations']
    result = plan(capped, directive('AUTH-48'))
    assert result['unfulfilled'] == ['T-92']
    assert result['reasons']['T-92'].startswith('vehicle:soc_cap')
    assert not [s for s in result['slots'] if s['vehicle'] == 'CAB-9']

    mismatched = peak()
    mismatched['vehicles']['CAB-9']['connector'] = 'CHADEMO'
    mismatched['jobs'][1]['connector'] = 'CHADEMO'
    result = plan(mismatched, directive('AUTH-48'))
    assert result['unfulfilled'] == ['T-92']
    assert result['reasons']['T-92'] == 'vehicle:connector'


def test_charger_fault_triggers_replan_with_cooldown():
    result = plan(peak(), directive('AUTH-48', 'FAULT-C1', 'SUB-T92'))
    history = [(r['trigger'], r['ready']) for r in result['revisions']]
    assert history == [('initial', False), ('AUTH-48', True),
                       ('FAULT-C1', False), ('SUB-T92', True)]
    faulted = result['revisions'][2]
    assert faulted['unfulfilled'] == ['T-92']
    # C1 is exclusive during its 07:20-07:50 fault cooldown.
    for slot in faulted['slots']:
        if slot['charger'] == 'C1':
            assert slot['end'] <= '07:20' or slot['start'] >= '07:50'
    # The medical job preempts C2 after the disconnect turnaround.
    cab4 = [s for s in faulted['slots'] if s['vehicle'] == 'CAB-4']
    assert [(s['charger'], s['start']) for s in cab4] == [('C1', '07:00'), ('C2', '07:25')]
    assert dispatch_of(faulted, 'T-91')['ok'] is True
    # Minimal-disturbance replan: the substitute only touches T-92/CAB-9.
    recovered = result['revisions'][3]
    assert [s for s in recovered['slots'] if s['vehicle'] == 'CAB-4'] == cab4
    assert dispatch_of(recovered, 'T-92')['vehicle'] == 'CAB-10'
    cab9 = next(s for s in recovered['slots'] if s['vehicle'] == 'CAB-9')
    assert cab9['start'] >= '08:00'
    assert all(r['occupancy_ok'] for r in result['revisions'])


def test_revision_readback_cancel_and_new_commitment():
    result = plan(peak(), directive('AUTH-48', 'CANCEL-AUTH-48', 'COMMIT-T93'))
    assert [h['trigger'] for h in result['history']] == [
        'initial', 'AUTH-48', 'CANCEL-AUTH-48', 'COMMIT-T93']
    # Exactly one revision is active; superseded ones stay readable.
    assert sum(1 for r in result['revisions'] if r['active']) == 1
    granted = result['revisions'][1]
    assert granted['ready'] is True and len(granted['slots']) == 2
    cancelled = result['revisions'][2]
    assert cancelled['ready'] is False and cancelled['directives'] == []
    # The new commitment is read back and honestly refused: 25 kWh cannot
    # reach CAB-11 inside the window even alone (time), and T-91+T-92 still
    # exceed the contract energy (energy).
    final = result['revisions'][3]
    assert final['active'] and final['ready'] is False
    assert final['unfulfilled'] == ['T-92', 'T-93']
    reasons = {tuple(c['jobs']): c['reason'] for c in final['conflicts']}
    assert reasons[('T-93',)] == 'time'
    assert reasons[('T-91', 'T-92')] == 'energy'
    assert dispatch_of(final, 'T-91')['ok'] is True


def test_single_active_revision_and_gun_exclusivity():
    result = plan(peak(), directive('AUTH-48', 'FAULT-C1', 'SUB-T92'))
    assert [r['active'] for r in result['revisions']].count(True) == 1
    for revision in result['revisions']:
        assert revision['occupancy_ok'] is True
        by_charger = {}
        for slot in revision['slots']:
            by_charger.setdefault(slot['charger'], []).append(slot)
        for runs in by_charger.values():
            runs.sort(key=lambda s: s['start'])
            for earlier, later in zip(runs, runs[1:]):
                assert later['start'] >= earlier['end']
