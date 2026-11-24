"""Count a vehicle's lift deployment telemetry."""

def deployments(events, vehicle):
    return [e for e in events if e['vehicle'] == vehicle and e['stage'] == 'deployed']
