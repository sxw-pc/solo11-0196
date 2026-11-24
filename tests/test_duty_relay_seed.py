import json
from pathlib import Path
from taxiproof.duty import minutes
from taxiproof.fatigue import dispatchable

def test_relayed_duty_blocks_a_driver_as_if_two_shifts():
    d = json.loads((Path(__file__).parents[1] / 'fixtures' / 'duty-relay.json').read_text())
    assert minutes(d['logs'], d['driver']) == 600
    assert dispatchable(d['logs'], d['driver'], d['maximum_minutes']) is False
    assert d['logs'][0]['start'] < d['logs'][1]['end']
