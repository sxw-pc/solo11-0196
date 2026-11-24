"""Baseline claim disposition from the latest trip scan."""
from .property_scan import latest

def decide(data):
    scan = latest(data['scans'], data['trip'])
    return {'claim': 'returned' if scan and scan['kind']=='handover' else 'open', 'seal': scan['seal'] if scan else None}
