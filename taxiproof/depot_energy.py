"""Vehicle energy needed before committed departures, metered at the site inlet.

required() prices every committed job in site-side energy: battery demand
grossed up by charging efficiency plus auxiliary load while the gun is
occupied, limited by the battery acceptance curve and the SOC cap.  The
aggregate gap against the standing grid contract drives depot_plan's
interval allocation, trip eligibility and revision directives.
"""

EPS = 1e-9


def minutes(text):
    """'07:00' -> 420 (minutes after midnight)."""
    hour, minute = str(text).split(':')
    return int(hour) * 60 + int(minute)


def accept_kw(vehicle, soc):
    """Battery acceptance power at a given state of charge."""
    for band in vehicle.get('acceptance', []):
        if soc < band['until_soc'] - EPS:
            return band['kw']
    return 0.0


def trips_by_id(data):
    return {t['id']: t for t in data.get('trips', [])}


def job_demand(data, job):
    """Battery energy a job must receive and the SOC it must reach."""
    vehicle = data['vehicles'][job['vehicle']]
    trip = trips_by_id(data).get(job['trip'], {})
    battery = vehicle['battery_kwh']
    min_soc = trip.get('min_soc', 0.0)
    reserve = trip.get('reserve_kwh', 0.0)
    need = max(job['required_kwh'], (min_soc - vehicle['soc']) * battery, 0.0)
    target_soc = vehicle['soc'] + need / battery
    violations = []
    if target_soc > vehicle.get('soc_cap', 1.0) + EPS:
        violations.append('soc_cap')
    if vehicle['soc'] * battery + need < reserve - EPS:
        violations.append('reserve')
    connector = job.get('connector', vehicle.get('connector'))
    if connector not in {c['connector'] for c in data['chargers']}:
        violations.append('connector')
    return {'vehicle': job['vehicle'], 'trip': job['trip'],
            'departure': minutes(job['departure']),
            'priority': job.get('priority', 'ordinary'), 'connector': connector,
            'kwh_battery': round(need, 6), 'target_soc': round(target_soc, 6),
            'min_soc': min_soc, 'reserve_kwh': reserve, 'violations': violations}


def charge_profile(vehicle, kwh_target, charger_kw, eff, aux_kw, step_min=5):
    """Standalone best-case session: minutes and site-side kWh for kwh_target.

    A 22 kW nameplate does not deliver 22 kWh per hour to the battery: the
    acceptance curve tapers with SOC, the SOC cap stops intake, and every
    site-side kWh pays charging losses plus the auxiliary load.
    """
    soc = vehicle['soc']
    battery = vehicle['battery_kwh']
    soc_cap = vehicle.get('soc_cap', 1.0)
    delivered = site = 0.0
    elapsed = 0
    while delivered < kwh_target - EPS and elapsed < 24 * 60:
        kw = min(charger_kw, accept_kw(vehicle, soc))
        if kw <= EPS or soc >= soc_cap - EPS:
            break
        dt = step_min / 60
        kw = min(kw, (kwh_target - delivered) / dt, (soc_cap - soc) * battery / dt)
        delivered += kw * dt
        site += kw * dt / eff + aux_kw * dt
        soc += kw * dt / battery
        elapsed += step_min
    return {'minutes': elapsed, 'kwh_site': round(site, 6),
            'feasible': delivered >= kwh_target - EPS}


def required(data):
    """Per-job real energy demand and the aggregate gap vs the base contract."""
    site = data.get('site', {})
    eff = site.get('charge_efficiency', 1.0)
    aux = site.get('aux_kw_per_session', 0.0)
    step = site.get('interval_minutes', 5)
    best_kw = max((c['kw'] for c in data['chargers']), default=0.0)
    jobs = {}
    for job in data['jobs']:
        demand = job_demand(data, job)
        profile = charge_profile(data['vehicles'][job['vehicle']],
                                 demand['kwh_battery'], best_kw, eff, aux, step)
        demand['min_minutes'] = profile['minutes']
        demand['kwh_site_min'] = profile['kwh_site']
        if not profile['feasible'] and 'soc_cap' not in demand['violations']:
            demand['violations'].append('acceptance')
        jobs[job['vehicle']] = demand
    start, end = minutes(data['start']), minutes(data['end'])
    available = data['grid_cap_kw'] * (end - start) / 60
    total = round(sum(j['kwh_site_min'] for j in jobs.values()), 6)
    return {'jobs': jobs, 'window': [start, end], 'total_site_kwh': total,
            'available_site_kwh': round(available, 6),
            'gap_kwh': round(total - available, 6)}
