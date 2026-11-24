import json
from pathlib import Path
from taxiproof.safety_alert import actionable, location_radius_km
from taxiproof.safety_response import assign


def test_current_pointwise_dispatch_exposes_shared_unit_and_uncertainty():
    fixture = json.loads((Path(__file__).parents[1] / 'fixtures' / 'safety-incident.json').read_text())
    assert actionable(fixture['alerts'][0])
    assert location_radius_km(fixture['alerts'][0]) == 4
    assert assign(fixture['alerts'], fixture['units']) == [
        {'alert_id': 'A1', 'unit_id': 'U1', 'eta_minutes': 4},
        {'alert_id': 'A2', 'unit_id': 'U1', 'eta_minutes': 4},
    ]
