"""Baseline maintenance interval from a single raw meter reading."""
from .odometer import latest


def decide(data):
    km = latest(data['readings'])
    return {'meter_km': km, 'due': km - data['last_service_km'] >= data['interval_km']}
