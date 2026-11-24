import json
from pathlib import Path
from taxiproof.depot_plan import plan

def test_independent_slots_exceed_site_contract():
    data=json.loads((Path(__file__).parents[1]/'fixtures'/'depot-peak.json').read_text())
    result=plan(data)
    assert result['ready'] is True
    assert sum(s['kw'] for s in result['slots'])==44
    assert sum(s['kw'] for s in result['slots'])>data['grid_cap_kw']
