"""Raw custody scans associated with trip objects."""

def latest(scans, trip):
    return next((s for s in reversed(scans) if s['trip'] == trip), None)
