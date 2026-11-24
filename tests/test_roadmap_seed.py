import json
from pathlib import Path
from taxiproof.suspension import positive_reports
from taxiproof.roadmap import decide


def test_relayed_vehicle_spike_is_counted_twice():
    data = json.loads((Path(__file__).parents[1] / 'fixtures' / 'road-spike-replay.json').read_text())
    assert positive_reports(data['reports']) == 2
    assert decide(data['reports']) == {'positive_reports': 2, 'state': 'confirmed'}
    assert {r['sensor_id'] for r in data['reports'] if r['vertical_g'] >= 2.5} == {'ACC-7'}
