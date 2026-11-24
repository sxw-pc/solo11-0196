"""Baseline road hazard decision from report count."""
from .suspension import positive_reports


def decide(rows):
    count = positive_reports(rows)
    return {'positive_reports': count, 'state': 'confirmed' if count >= 2 else 'unverified'}
