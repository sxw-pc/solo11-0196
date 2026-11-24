"""Append-only evidence journal for emergency dispatch and re-planning.

The planner in :mod:`taxiproof.safety_response` produces candidate routes at an
instant. Real incidents evolve: trusted location fixes arrive late, callbacks
change the reachable set, and responders actually reach the scene. This module
keeps the *trace* of that evolution:

* a dispatch is a committed, notified decision with a revision;
* a later trusted update triggers a fresh joint plan over the still-unarrived
  tasks, and replacing a responder records an explicit cancellation plus a
  handover entry naming the previously notified person — assignments are never
  silently overwritten;
* remote voice contact is recorded but cannot mark a task as on-scene;
* passenger location and alert detail are exposed only to responders holding a
  trip authorization, while a public view shows acceptance, the response
  window and the review version with no candidate locations.
"""
from copy import deepcopy

from .safety_alert import (actionable, alert_state, candidate_points,
                           revoked_by_trusted_callback)
from .safety_response import (FIRST_RESPONSE_SLA_MINUTES, can_serve,
                              plan_responses)

JOURNAL_DISPATCH = 'dispatch'
JOURNAL_CANCEL = 'assignment_cancelled'
JOURNAL_HANDOVER = 'handover'
JOURNAL_ARRIVAL = 'on_scene_arrival'
JOURNAL_REMOTE = 'remote_contact'
JOURNAL_REPLAN = 'replan'
JOURNAL_ESCALATION = 'escalation'
JOURNAL_REVOKE = 'trusted_revocation'


class ReplanError(RuntimeError):
    """Raised when a trusted update cannot be reconciled with the SLA."""


class ResponseCapture:
    def __init__(self, alerts, units, now, sla_minutes=FIRST_RESPONSE_SLA_MINUTES):
        self.alerts = [deepcopy(alert) for alert in alerts]
        self.units = [deepcopy(unit) for unit in units]
        self.initial_now = now
        self.sla_minutes = sla_minutes
        self.journal = []
        self.revision = 0
        # alert_id -> committed task dict (lives until cancelled/arrived)
        self.tasks = {}
        # alert_id -> arrival record once the responder is physically there
        self.arrivals = {}
        # alert_id -> list of remote contact records
        self.remote_contacts = {}
        self.escalations = []

    # ------------------------------------------------------------------ #
    # Journal primitives
    # ------------------------------------------------------------------ #
    def _record(self, kind, at, **payload):
        entry = {'seq': len(self.journal) + 1, 'revision': self.revision,
                 'kind': kind, 'at': at, **payload}
        self.journal.append(entry)
        return entry

    def history(self, alert_id=None):
        """Journal entries, optionally filtered to one alert's evidence chain."""
        if alert_id is None:
            return [deepcopy(entry) for entry in self.journal]
        ids = {alert_id}
        return [deepcopy(entry) for entry in self.journal
                if alert_id in entry.get('alert_ids', ())
                or entry.get('alert_id') == alert_id]

    def evidence_chain(self, alert_id):
        """Full ordered trace for one alert, including handovers."""
        return self.history(alert_id)

    # ------------------------------------------------------------------ #
    # Initial concurrent dispatch
    # ------------------------------------------------------------------ #
    def dispatch(self, now, route_index=0):
        """Commit one of the jointly feasible routes and notify responders.

        All open alerts are decided together. If the joint problem is
        infeasible nothing is dispatched and the unserved alerts are escalated
        to external emergency services with their feasibility evidence.
        """
        decision = plan_responses(self.alerts, self.units, now, self.sla_minutes)
        if not decision['feasible']:
            return self._escalate_infeasible(decision, now, trigger='initial dispatch')
        route = decision['plans'][route_index]
        self.revision += 1
        for task in route['tasks']:
            committed = deepcopy(task)
            committed.update(revision=self.revision, status='dispatched',
                             notified_at=now)
            self.tasks[task['alert_id']] = committed
            self._record(JOURNAL_DISPATCH, now,
                         alert_ids=[task['alert_id']],
                         alert_id=task['alert_id'],
                         unit_id=task['unit_id'],
                         candidate_points=task['candidate_points'],
                         worst_case_eta_minutes=task['worst_case_eta_minutes'],
                         sla_minutes=task['sla_minutes'],
                         route_index=route_index,
                         notification='responder notified with authorized location',
                         evidence_ref='feasibility:' + task['alert_id'])
        return {'revision': self.revision, 'feasible': True,
                'tasks': [deepcopy(task) for task in self.tasks.values()],
                'reserve_unit_ids': route['reserve_unit_ids'],
                'route_count': decision['plan_count']}

    def _escalate_infeasible(self, decision, now, trigger):
        self.revision += 1
        for item in decision['escalation']:
            record = deepcopy(item)
            record.update(revision=self.revision, escalated_at=now, trigger=trigger)
            self.escalations.append(record)
            for alert in self.alerts:
                if alert['id'] == item['alert_id']:
                    alert['escalated_at'] = now
            self._record(JOURNAL_ESCALATION, now,
                         alert_ids=[item['alert_id']], alert_id=item['alert_id'],
                         channel=item['channel'], reason=item['reason'],
                         candidate_points=item['candidate_points'],
                         evidence_ref=item['evidence_ref'])
        return {'revision': self.revision, 'feasible': False,
                'unserved': deepcopy(decision['unserved']),
                'escalation': deepcopy(self.escalations)}

    # ------------------------------------------------------------------ #
    # On-scene progress and remote contact (never interchangeable)
    # ------------------------------------------------------------------ #
    def mark_arrived(self, alert_id, unit_id, at):
        task = self.tasks.get(alert_id)
        if task is None:
            raise ReplanError(f'no active dispatch for {alert_id}')
        if task['unit_id'] != unit_id:
            raise ReplanError(f'{unit_id} is not the dispatched responder for {alert_id}')
        arrival = {'alert_id': alert_id, 'unit_id': unit_id, 'arrived_at': at,
                   'mode': 'on_scene'}
        self.arrivals[alert_id] = arrival
        task['status'] = 'on_scene'
        task['arrived_at'] = at
        self._record(JOURNAL_ARRIVAL, at, alert_ids=[alert_id], alert_id=alert_id,
                     unit_id=unit_id, mode='on_scene')
        return deepcopy(arrival)

    def record_remote_contact(self, alert_id, unit_id, at, note=''):
        """Log a remote call. It never substitutes for physical arrival."""
        record = {'alert_id': alert_id, 'unit_id': unit_id, 'at': at,
                  'mode': 'remote', 'note': note, 'counts_as_arrival': False}
        self.remote_contacts.setdefault(alert_id, []).append(record)
        self._record(JOURNAL_REMOTE, at, alert_ids=[alert_id], alert_id=alert_id,
                     unit_id=unit_id, note=note,
                     notification='remote contact does not satisfy on-scene SLA')
        return deepcopy(record)

    # ------------------------------------------------------------------ #
    # Trusted evidence: revocation, location correction, ETA change
    # ------------------------------------------------------------------ #
    def apply_trusted_callback(self, alert_id, callback, at):
        """Apply a trusted human callback.

        A ``revoked`` verdict closes the alert and cancels its still-unarrived
        dispatch with evidence. Driver-only acknowledgement is routed through
        :meth:`note_driver_ack` and can never close an alert.
        """
        alert = self._alert(alert_id)
        alert.setdefault('trusted_callbacks', []).append(deepcopy(callback))
        if revoked_by_trusted_callback(alert):
            self._record(JOURNAL_REVOKE, at, alert_ids=[alert_id], alert_id=alert_id,
                         reviewer=callback.get('reviewer'),
                         notification='panic button revoked by trusted human callback')
            if alert_id in self.tasks and alert_id not in self.arrivals:
                self._cancel_task(alert_id, at, reason='trusted callback revoked alert')
        return alert_state(alert)

    def note_driver_ack(self, alert_id, at):
        """Record a unilateral driver acknowledgement; the alert stays open."""
        alert = self._alert(alert_id)
        alert.setdefault('signals', {})['driver_ack'] = True
        self._record('driver_acknowledgement', at, alert_ids=[alert_id],
                     alert_id=alert_id,
                     notification='driver-only acknowledgement cannot close a panic alert')
        return actionable(alert)

    def apply_location_correction(self, alert_id, correction, at):
        """Apply a late trusted location fix and replan unarrived tasks.

        ``correction`` holds the trusted candidate set (and optional reported
        point/radius/trust). Arrived tasks are frozen — responders already on
        scene are not reassigned — while every other open alert is re-solved
        jointly against the new reachability windows.
        """
        alert = self._alert(alert_id)
        old_points = candidate_points(alert)
        gps = alert.setdefault('gps', {})
        gps['point'] = correction.get('point', gps.get('point'))
        gps['candidate_points'] = list(correction['candidate_points'])
        if 'reported_radius_km' in correction:
            gps['reported_radius_km'] = correction['reported_radius_km']
        gps['trust'] = correction.get('trust', 'trusted')
        self._record('trusted_location_update', at, alert_ids=[alert_id],
                     alert_id=alert_id, previous_candidate_points=old_points,
                     candidate_points=list(correction['candidate_points']),
                     evidence_ref='trusted-fix:' + alert_id)
        return self._replan(at, trigger=f'trusted location correction for {alert_id}')

    def apply_eta_update(self, unit_id, eta_minutes, at):
        """Apply a trusted change to a responder's reachability and replan."""
        unit = self._unit(unit_id)
        previous = dict(unit.get('eta_minutes', {}))
        unit['eta_minutes'] = dict(eta_minutes)
        self._record('trusted_eta_update', at, alert_ids=sorted(self.tasks),
                     unit_id=unit_id, previous_eta_minutes=previous,
                     eta_minutes=dict(eta_minutes))
        return self._replan(at, trigger=f'trusted ETA update for {unit_id}')

    # ------------------------------------------------------------------ #
    # Joint re-planning with cancellation + handover evidence
    # ------------------------------------------------------------------ #
    def _replan(self, at, trigger):
        open_alerts = [alert for alert in self.alerts if actionable(alert)]
        # Tasks whose responder is physically on scene are immutable.
        frozen = {alert_id for alert_id in self.arrivals}
        active = [alert for alert in open_alerts if alert['id'] not in frozen]

        # Re-qualify every current assignment against the new evidence.
        retained = {}
        occupied_units = {self.arrivals[aid]['unit_id'] for aid in frozen}
        to_plan = []
        # Remember the withdrawn dispatch so the replacement can cite it as a
        # handover — the cancellation record alone must not erase who had
        # already been notified.
        withdrawn = {}
        for alert in active:
            task = self.tasks.get(alert['id'])
            if task is None:
                to_plan.append(alert)
                continue
            unit = self._unit(task['unit_id'])
            if can_serve(unit, alert, at, self.sla_minutes):
                retained[alert['id']] = task['unit_id']
                occupied_units.add(task['unit_id'])
            else:
                withdrawn[alert['id']] = {'unit_id': task['unit_id'],
                                          'revision': task['revision']}
                self._cancel_task(alert['id'], at,
                                  reason='assignment no longer meets worst-case SLA '
                                         'after trusted evidence')
                to_plan.append(alert)

        changes = []
        if to_plan:
            # Units locked by frozen arrivals or retained dispatches cannot
            # take a new non-interruptible task in the same window.
            available_units = [unit for unit in self.units
                               if unit['id'] not in occupied_units]
            decision = plan_responses(to_plan, available_units, at, self.sla_minutes)
            if not decision['feasible']:
                failure = self._escalate_infeasible(decision, at, trigger=trigger)
                failure['retained_assignments'] = deepcopy(retained)
                failure['frozen_arrivals'] = sorted(frozen)
                raise ReplanError({'trigger': trigger, 'result': failure})

            route = decision['plans'][0]
            self.revision += 1
            for task in route['tasks']:
                prior = withdrawn.get(task['alert_id'])
                previous = self.tasks.get(task['alert_id'])
                previous_unit = (prior['unit_id'] if prior else
                                 previous['unit_id'] if previous else None)
                previous_revision = (prior['revision'] if prior else
                                     previous['revision'] if previous else None)
                committed = deepcopy(task)
                committed.update(revision=self.revision, status='dispatched',
                                 notified_at=at,
                                 supersedes_revision=previous_revision)
                self.tasks[task['alert_id']] = committed
                if previous_unit is not None and previous_unit != task['unit_id']:
                    self._record(JOURNAL_HANDOVER, at,
                                 alert_ids=[task['alert_id']],
                                 alert_id=task['alert_id'],
                                 from_unit=previous_unit, to_unit=task['unit_id'],
                                 cancelled_dispatch_ref=JOURNAL_CANCEL + ':'
                                 + task['alert_id'],
                                 notification='previously notified responder handed '
                                              'off; briefing transferred with evidence '
                                              'chain')
                self._record(JOURNAL_DISPATCH, at,
                             alert_ids=[task['alert_id']],
                             alert_id=task['alert_id'],
                             unit_id=task['unit_id'],
                             candidate_points=task['candidate_points'],
                             worst_case_eta_minutes=task['worst_case_eta_minutes'],
                             sla_minutes=task['sla_minutes'],
                             route_index=0, revision=self.revision,
                             notification='new dispatch after trusted evidence')
                occupied_units.add(task['unit_id'])
                changes.append({'alert_id': task['alert_id'],
                                'unit_id': task['unit_id'],
                                'previous_unit_id': previous_unit})

        self._record(JOURNAL_REPLAN, at,
                     alert_ids=sorted(self.tasks), trigger=trigger,
                     revision=self.revision,
                     changes=changes, frozen_arrivals=sorted(frozen),
                     notification='recomputed on unarrived tasks; prior cancellations '
                                  'and handovers retained')
        return {'revision': self.revision, 'trigger': trigger,
                'changes': changes, 'retained_assignments': deepcopy(retained),
                'frozen_arrivals': sorted(frozen),
                'tasks': [deepcopy(task) for task in self.tasks.values()]}

    def _cancel_task(self, alert_id, at, reason, previous_unit=None,
                     cancelled_revision=None):
        task = self.tasks.pop(alert_id, None)
        if task is None and previous_unit is None:
            return
        unit_id = task['unit_id'] if task else previous_unit
        revision = task['revision'] if task else cancelled_revision
        self._record(JOURNAL_CANCEL, at, alert_ids=[alert_id], alert_id=alert_id,
                     unit_id=unit_id, cancelled_revision=revision,
                     reason=reason,
                     notification='dispatch explicitly withdrawn; responder informed')
        return {'alert_id': alert_id, 'unit_id': unit_id, 'revision': revision}

    # ------------------------------------------------------------------ #
    # Privacy views
    # ------------------------------------------------------------------ #
    def responder_view(self, unit_id):
        """What one responder may see.

        Need-to-know on top of the trip grant: alert detail (including
        candidate locations and signals) is returned only for alerts this
        responder is actually committed to. Other authorized-but-unassigned
        trips do not leak passenger location; the unit merely sees its grant
        list and an empty alert set.
        """
        unit = self._unit(unit_id)
        grants = set(unit.get('authorized_trips', []))
        visible = []
        for alert in self.alerts:
            if alert.get('trip_id') not in grants:
                continue
            task = self.tasks.get(alert['id'])
            if not task or task['unit_id'] != unit_id:
                continue
            visible.append({
                'alert_id': alert['id'],
                'trip_id': alert['trip_id'],
                'assigned_to_you': True,
                'candidate_points': candidate_points(alert),
                'signals': deepcopy(alert.get('signals', {})),
                'task': deepcopy(task),
            })
        return {'unit_id': unit_id, 'authorized_trips': sorted(grants),
                'alerts': visible}

    def public_status(self):
        """Customer-service-safe view: no candidate locations, no detail."""
        out = []
        for alert in self.alerts:
            state = alert_state(alert)
            task = self.tasks.get(alert['id'])
            window = {
                'accepted': state in ('open', 'escalated'),
                'response_window_minutes': self.sla_minutes,
                'review_version': task['revision'] if task else self.revision,
            }
            if alert['id'] in self.arrivals:
                window['phase'] = 'on_scene'
            elif state == 'revoked':
                window['phase'] = 'closed_after_trusted_callback'
            elif state == 'escalated':
                window['phase'] = 'escalated_external_emergency'
            elif task:
                window['phase'] = 'response_dispatched'
            else:
                window['phase'] = 'accepted'
            out.append({'alert_id': alert['id'], **window})
        return {'alerts': out, 'journal_revision': self.revision}

    # ------------------------------------------------------------------ #
    # Inspection helpers
    # ------------------------------------------------------------------ #
    def active_tasks(self):
        return [deepcopy(task) for task in sorted(self.tasks.values(),
                                                  key=lambda t: t['alert_id'])]

    def _alert(self, alert_id):
        for alert in self.alerts:
            if alert['id'] == alert_id:
                return alert
        raise ReplanError(f'unknown alert {alert_id}')

    def _unit(self, unit_id):
        for unit in self.units:
            if unit['id'] == unit_id:
                return unit
        raise ReplanError(f'unknown unit {unit_id}')
