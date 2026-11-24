import json
from pathlib import Path
from taxiproof.lift import deployments
from taxiproof.accessibility import served

def test_unattributed_lift_event_is_counted_as_passenger_service():
    d = json.loads((Path(__file__).parents[1] / 'fixtures' / 'lift-test-vs-ride.json').read_text())
    assert len(deployments(d['events'], d['booking']['vehicle'])) == 1
    assert served(d['booking'], d['events']) is True
    assert d['events'][0]['purpose'] == 'unknown'
    assert 'passenger' not in d['events'][0]
