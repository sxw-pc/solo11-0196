"""Baseline independent charger allocation."""
from .depot_energy import required

def plan(data):
    needs=required(data['jobs'])
    slots=[]
    for vehicle,kwh in needs.items():
        charger=data['chargers'][len(slots)%len(data['chargers'])]
        slots.append({'vehicle':vehicle,'charger':charger['id'],'start':data['start'],'end':data['end'],'kw':charger['kw'],'kwh':min(kwh,charger['kw'])})
    return {'slots':slots,'ready':all(s['kwh']>=needs[s['vehicle']] for s in slots)}
