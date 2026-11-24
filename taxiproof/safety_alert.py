"""Passenger panic-button triage and alert lifecycle.

A panic button is a passenger-originated emergency signal. A driver tapping
"acknowledged" is a single-party confirmation that must never close the case
on its own: the alert stays open until a *trusted human callback* verifies the
situation and explicitly revokes it. Every state transition is evidence
bearing so the incident desk can reconstruct why an alert was or was not
dispatched.
"""

# Alert states recorded in the lifecycle journal.
OPEN = 'open'
REVOKED = 'revoked'
ESCALATED = 'escalated'

# Who may revoke a passenger emergency signal.
TRUSTED_REVIEWERS = ('human_callback', 'trusted_operator', 'supervisor')


def _signals(alert):
    return alert.get('signals', {})


def panic_pressed(alert):
    """True only when the passenger emergency button signal is present."""
    return bool(_signals(alert).get('panic_button'))


def driver_acknowledged(alert):
    """Whether the driver unilaterally confirmed the alert."""
    return bool(_signals(alert).get('driver_ack'))


def trusted_callbacks(alert):
    """Trusted human callbacks attached to the alert, newest first.

    Each callback carries ``at``, ``reviewer`` and a ``verdict`` of either
    ``verified`` (the emergency is real) or ``revoked`` (false alarm).
    """
    callbacks = alert.get('trusted_callbacks', [])
    return sorted(callbacks, key=lambda c: c.get('at', ''), reverse=True)


def revoked_by_trusted_callback(alert):
    """Return the trusted callback that revoked the alert, else ``None``.

    A driver acknowledgement is deliberately not consulted here: only a
    reviewer in :data:`TRUSTED_REVIEWERS` performing an explicit ``revoked``
    verdict can take a panic-button alert out of the emergency queue.
    """
    for callback in trusted_callbacks(alert):
        if callback.get('verdict') == 'revoked' and callback.get('reviewer') in TRUSTED_REVIEWERS:
            return callback
    return None


def verified_by_trusted_callback(alert):
    """Return the latest trusted callback that confirmed the emergency."""
    for callback in trusted_callbacks(alert):
        if callback.get('verdict') == 'verified' and callback.get('reviewer') in TRUSTED_REVIEWERS:
            return callback
    return None


def alert_state(alert):
    """Lifecycle state of an alert.

    ``escalated`` is sticky once external emergency services have been
    engaged; otherwise a trusted revocation closes the alert and every other
    panic-button alert remains ``open`` regardless of driver acknowledgement.
    """
    if alert.get('escalated_at'):
        return ESCALATED
    if panic_pressed(alert) and revoked_by_trusted_callback(alert):
        return REVOKED
    return OPEN


def actionable(alert):
    """Whether the alert must enter concurrent dispatch planning.

    Hard constraint one: a passenger panic button that has not been revoked by
    a trusted human callback stays actionable *even when the driver has
    acknowledged it*. The legacy desk closed such alerts on
    ``driver_ack == true``; that behaviour is intentionally removed.
    """
    if not panic_pressed(alert):
        return False
    return alert_state(alert) == OPEN


def location_radius_km(alert):
    gps = alert.get('gps', {})
    return gps.get('reported_radius_km', 0)


def candidate_points(alert):
    """All still-plausible physical locations for the passenger.

    A stale satellite fix yields more than one candidate. The reported point
    alone must never be treated as certain when a radius / candidate set says
    otherwise.
    """
    gps = alert.get('gps', {})
    points = gps.get('candidate_points')
    if points:
        return list(points)
    point = gps.get('point')
    return [point] if point is not None else []


def location_trust(alert):
    """Trust level of the current location evidence (``trusted``/``reported``)."""
    return alert.get('gps', {}).get('trust', 'reported')
