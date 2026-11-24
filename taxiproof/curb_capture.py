"""Evidence-capturing airport curb dispatch desk.

This module is the auditable state machine behind curb dispatch. It deliberately
keeps *evidence* separate from *decisions*:

* flight/landing/baggage facts arrive as versioned evidence and never mutate an
  already promised plate/time;
* each cab holds one queue ticket at a time, and a consumed ticket can never
  claim dispatch priority twice — re-entering the queue mints a new ticket whose
  position is verifiable;
* temporary standby is an explicit, time-bounded authorization, distinct from a
  queue ticket;
* every vehicle/driver occupancy interval (carrying, empty return, holding,
  next pickup) is recorded so overlap is impossible to hide;
* every binding change emits separate driver and passenger notices.

Driver license, permit, shift and region authority are judged exclusively
through :mod:`taxiproof.rules`. The ``regions`` whitelist on a fixture cab is
untrusted marketing data and is never consulted here.
"""
from dataclasses import asdict, dataclass, field
from copy import deepcopy

from . import rules
from .flight_arrival import arrival_evidence, clock, ready_minute


class DispatchError(ValueError):
    """A requested dispatch action violates a hard constraint."""

    def __init__(self, reason, **details):
        super().__init__(reason)
        self.reason = reason
        self.details = details


@dataclass
class QueueTicket:
    id: str
    cab: str
    driver: str
    issued_minute: int
    status: str = 'held'            # held | consumed | revoked
    consumed_minute: int = None
    consumed_by: str = None
    evidence: str = ''

    def is_active(self):
        return self.status == 'held'


@dataclass
class StandbyAuthorization:
    id: str
    cab: str
    driver: str
    issued_minute: int
    valid_from: int
    valid_until: int
    status: str = 'active'          # active | released | revoked
    used_by: str = None
    used_minute: int = None
    evidence: str = ''

    def covers(self, minute):
        return self.status == 'active' and self.valid_from <= minute <= self.valid_until


@dataclass
class Commitment:
    request: str
    cab: str
    driver: str
    pickup_minute: int
    pickup_clock: str
    ready_minute: int
    latest_pickup_minute: int
    status: str                     # promised | picked_up | cancelled | superseded | breached
    ticket_id: str = None
    standby_id: str = None
    requeue_ticket_id: str = None
    requeue_minute: int = None
    cancel_requeue_ticket_id: str = None
    flight_version: str = ''
    evidence: list = field(default_factory=list)
    cancel_reason: str = None


@dataclass
class Notice:
    id: str
    minute: int
    audience: str                   # driver | passenger
    subject: str                    # driver id or request id
    type: str
    message: str
    evidence: list = field(default_factory=list)


def _shift_assignments(cab):
    spec = cab.get('spec', cab)
    shift = spec.get('shift') or {}
    return shift.get('assignments', [])


def evaluate_cab(cab, request, pickup_minute):
    """Pure hard-constraint evaluation of one cab for one pickup minute.

    Returns ``(ok, reason)``. Authority is judged via :mod:`taxiproof.rules`;
    the cab's top-level ``regions`` whitelist is intentionally never read.
    """
    if cab['status'] == 'pulled':
        return False, 'cab_pulled'
    if pickup_minute < cab['ready_minute']:
        return False, 'cab_not_ready'
    # Occupancy intervals are half-open [start, end): carrying, empty return
    # and the next pickup all live on this one timeline and may never overlap.
    # A cab is free again exactly at its curb-return minute.
    if any(pickup_minute < end and start <= pickup_minute
           for start, end in cab['occupancy']):
        return False, 'overlapping_occupancy'
    if cab['seats'] < request['passengers']:
        return False, 'capacity'
    window_end = pickup_minute + cab['return_to_curb_minutes']
    assignment = rules.driver_assignment(_shift_assignments(cab), cab['driver'],
                                         pickup_minute, window_end)
    if not rules.assignment_authority(assignment, request['region'],
                                      pickup_minute, window_end):
        return False, 'shift_authority'
    return True, 'eligible'


class CurbDesk:
    """Stateful dispatch desk rebuilding every decision from evidence."""

    def __init__(self, requests, cabs, standby_authorizations=None, now_minute=0):
        self.now_minute = now_minute
        self.requests = {}
        self.flight_versions = {}
        for request in requests:
            stored = dict(request)
            self.requests[request['id']] = stored
            self.flight_versions[request['id']] = [{
                'id': f"{request['id']}:flight:v1",
                'delay_minutes': request.get('flight_delay_minutes', 0),
                'source': 'schedule',
            }]
        self.cabs = {}
        self.tickets = []
        self.ticket_seq = 0
        standby_cabs = {item['cab'] for item in (standby_authorizations or [])}
        for cab in cabs:
            state = {
                'spec': dict(cab),
                'id': cab['id'],
                'driver': cab['driver'],
                'seats': cab['seats'],
                'ready_minute': cab['ready_minute'],
                'return_to_curb_minutes': cab['return_to_curb_minutes'],
                'status': 'standby' if cab['id'] in standby_cabs else 'holding',
                'occupancy': [],
                'active_ticket_id': None,
                'available_at': cab['ready_minute'],
            }
            self.cabs[cab['id']] = state
            ticket = QueueTicket(
                id=cab.get('initial_ticket_id', f"{cab['id']}-T1"),
                cab=cab['id'], driver=cab['driver'],
                issued_minute=cab['queue_ticket_minute'],
                evidence=f"{cab['id']}:queue-ticket")
            self.tickets.append(ticket)
            state['active_ticket_id'] = ticket.id
            self.ticket_seq += 1
        self.standby = [StandbyAuthorization(**item)
                        for item in (standby_authorizations or [])]
        self.commitments = {}
        self.notices = []
        self.notice_seq = 0
        self.breaches = []

    # ----- introspection -------------------------------------------------

    def ready_of(self, request_id):
        return ready_minute(self.requests[request_id])

    def latest_of(self, request_id):
        return self.requests[request_id]['latest_pickup_minute']

    def active_queue(self):
        """Cabs with a held queue ticket, in verifiable FIFO ticket order.

        A cab under an active temporary standby authorization is a reserve and
        is deliberately absent from the FIFO queue until the authorization is
        released, revoked or consumed.
        """
        queued = []
        for ticket in sorted(self.tickets, key=lambda t: (t.issued_minute, t.id)):
            if ticket.is_active():
                cab = self.cabs[ticket.cab]
                if cab['status'] != 'pulled' and not self._in_standby(cab['id']):
                    queued.append((ticket, cab))
        return queued

    def _in_standby(self, cab_id):
        return any(a.cab == cab_id and a.status == 'active' for a in self.standby)

    def standby_pool(self, minute):
        return [auth for auth in self.standby if auth.covers(minute)]

    def ticket(self, ticket_id):
        return next(t for t in self.tickets if t.id == ticket_id)

    def uncommitted_requests(self):
        return [rid for rid in sorted(self.requests)
                if rid not in self.commitments
                or self.commitments[rid].status in ('cancelled', 'superseded', 'breached')]

    # ----- notices --------------------------------------------------------

    def _notice(self, minute, audience, subject, ntype, message, evidence=None):
        self.notice_seq += 1
        notice = Notice(f'N-{self.notice_seq:03d}', minute, audience, subject,
                        ntype, message, list(evidence or []))
        self.notices.append(notice)
        return notice

    def notices_for(self, request_id=None, cab_id=None):
        out = list(self.notices)
        if request_id is not None:
            out = [n for n in out if n.audience == 'passenger' and n.subject == request_id]
        if cab_id is not None:
            driver = self.cabs[cab_id]['driver']
            out = [n for n in out if n.audience == 'driver' and n.subject == driver]
        return out

    # ----- commitment -----------------------------------------------------

    def _dispatch_token(self, cab, pickup_minute, release_standby, request_id):
        ticket = None
        if cab['active_ticket_id']:
            candidate = self.ticket(cab['active_ticket_id'])
            if candidate.is_active() and candidate.issued_minute <= pickup_minute:
                ticket = candidate
        auth = None
        if release_standby:
            for candidate in self.standby:
                if candidate.cab == cab['id'] and candidate.covers(pickup_minute):
                    auth = candidate
                    break
        return ticket, auth

    def commit(self, request_id, cab_id, pickup_minute=None, release_standby=False,
               requeue_at=None):
        """Issue an evidenced, hard-constraint-checked pickup commitment.

        ``requeue_at`` is set when the cab only reaches the curb again at that
        minute: a fresh queue ticket is minted and consumed by this commitment,
        proving the returning cab re-entered the queue rather than reusing its
        prior ticket.
        """
        if request_id not in self.requests:
            raise DispatchError('unknown_request', request=request_id)
        request = self.requests[request_id]
        cab = self.cabs[cab_id]
        ready = self.ready_of(request_id)
        if pickup_minute is None:
            pickup_minute = max(ready, cab['available_at'])
        if pickup_minute < ready:
            raise DispatchError('passenger_not_at_curb', request=request_id,
                                ready=ready, pickup=pickup_minute)
        if pickup_minute > request['latest_pickup_minute']:
            raise DispatchError('past_latest_pickup', request=request_id,
                                pickup=pickup_minute,
                                latest=request['latest_pickup_minute'])
        ok, reason = evaluate_cab(cab, request, pickup_minute)
        if not ok:
            raise DispatchError(reason, request=request_id, cab=cab_id,
                                pickup=pickup_minute)
        if requeue_at is not None:
            requeue_ticket = self._mint_requeue_ticket(cab, requeue_at)
        else:
            requeue_ticket = None
        ticket, auth = self._dispatch_token(cab, pickup_minute,
                                            release_standby, request_id)
        if ticket is None:
            raise DispatchError('no_held_queue_ticket', cab=cab_id,
                                request=request_id)
        if release_standby and auth is None:
            raise DispatchError('standby_not_authorized', cab=cab_id,
                                request=request_id)
        arrival = arrival_evidence(request, self._current_flight_version(request_id))
        evidence = list(arrival['evidence'])
        ticket.status = 'consumed'
        ticket.consumed_minute = pickup_minute
        ticket.consumed_by = request_id
        cab['active_ticket_id'] = None
        evidence.append(ticket.id)
        if auth:
            auth.status = 'released'
            auth.used_by = request_id
            auth.used_minute = pickup_minute
            evidence.append(auth.id)
        interval_end = pickup_minute + cab['return_to_curb_minutes']
        cab['occupancy'].append([pickup_minute, interval_end])
        cab['status'] = 'assigned'
        cab['available_at'] = max(cab['available_at'], interval_end)
        commitment = Commitment(
            request=request_id, cab=cab_id, driver=cab['driver'],
            pickup_minute=pickup_minute, pickup_clock=clock(pickup_minute),
            ready_minute=ready,
            latest_pickup_minute=request['latest_pickup_minute'],
            status='promised', ticket_id=ticket.id if ticket else None,
            standby_id=auth.id if auth else None,
            requeue_ticket_id=requeue_ticket.id if requeue_ticket else None,
            requeue_minute=requeue_at,
            flight_version=arrival['flight_version'], evidence=evidence)
        self.commitments[request_id] = commitment
        self._notice(pickup_minute, 'driver', cab['driver'], 'dispatch',
                     f'{cab_id} dispatched to {request_id} at {clock(pickup_minute)}',
                     evidence)
        self._notice(pickup_minute, 'passenger', request_id, 'promise',
                     f'cab {cab_id} promised at {clock(pickup_minute)}',
                     evidence)
        return commitment

    def _current_flight_version(self, request_id):
        versions = self.flight_versions[request_id]
        baseline = versions[0]
        current = versions[-1]
        return current if current is not baseline else None

    # ----- trip lifecycle -------------------------------------------------

    def mark_picked_up(self, request_id, at_minute):
        """Record a real pickup; illegal before the passenger reaches the curb."""
        commitment = self.commitments.get(request_id)
        if not commitment or commitment.status != 'promised':
            raise DispatchError('no_promised_commitment', request=request_id)
        ready = self.ready_of(request_id)
        if at_minute < ready:
            raise DispatchError('passenger_not_at_curb', request=request_id,
                                ready=ready, attempted=at_minute)
        if at_minute > commitment.latest_pickup_minute:
            raise DispatchError('past_latest_pickup', request=request_id,
                                pickup=at_minute)
        commitment.status = 'picked_up'
        cab = self.cabs[commitment.cab]
        cab['status'] = 'carrying'
        cab['pickup_evidence'] = f'{request_id}:pickup@{at_minute}'
        self._notice(at_minute, 'driver', commitment.driver, 'pickup_confirmed',
                     f'{commitment.cab} picked up {request_id}',
                     commitment.evidence)
        self._notice(at_minute, 'passenger', request_id, 'pickup_confirmed',
                     f'pickup confirmed at {clock(at_minute)}',
                     commitment.evidence)
        return commitment

    def _mint_requeue_ticket(self, cab, at_minute):
        self.ticket_seq += 1
        ticket = QueueTicket(
            id=f'{cab["id"]}-T{self.ticket_seq}', cab=cab['id'], driver=cab['driver'],
            issued_minute=at_minute, status='held',
            evidence=f'{cab["id"]}:requeue@{at_minute}')
        self.tickets.append(ticket)
        cab['active_ticket_id'] = ticket.id
        cab['status'] = 'requeued'
        cab['available_at'] = max(cab['available_at'], at_minute)
        return ticket

    def mark_returned(self, cab_id, at_minute):
        """Cab finishes its empty return and re-enters the queue on a NEW ticket."""
        cab = self.cabs[cab_id]
        if cab['status'] not in ('carrying', 'returning'):
            raise DispatchError('cab_not_on_trip', cab=cab_id, status=cab['status'])
        if cab['occupancy']:
            cab['occupancy'][-1][1] = max(cab['occupancy'][-1][1], at_minute)
        ticket = self._mint_requeue_ticket(cab, at_minute)
        self._notice(at_minute, 'driver', cab['driver'], 'requeued',
                     f'{cab_id} re-entered curb queue on {ticket.id}',
                     [ticket.evidence])
        return ticket

    def report_return_delay(self, cab_id, actual_available_minute, evidence):
        """Extend the active trip's return occupancy and cancel collisions.

        The delay applies to the interval of the trip currently carrying/returning,
        never to a later interval reserved for a not-yet-started commitment.
        """
        cab = self.cabs[cab_id]
        if not cab['occupancy']:
            raise DispatchError('no_active_occupancy', cab=cab_id)
        # The active trip is the earliest interval tied to a real (picked-up or
        # currently promised) commitment; later intervals are forward reservations.
        bound_starts = sorted(c.pickup_minute for c in self.commitments.values()
                              if c.cab == cab_id and c.status in ('picked_up', 'promised'))
        active_start = bound_starts[0] if bound_starts else cab['occupancy'][0][0]
        active_index = next((i for i, (start, end) in enumerate(cab['occupancy'])
                             if start == active_start), 0)
        planned_end = cab['occupancy'][active_index][1]
        if actual_available_minute <= planned_end:
            raise DispatchError('not_a_delay', cab=cab_id,
                                planned=planned_end, actual=actual_available_minute)
        # First cancel every not-yet-started commitment the delayed return
        # collides with; cancellation releases those forward intervals and
        # reissues a fresh queue ticket, leaving only real trip occupancy.
        affected = []
        for request_id, commitment in list(self.commitments.items()):
            if commitment.cab != cab_id or commitment.status != 'promised':
                continue
            if commitment.pickup_minute < actual_available_minute:
                self._cancel_commitment(request_id, 'return_delay_collision',
                                        evidence, replannable=True)
                affected.append(request_id)
        if active_index >= len(cab['occupancy']):
            active_index = len(cab['occupancy']) - 1
        cab['occupancy'][active_index][1] = actual_available_minute
        cab['available_at'] = max(cab['available_at'], actual_available_minute)
        cab['status'] = 'returning'
        self._notice(actual_available_minute, 'driver', cab['driver'],
                     'return_delay',
                     f'{cab_id} return delayed until {clock(actual_available_minute)}',
                     [evidence])
        return affected

    def pull_driver(self, cab_id, at_minute, reason, evidence):
        """Temporarily withdraw a driver: revoke authority and all their bindings."""
        cab = self.cabs[cab_id]
        cab['status'] = 'pulled'
        for auth in self.standby:
            if auth.cab == cab_id and auth.status == 'active':
                auth.status = 'revoked'
        if cab['active_ticket_id']:
            ticket = self.ticket(cab['active_ticket_id'])
            ticket.status = 'revoked'
            cab['active_ticket_id'] = None
        self._notice(at_minute, 'driver', cab['driver'], 'driver_pulled',
                     f'{cab_id} driver pulled: {reason}', [evidence])
        affected = []
        for request_id, commitment in list(self.commitments.items()):
            if commitment.cab == cab_id and commitment.status == 'promised':
                self._cancel_commitment(request_id, 'driver_pulled', evidence,
                                        replannable=True)
                affected.append(request_id)
        return affected

    def mark_no_show(self, request_id, at_minute):
        """Passenger is overdue: release the binding and return the cab to queue."""
        commitment = self.commitments.get(request_id)
        if not commitment or commitment.status != 'promised':
            raise DispatchError('no_promised_commitment', request=request_id)
        if at_minute <= commitment.latest_pickup_minute:
            raise DispatchError('not_yet_overdue', request=request_id,
                                latest=commitment.latest_pickup_minute)
        cab = self.cabs[commitment.cab]
        self._cancel_commitment(request_id, 'passenger_no_show',
                                f'{request_id}:no-show@{at_minute}',
                                replannable=False, requeue=False)
        cab['status'] = 'returning'
        return self.mark_returned(cab['id'], at_minute)

    def change_plate(self, request_id, new_cab_id, at_minute, evidence):
        """Revoke an existing binding and rebind to a different plate if legal."""
        commitment = self.commitments.get(request_id)
        if not commitment or commitment.status != 'promised':
            raise DispatchError('no_promised_commitment', request=request_id)
        self._cancel_commitment(request_id, 'plate_change', evidence, replannable=True)
        try:
            return self.commit(request_id, new_cab_id)
        except DispatchError as exc:
            self._notice(at_minute, 'passenger', request_id, 'rebind_failed',
                         f'new plate {new_cab_id} unavailable: {exc.reason}',
                         [evidence])
            return None

    # ----- cancellation internals ----------------------------------------

    def _release_forward_occupancy(self, commitment):
        """Drop the planned occupancy interval of an unstarted commitment.

        Intervals of completed (picked-up) trips are never touched, so a late
        flight update cannot free a vehicle that is already carrying passengers.
        """
        if commitment.status == 'picked_up':
            return
        cab = self.cabs[commitment.cab]
        target = [commitment.pickup_minute,
                  commitment.pickup_minute + cab['return_to_curb_minutes']]
        cab['occupancy'] = [interval for interval in cab['occupancy']
                            if interval != target]
        if cab['occupancy']:
            cab['available_at'] = max(cab['ready_minute'],
                                      max(end for start, end in cab['occupancy']))
        else:
            cab['available_at'] = cab['ready_minute']
            if cab['status'] != 'pulled':
                cab['status'] = 'holding'

    def _cancel_commitment(self, request_id, reason, evidence, replannable,
                           requeue=True):
        commitment = self.commitments[request_id]
        commitment.status = 'cancelled'
        commitment.cancel_reason = reason
        self._release_forward_occupancy(commitment)
        # The consumed ticket stays consumed: a cancelled unstarted binding
        # re-enters the queue on a brand-new ticket, so one ticket can never
        # claim dispatch priority twice.
        if requeue:
            self._requeue_after_unbind(commitment)
        self._notice(commitment.pickup_minute, 'driver', commitment.driver,
                     'commitment_cancelled',
                     f'{commitment.cab} released from {request_id}: {reason}',
                     [evidence])
        self._notice(commitment.pickup_minute, 'passenger', request_id,
                     'commitment_cancelled',
                     f'cab {commitment.cab} commitment cancelled: {reason}',
                     [evidence])
        if not replannable:
            return
        # Keep the request available for a fresh evidenced commitment; the cab's
        # surviving occupancy intervals still protect every completed trip.

    # ----- flight evidence versions --------------------------------------

    def add_flight_version(self, request_id, version):
        """Apply an attested new flight correction without rewriting promises."""
        if request_id not in self.requests:
            raise DispatchError('unknown_request', request=request_id)
        self.flight_versions[request_id].append(dict(version))
        request = self.requests[request_id]
        request['flight_delay_minutes'] = version['delay_minutes']
        request['flight_version_id'] = version['id']
        new_ready = self.ready_of(request_id)
        arrival = arrival_evidence(request, version)
        commitment = self.commitments.get(request_id)
        if commitment is None or commitment.status in ('cancelled', 'superseded', 'breached'):
            return {'outcome': 'ready_updated', 'request': request_id,
                    'ready_minute': new_ready, 'evidence': arrival['evidence']}
        if commitment.status == 'picked_up':
            return {'outcome': 'immutable_completed', 'request': request_id,
                    'ready_minute': new_ready, 'evidence': arrival['evidence']}
        # A promised, not-yet-served commitment: never move it silently.
        if new_ready > request['latest_pickup_minute']:
            old = asdict(commitment)
            commitment.status = 'breached'
            commitment.cancel_reason = 'late_beyond_latest_pickup'
            self._release_forward_occupancy(commitment)
            requeue_ticket = self._requeue_after_unbind(commitment)
            self.breaches.append({
                'request': request_id, 'reason': 'late_beyond_latest_pickup',
                'ready_minute': new_ready,
                'latest_pickup_minute': request['latest_pickup_minute'],
                'flight_version': version['id'],
                'evidence': arrival['evidence'],
            })
            self._notice(new_ready, 'driver', commitment.driver, 'breach',
                         f'{commitment.cab} cannot serve {request_id}: passenger '
                         f'late beyond {clock(request["latest_pickup_minute"])}',
                         arrival['evidence'])
            self._notice(new_ready, 'passenger', request_id, 'breach',
                         f'cannot be picked up by {clock(request["latest_pickup_minute"])}; '
                         f'now arriving {clock(new_ready)}', arrival['evidence'])
            return {'outcome': 'breach', 'request': request_id,
                    'ready_minute': new_ready, 'superseded': old,
                    'requeue_ticket': requeue_ticket,
                    'evidence': arrival['evidence']}
        old = asdict(commitment)
        commitment.status = 'superseded'
        commitment.cancel_reason = f'new_flight_version:{version["id"]}'
        self._release_forward_occupancy(commitment)
        requeue_ticket = self._requeue_after_unbind(commitment)
        self._notice(new_ready, 'passenger', request_id, 'commitment_superseded',
                     f'original {commitment.cab} promise at {clock(commitment.pickup_minute)} '
                     f'replaced under {version["id"]}', arrival['evidence'])
        return {'outcome': 'new_commitment_required', 'request': request_id,
                'ready_minute': new_ready, 'superseded': old,
                'requeue_ticket': requeue_ticket,
                'evidence': arrival['evidence']}

    def _requeue_after_unbind(self, commitment):
        """Mint the fresh queue ticket owed after an unstarted binding ends."""
        cab = self.cabs[commitment.cab]
        if cab['status'] == 'pulled' or cab['active_ticket_id'] is not None:
            return None
        ticket = self._mint_requeue_ticket(
            cab, max(commitment.pickup_minute, cab['ready_minute']))
        commitment.cancel_requeue_ticket_id = ticket.id
        return ticket.id

    # ----- snapshots & reporting -----------------------------------------

    def snapshot(self):
        return {
            'now_minute': self.now_minute,
            'requests': deepcopy(self.requests),
            'flight_versions': deepcopy(self.flight_versions),
            'cabs': deepcopy(self.cabs),
            'tickets': deepcopy(self.tickets),
            'standby': deepcopy(self.standby),
            'commitments': deepcopy(self.commitments),
            'notices': deepcopy(self.notices),
            'ticket_seq': self.ticket_seq,
            'notice_seq': self.notice_seq,
            'breaches': deepcopy(self.breaches),
        }

    def restore(self, snap):
        self.now_minute = snap['now_minute']
        self.requests = deepcopy(snap['requests'])
        self.flight_versions = deepcopy(snap['flight_versions'])
        self.cabs = deepcopy(snap['cabs'])
        self.tickets = deepcopy(snap['tickets'])
        self.standby = deepcopy(snap['standby'])
        self.commitments = deepcopy(snap['commitments'])
        self.notices = deepcopy(snap['notices'])
        self.ticket_seq = snap['ticket_seq']
        self.notice_seq = snap['notice_seq']
        self.breaches = deepcopy(snap['breaches'])

    def report(self):
        return {
            'commitments': {rid: asdict(c) for rid, c in self.commitments.items()},
            'unmet': [
                {'request': rid, 'reason': 'no_feasible_assignment'}
                for rid in self.uncommitted_requests()
            ] + list(self.breaches),
            'tickets': [asdict(t) for t in self.tickets],
            'standby': [asdict(a) for a in self.standby],
            'occupancy': {cid: list(cab['occupancy']) for cid, cab in self.cabs.items()},
            'notices': [asdict(n) for n in self.notices],
        }
