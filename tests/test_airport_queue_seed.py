import json
from pathlib import Path
from taxiproof.flight_arrival import ready_minute
from taxiproof.curb_dispatch import assign


def test_current_assignment_reuses_cab_for_overlapping_pickups():
    data = json.loads((Path(__file__).parents[1] / 'fixtures' / 'airport-queue.json').read_text())
    assert ready_minute(data['requests'][0]) == 390
    assert assign(data['requests'], data['cabs']) == [
        {'request': 'P1', 'cab': 'CAB-1', 'pickup_minute': 390},
        {'request': 'P2', 'cab': 'CAB-1', 'pickup_minute': 405},
    ]
    delayed = {**data['requests'][1], 'flight_delay_minutes': 20}
    assert assign([data['requests'][0], delayed], data['cabs'])[1]['pickup_minute'] == 425
