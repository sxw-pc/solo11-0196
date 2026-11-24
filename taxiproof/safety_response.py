"""Current desk assignment: one alert at a time, one reported GPS point."""
from .safety_alert import actionable


def assign(alerts, units):
    result = []
    for alert in alerts:
        if not actionable(alert):
            continue
        available = [u for u in units if u.get('on_duty') and u.get('eta_minutes', {}).get(alert['gps']['point']) is not None]
        chosen = min(available, key=lambda u: u['eta_minutes'][alert['gps']['point']]) if available else None
        result.append({'alert_id': alert['id'], 'unit_id': chosen['id'] if chosen else None,
                       'eta_minutes': chosen['eta_minutes'][alert['gps']['point']] if chosen else None})
    return result
