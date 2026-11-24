"""Passenger curb readiness derived from landing, baggage and flight evidence.

A passenger only becomes reachable at the curb after three independently
evidenced components have all elapsed:

1. landing time (wheel-on at the gate),
2. baggage / deplaning wait,
3. an attested flight correction (delay or early arrival).

Every component carries its own evidence identifier so that a later flight
version can never silently move an already promised pickup time: the new
version gets a new evidence id and therefore produces a *new* commitment
rather than rewriting history.
"""


def ready_minute(request):
    """Minute-of-day when the passenger can physically reach the curb."""
    return (request['landing_minute']
            + request['baggage_minutes']
            + request.get('flight_delay_minutes', 0))


def clock(minute):
    """Render a minute-of-day (e.g. 390) as a clock string (``06:30``)."""
    return f'{minute // 60:02d}:{minute % 60:02d}'


def arrival_components(request, flight_version=None):
    """Evidence-tagged components that sum to the passenger's ready minute.

    ``flight_version`` is an optional attested correction::

        {'id': 'FLIGHT-P2-V2', 'delay_minutes': 20, 'source': 'AODB'}

    When omitted, the request's own ``flight_delay_minutes`` (default zero)
    is used as the baseline flight correction.
    """
    request_id = request['id']
    delay = request.get('flight_delay_minutes', 0)
    evidence_id = f'{request_id}:flight:v1'
    source = 'schedule'
    if flight_version is not None:
        delay = flight_version['delay_minutes']
        evidence_id = flight_version['id']
        source = flight_version.get('source', 'flight-feed')
    return [
        {'component': 'landing', 'minutes': request['landing_minute'],
         'evidence': f'{request_id}:landing'},
        {'component': 'baggage', 'minutes': request['baggage_minutes'],
         'evidence': f'{request_id}:baggage'},
        {'component': 'flight_correction', 'minutes': delay,
         'evidence': evidence_id, 'source': source},
    ]


def arrival_evidence(request, flight_version=None):
    """Full, auditable synthesis of when a passenger reaches the curb."""
    components = arrival_components(request, flight_version)
    ready = sum(part['minutes'] for part in components)
    return {
        'request': request['id'],
        'ready_minute': ready,
        'ready_clock': clock(ready),
        'components': components,
        'evidence': [part['evidence'] for part in components],
        'flight_version': flight_version['id'] if flight_version
        else f"{request['id']}:flight:v1",
    }


def apply_flight_version(request, flight_version):
    """Return a request copy with an attested flight correction applied.

    The original request is never mutated: each flight version is a separate
    evidence fact that downstream dispatch must treat as a new promise
    decision, not as an edit of an existing plate/time commitment.
    """
    updated = dict(request)
    updated['flight_delay_minutes'] = flight_version['delay_minutes']
    updated['flight_version_id'] = flight_version['id']
    return updated
