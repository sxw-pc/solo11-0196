"""Joint curb planning over flight versions, cab shifts and the curb queue.

The planner never takes a greedy per-request nearest-cab decision. It enumerates
every *full* set of hard-constraint-compliant commitments — including the empty
return occupancy of each cab, queue-ticket fairness and explicit standby
releases — so that the two structurally different compliant promises for the
same two-order/two-cab input (reuse the returning cab, or release the standby
cab) are both visible and auditable.
"""
from dataclasses import dataclass, field

from .curb_capture import CurbDesk, DispatchError, evaluate_cab


@dataclass
class PlannedCommitment:
    request: str
    cab: str
    driver: str
    pickup_minute: int
    pickup_clock: str
    ticket_id: str = None
    standby_id: str = None
    requeue_at: int = None
    evidence: list = field(default_factory=list)


@dataclass
class Plan:
    commitments: list
    uses_standby: bool = False
    standby_ids: list = field(default_factory=list)
    total_wait_minutes: int = 0
    cabs_used: int = 0
    breaches: list = field(default_factory=list)
    rationale: str = ''

    def assignment_for(self, request_id):
        return next((c for c in self.commitments if c.request == request_id), None)

    def as_dicts(self):
        return [
            {'request': c.request, 'cab': c.cab, 'pickup_minute': c.pickup_minute,
             'pickup_clock': c.pickup_clock, 'ticket_id': c.ticket_id,
             'standby_id': c.standby_id, 'driver': c.driver,
             'evidence': list(c.evidence)}
            for c in sorted(self.commitments, key=lambda c: c.request)
        ]


def _sorted_requests(desk):
    return sorted(desk.requests, key=lambda rid: (desk.ready_of(rid), rid))


def _eligible(cab, desk, request_id, pickup_minute):
    ok, _ = evaluate_cab(cab, desk.requests[request_id], pickup_minute)
    return ok


def enumerate_plans(desk, request_ids=None):
    """Enumerate every full compliant commitment set on the supplied desk.

    Queue fairness: the active queue yields at most one FIFO branch per request
    — the earliest-ticket eligible cab; later-ticket cabs may never overtake.
    A cab returning from a trip rejoins via a freshly minted ticket at its
    curb-return minute. The standby reserve opens only while no FIFO cab is
    idle at the passenger's ready minute; at that point "wait for the returning
    cab" and "release the reserve early" appear as distinct plans whenever both
    are feasible, and the policy chooses.
    """
    order = request_ids or [rid for rid in _sorted_requests(desk)
                            if rid in desk.uncommitted_requests()]
    plans = []

    def branch_commitments(sim):
        return [
            PlannedCommitment(
                request=c.request, cab=c.cab, driver=c.driver,
                pickup_minute=c.pickup_minute, pickup_clock=c.pickup_clock,
                ticket_id=c.ticket_id, standby_id=c.standby_id,
                requeue_at=c.requeue_minute,
                evidence=list(c.evidence))
            for c in sim.commitments.values() if c.status == 'promised'
        ]

    def search(index):
        if index == len(order):
            commitments = branch_commitments(desk)
            plans.append(Plan(
                commitments=sorted(commitments, key=lambda c: c.request),
                uses_standby=any(c.standby_id for c in commitments),
                standby_ids=[c.standby_id for c in commitments if c.standby_id],
                total_wait_minutes=sum(c.pickup_minute - desk.ready_of(c.request)
                                       for c in commitments),
                cabs_used=len({c.cab for c in commitments}),
            ))
            return
        request_id = order[index]
        request = desk.requests[request_id]
        ready = desk.ready_of(request_id)
        latest = request['latest_pickup_minute']
        branches = []
        # Branch 1: earliest-ticket FIFO cab that can legally serve the request.
        # This covers both a cab already holding in the queue and a cab whose
        # empty return completes before the latest pickup: the latter re-enters
        # the queue on a brand-new ticket minted at its curb-return minute, so
        # the returning cab can never reuse the ticket consumed by the prior trip.
        active_cab = None
        for ticket, cab in desk.active_queue():
            pickup = max(ready, cab['available_at'])
            if pickup <= latest and _eligible(cab, desk, request_id, pickup):
                active_cab = (cab, pickup, False, None)
                break
        if active_cab is None:
            for cab in desk.cabs.values():
                if cab['status'] == 'pulled' or cab['active_ticket_id']:
                    continue
                if not cab['occupancy'] or cab['status'] not in ('assigned', 'carrying', 'returning'):
                    continue
                return_minute = cab['occupancy'][-1][1]
                pickup = max(ready, return_minute)
                if pickup <= latest and _eligible(cab, desk, request_id, pickup):
                    active_cab = (cab, pickup, False, return_minute)
                    break
        if active_cab is not None:
            branches.append(active_cab)
        # Branch 2: the explicitly authorized standby reserve.
        # A reserve can never overtake a FIFO cab that is already free at the
        # passenger's ready minute (queue fairness). But when every FIFO cab is
        # still occupied at that minute, releasing the reserve early (plan B)
        # is a genuine alternative to waiting for the returning cab (plan A);
        # both branches are retained and the policy chooses. The held queue
        # ticket and the standby authorization are both consumed on release.
        fifo_idle_now = any(
            cab['available_at'] <= ready
            for ticket, cab in desk.active_queue())
        if not fifo_idle_now:
            seen = set()
            for auth in desk.standby_pool(ready):
                cab = desk.cabs[auth.cab]
                if cab['id'] in seen or not cab['active_ticket_id']:
                    continue
                pickup = max(ready, cab['available_at'])
                if pickup <= latest and _eligible(cab, desk, request_id, pickup):
                    branches.append((cab, pickup, True, None))
                    seen.add(cab['id'])
        if not branches:
            # Record the unmet request and continue: other requests are still
            # independently solvable and breaches stay visible in the plan.
            plans_snapshot = list(plans)
            search(index + 1)
            for plan in plans[len(plans_snapshot):]:
                plan.breaches.append({
                    'request': request_id, 'reason': 'no_feasible_assignment',
                    'ready_minute': ready, 'latest_pickup_minute': latest})
            return
        for cab, pickup, release_standby, requeue_at in branches:
            snap = desk.snapshot()
            try:
                desk.commit(request_id, cab['id'], pickup,
                            release_standby=release_standby,
                            requeue_at=requeue_at)
                search(index + 1)
            finally:
                desk.restore(snap)

    search(0)
    return _distinct_plans(plans)


def _distinct_plans(plans):
    unique, seen = [], set()
    for plan in plans:
        key = tuple((c.request, c.cab, c.pickup_minute) for c in plan.commitments)
        if key not in seen:
            seen.add(key)
            unique.append(plan)
    return unique


def choose_plan(plans, policy='reserve_standby'):
    """Pick a compliant plan under an explicit, auditable policy.

    ``reserve_standby`` (default): keep standby cabs free for unexpected orders,
    reusing returning cabs whenever the latest pickup is still met.
    ``minimize_wait``: prefer the plan with the least passenger waiting, even if
    it releases standby and puts more cabs into paid service.
    """
    if not plans:
        return None
    if policy == 'minimize_wait':
        return sorted(plans, key=lambda p: (p.total_wait_minutes, p.cabs_used,
                                            p.uses_standby))[0]
    return sorted(plans, key=lambda p: (p.uses_standby, p.total_wait_minutes,
                                        p.cabs_used))[0]


def materialize(desk, plan):
    """Issue the real commitments captured by a plan on the desk."""
    for planned in plan.commitments:
        desk.commit(planned.request, planned.cab, planned.pickup_minute,
                    release_standby=planned.standby_id is not None,
                    requeue_at=planned.requeue_at)
    return desk


def build_desk(requests, cabs, standby_authorizations=None):
    return CurbDesk(requests, cabs, standby_authorizations)


def assign(requests, cabs, standby_authorizations=None, policy='reserve_standby',
           return_plan=False):
    """Compliant assignment: one authoritative commitment per request.

    A request that no cab can serve before its latest pickup is returned as an
    ``unmet`` row with its breach reason rather than silently dropped or, worse,
    given an illegal overlapping commitment.
    """
    desk = build_desk(requests, cabs, standby_authorizations)
    plans = enumerate_plans(desk)
    if not plans:
        raise DispatchError('no_compliant_plan')
    plan = choose_plan(plans, policy)
    materialize(desk, plan)
    breach_by_request = {b['request']: b for b in plan.breaches}
    rows = []
    for request_id in _sorted_requests(desk):
        commitment = desk.commitments.get(request_id)
        if commitment is None or commitment.status in ('cancelled', 'superseded', 'breached'):
            breach = breach_by_request.get(request_id)
            rows.append({'request': request_id, 'cab': None, 'pickup_minute': None,
                         'status': 'unmet',
                         'reason': breach['reason'] if breach else 'no_feasible_assignment'})
        else:
            rows.append({'request': commitment.request, 'cab': commitment.cab,
                         'driver': commitment.driver,
                         'pickup_minute': commitment.pickup_minute,
                         'pickup_clock': commitment.pickup_clock,
                         'status': commitment.status,
                         'ticket_id': commitment.ticket_id,
                         'standby_id': commitment.standby_id,
                         'evidence': list(commitment.evidence)})
    if return_plan:
        return rows, plan, plans
    return rows


def replan(desk, policy='reserve_standby'):
    """Re-plan only the requests left unbound after a disruption.

    This is a full joint recomputation over current occupancy, tickets and
    standby state — never a sort-key tweak on the old ranking.
    """
    plans = enumerate_plans(desk)
    if not plans:
        return None
    plan = choose_plan(plans, policy)
    materialize(desk, plan)
    return plan
