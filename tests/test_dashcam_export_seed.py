import json
from pathlib import Path
from taxiproof.video_coverage import envelope
from taxiproof.export_gate import decide


def test_front_envelope_masks_middle_gap():
    data = json.loads((Path(__file__).parents[1] / 'fixtures' / 'dashcam-export.json').read_text())
    front = [row for row in data['segments'] if row['camera'] == 'front']
    assert envelope(front) == (1000, 1060)
    assert decide(data['trip'], front) == 'full_trip_exportable'
    shortened = [{**row, 'end_second': 1050} if row['id'] == 'FRONT-2' else row for row in front]
    assert decide(data['trip'], shortened) == 'hold'
