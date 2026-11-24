"""Small offline alert triage used by the incident desk."""


def actionable(alert):
    signals = alert.get('signals', {})
    return bool(signals.get('panic_button') and not signals.get('driver_ack'))


def location_radius_km(alert):
    gps = alert.get('gps', {})
    return gps.get('reported_radius_km', 0)
