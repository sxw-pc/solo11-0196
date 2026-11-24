import json
from pathlib import Path
from taxiproof.odometer import latest
from taxiproof.maintenance_due import decide


def test_ecu_replacement_hides_cumulative_service_distance():
    data = json.loads((Path(__file__).parents[1] / 'fixtures' / 'ecu-swap-service.json').read_text())
    assert latest(data['readings']) == 1800
    assert decide(data) == {'meter_km': 1800, 'due': False}
    assert data['readings'][0]['kilometres'] + data['readings'][1]['kilometres'] == 151800
