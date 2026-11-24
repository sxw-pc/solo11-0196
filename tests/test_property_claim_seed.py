import json
from pathlib import Path
from taxiproof.property_claim import decide

def test_wrong_bag_handover_closes_claim():
    data=json.loads((Path(__file__).parents[1]/'fixtures'/'property-two-bags.json').read_text())
    assert decide(data)=={'claim':'returned','seal':'S-B'}
    assert data['claimed_seal'] != data['scans'][-1]['seal']
