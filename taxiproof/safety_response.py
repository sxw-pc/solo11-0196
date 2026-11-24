"""Concurrent first-response dispatch over alerts, location hypotheses and
resource time windows.

The legacy desk handled one alert at a time against one reported GPS point and
could therefore hand the same responder to two simultaneous emergencies while
ignoring the alternative locations of a stale fix. This module instead plans
*jointly*:

* every still-open panic-button alert is planned in the same decision;
* each alert carries a set of plausible locations, and the committed response
  must reach the **worst-case** location inside the service-level window
  (15 minutes for a first on-scene response), never an average ETA;
* one responder can own at most one non-interruptible first-aid task in a time
  window — remote voice contact is tracked separately and never counts as an
  on-scene arrival;
* responder eligibility comes from shift windows plus licences/permits (an
  expired shift cannot be laundered through a bare ``on_duty`` flag), and
  passenger location is disclosed only to responders authorised for the trip.

When no joint plan meets every deadline the planner refuses to emit a
single-point pseudo-plan: it returns the alerts that cannot be served with
per-alert evidence so the desk escalates them to external emergency services.
"""
from .safety_alert import actionable, candidate_points

FIRST_RESPONSE_SLA_MINUTES = 15


# --------------------------------------------------------------------------- #
# Responder qualification and privacy
# --------------------------------------------------------------------------- #
def _shift_window(unit, now):
    """Return the (start, end) shift window covering ``now`` for a unit.

    ``None`` means the unit has no shift covering the planning instant, so a
    plain ``on_duty`` flag cannot extend an expired shift.
    """
    shift = unit.get('shift') or {}
    start = shift.get('start')
    end = shift.get('end')
    if start is not None and end is not None and start <= now < end:
        return start, end
    return None


def qualification(unit, now):
    """Explain why a responder is or is not dispatchable at ``now``.

    The check combines the on-duty flag, a shift window that actually covers
    the planning instant, and unexpired licence/permits — mirroring the
    evidence used for driver shifts in :mod:`taxiproof.rules` instead of
    trusting one boolean.
    """
    reasons = []
    eligible = True
    if not unit.get('on_duty'):
        eligible = False
        reasons.append('not on duty')
    if _shift_window(unit, now) is None:
        eligible = False
        reasons.append('no active shift window at planning time')
    shift = unit.get('shift') or {}
    license_until = shift.get('license_until')
    permit_until = shift.get('permit_until')
    if license_until is None or license_until < now:
        eligible = False
        reasons.append('responder licence expired or missing')
    if permit_until is None or permit_until < now:
        eligible = False
        reasons.append('response permit expired or missing')
    if not unit.get('first_aid_certified', False):
        eligible = False
        reasons.append('not certified for non-interruptible first aid')
    return {'unit_id': unit['id'], 'eligible': eligible, 'reasons': reasons}


def eligible_units(units, now):
    return [u for u in units if qualification(u, now)['eligible']]


def authorized_for(unit, alert):
    """Hard constraint three: location disclosure needs trip authorization.

    A responder may see passenger location / alert detail only when the unit's
    authorization scope covers the alert's trip. Units without an explicit
    grant are denied (default-deny), even if they are otherwise eligible.
    """
    grants = unit.get('authorized_trips', [])
    return alert.get('trip_id') in grants


# --------------------------------------------------------------------------- #
# Reachability under location uncertainty
# --------------------------------------------------------------------------- #
def eta_table(unit):
    return unit.get('eta_minutes', {}) or {}


def worst_case_eta(unit, alert):
    """Maximum ETA over every still-plausible location of ``alert``.

    Returns ``None`` when the responder cannot reach one of the candidate
    points (missing ETA). The maximum — not an average — is what the service
    promise is measured against.
    """
    etas = eta_table(unit)
    worst = None
    for point in candidate_points(alert):
        eta = etas.get(point)
        if eta is None:
            return None
        worst = eta if worst is None else max(worst, eta)
    return worst


def can_serve(unit, alert, now, sla_minutes=FIRST_RESPONSE_SLA_MINUTES):
    """Full per-pair feasibility: qualification, privacy and worst-case ETA."""
    if not qualification(unit, now)['eligible']:
        return False
    if not authorized_for(unit, alert):
        return False
    worst = worst_case_eta(unit, alert)
    return worst is not None and worst <= sla_minutes


def feasibility_evidence(alert, units, now, sla_minutes=FIRST_RESPONSE_SLA_MINUTES):
    """Per-alert audit row describing every responder considered."""
    rows = []
    for unit in units:
        qual = qualification(unit, now)
        etas = eta_table(unit)
        point_etas = {point: etas.get(point) for point in candidate_points(alert)}
        if not qual['eligible']:
            verdict = 'ineligible'
        elif not authorized_for(unit, alert):
            verdict = 'not authorized for trip privacy scope'
        elif any(eta is None for eta in point_etas.values()):
            verdict = 'cannot cover every candidate location'
        elif max(point_etas.values()) > sla_minutes:
            verdict = 'worst-case ETA exceeds SLA'
        else:
            verdict = 'feasible'
        rows.append({'unit_id': unit['id'], 'verdict': verdict,
                     'reasons': qual['reasons'], 'eta_by_candidate': point_etas,
                     'worst_case_eta': worst_case_eta(unit, alert)})
    feasible_ids = [row['unit_id'] for row in rows if row['verdict'] == 'feasible']
    return {'alert_id': alert['id'], 'candidate_points': candidate_points(alert),
            'sla_minutes': sla_minutes, 'feasible_unit_ids': feasible_ids,
            'units_considered': rows}


# --------------------------------------------------------------------------- #
# Joint planning across alerts, hypotheses and resource windows
# --------------------------------------------------------------------------- #
def _enumerate_plans(open_alerts, units, now, sla_minutes):
    """Backtracking search over alert->responder assignments.

    Alerts are handled most-constrained first. Each branch extends a partial
    plan only when (a) the responder can meet the worst-case SLA for that
    alert and (b) the responder has not already taken another
    non-interruptible task in this window. Every complete assignment
    returned therefore satisfies all hard constraints simultaneously.
    """
    options = []
    for alert in open_alerts:
        choices = []
        for unit in eligible_units(units, now):
            if not authorized_for(unit, alert):
                continue
            worst = worst_case_eta(unit, alert)
            if worst is not None and worst <= sla_minutes:
                choices.append((unit['id'], worst))
        choices.sort(key=lambda item: (item[1], item[0]))
        options.append((alert, choices))
    options.sort(key=lambda pair: (len(pair[1]), pair[0]['id']))

    plans = []

    def extend(index, taken, assignment):
        if index == len(options):
            plans.append(dict(assignment))
            return
        alert, choices = options[index]
        for unit_id, worst in choices:
            if unit_id in taken:
                continue
            assignment[alert['id']] = unit_id
            taken.add(unit_id)
            extend(index + 1, taken, assignment)
            taken.remove(unit_id)
            del assignment[alert['id']]

    extend(0, set(), {})
    return options, plans


def _build_plan(open_alerts, units, assignment, now, sla_minutes):
    units_by_id = {unit['id']: unit for unit in units}
    tasks = []
    for alert in open_alerts:
        unit_id = assignment[alert['id']]
        unit = units_by_id[unit_id]
        points = candidate_points(alert)
        etas = eta_table(unit)
        tasks.append({
            'alert_id': alert['id'],
            'trip_id': alert.get('trip_id'),
            'unit_id': unit_id,
            'candidate_points': list(points),
            'eta_by_candidate': {point: etas[point] for point in points},
            'worst_case_eta_minutes': worst_case_eta(unit, alert),
            'sla_minutes': sla_minutes,
            'within_sla': worst_case_eta(unit, alert) <= sla_minutes,
            'mode': 'on_scene',
            'interruptible': False,
        })
    reserve = sorted(
        (unit['id'] for unit in eligible_units(units, now)
         if unit['id'] not in set(assignment.values())),
    )
    return {
        'feasible': True,
        'planned_at': now,
        'sla_minutes': sla_minutes,
        'tasks': sorted(tasks, key=lambda task: task['alert_id']),
        'reserve_unit_ids': reserve,
        'alert_count': len(open_alerts),
        'evidence': [feasibility_evidence(alert, units, now, sla_minutes)
                     for alert in open_alerts],
    }


def plan_responses(alerts, units, now, sla_minutes=FIRST_RESPONSE_SLA_MINUTES):
    """Compute every joint feasible first-response plan, or an escalation set.

    Returns a dict with either::

        {'feasible': True, 'plans': [plan, ...]}   # all valid routes, ordered

    or, when at least one open alert cannot be served without double-booking
    or missing its deadline::

        {'feasible': False, 'unserved': [evidence, ...],
         'escalation': [...]}

    The infeasible branch deliberately contains no partial dispatch: emitting
    a plausible-looking single-point plan while some alert misses its window
    would hide the failure from the desk.
    """
    open_alerts = [alert for alert in alerts if actionable(alert)]
    options, raw_plans = _enumerate_plans(open_alerts, units, now, sla_minutes)
    choices_by_alert = {alert['id']: [unit_id for unit_id, _ in choices]
                        for alert, choices in options}
    zero_option_ids = {alert['id'] for alert, choices in options if not choices}

    if not raw_plans:
        # Two failure shapes share one safe response: escalate every open
        # alert. An alert with zero feasible responders cannot be served at
        # all; alerts with options can still be unassignable jointly because
        # the only feasible responders are needed exclusively elsewhere
        # (resource contention). Neither case permits a partial pseudo-plan.
        evidence = [feasibility_evidence(alert, units, now, sla_minutes)
                    for alert in open_alerts]
        escalation = []
        for alert in open_alerts:
            feasible_ids = choices_by_alert.get(alert['id'], [])
            if alert['id'] in zero_option_ids:
                reason = ('no qualified, authorized responder meets the '
                          'worst-case first-response SLA')
            else:
                reason = ('joint resource contention: every feasible responder '
                          'is required exclusively by another concurrent alert; '
                          'double-booking a non-interruptible first-aid task is '
                          'not permitted')
            escalation.append({
                'alert_id': alert['id'],
                'trip_id': alert.get('trip_id'),
                'reason': reason,
                'candidate_points': candidate_points(alert),
                'sla_minutes': sla_minutes,
                'feasible_unit_ids': feasible_ids,
                'channel': 'external_emergency_services',
                'evidence_ref': 'feasibility:' + alert['id'],
            })
        return {'feasible': False, 'planned_at': now, 'sla_minutes': sla_minutes,
                'unserved': evidence, 'escalation': escalation}

    built = [_build_plan(open_alerts, units, assignment, now, sla_minutes)
             for assignment in raw_plans]
    built.sort(key=lambda plan: (
        max((task['worst_case_eta_minutes'] for task in plan['tasks']), default=0),
        sum(task['worst_case_eta_minutes'] for task in plan['tasks']),
        tuple(task['unit_id'] for task in plan['tasks']),
    ))
    for index, plan in enumerate(built):
        plan['route_index'] = index
    return {'feasible': True, 'planned_at': now, 'sla_minutes': sla_minutes,
            'plans': built, 'plan_count': len(built)}


# --------------------------------------------------------------------------- #
# Legacy pointwise behaviour, retained to document the seed defect
# --------------------------------------------------------------------------- #
def assign(alerts, units):
    """Legacy one-alert/one-point assignment.

    Kept for the seed regression test that pins the original defect (U1 handed
    to both A1 and A2 while A1's P2 hypothesis is dropped from the report).
    New code must call :func:`plan_responses`.
    """
    from .safety_alert import actionable as _actionable

    result = []
    for alert in alerts:
        if not _actionable(alert):
            continue
        point = alert['gps']['point']
        available = [u for u in units
                     if u.get('on_duty') and u.get('eta_minutes', {}).get(point) is not None]
        chosen = min(available, key=lambda u: u['eta_minutes'][point]) if available else None
        result.append({'alert_id': alert['id'],
                       'unit_id': chosen['id'] if chosen else None,
                       'eta_minutes': chosen['eta_minutes'][point] if chosen else None})
    return result
