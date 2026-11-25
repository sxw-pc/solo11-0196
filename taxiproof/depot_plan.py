"""Revisioned depot charging and dispatch planner.

Every issued plan is a revision: capacity grants, trip substitutions,
charger faults, cancellations and new dispatch commitments each trigger a
replan of the affected commitments.  Only the latest revision is active;
superseded revisions stay readable and never hold resources.  A revision
never silently exceeds the contract capacity in force at the time and
never marks an unmet trip ready.
"""
from .depot_energy import EPS, accept_kw, minutes, required, trips_by_id

PRIORITY = {'medical': 0, 'ordinary': 1}


def _fmt(t):
    return f'{t // 60:02d}:{t % 60:02d}'


def _intervals(data):
    step = data.get('site', {}).get('interval_minutes', 5)
    start = minutes(data['start'])
    end = minutes(data.get('horizon_end', data['end']))
    return [(t, t + step) for t in range(start, end, step)], step


def _effective_caps(data, grants, intervals):
    """Contract capacity per interval.

    A temporary grant only counts inside its authorized window and only
    for intervals that start after the supplier approved it; a retroactive
    approval cannot legalize an overload that already happened.
    """
    caps, used = [], set()
    for t0, t1 in intervals:
        cap = data['grid_cap_kw']
        for grant in grants:
            w0, w1 = minutes(grant['window'][0]), minutes(grant['window'][1])
            if w0 <= t0 and t1 <= w1 and minutes(grant['approved_at']) <= t0:
                if grant['kw'] > cap:
                    used.add(grant['id'])
                cap = max(cap, grant['kw'])
        caps.append(cap)
    return caps, used


def _fault_windows(directives):
    faults = {}
    for d in directives:
        if d['type'] == 'charger_fault':
            begin = minutes(d['at'])
            faults.setdefault(d['charger'], []).append(
                (begin, begin + d.get('cooldown_minutes', 0)))
    return faults


def _tariff(data, t0):
    for band in data.get('tariffs', []):
        if minutes(band['start']) <= t0 < minutes(band['end']):
            return band['per_kwh']
    return 0.0


def _simulate(data, demands, caps, faults, committed, deferred, fixed):
    """Interval simulation.

    Committed jobs may charge in intervals ending at or before their
    departure; deferred jobs only after the peak window.  `fixed` replays
    frozen per-interval (charger, kw) assignments of surviving jobs so a
    substitution replans only what it must.  Guns are exclusive: a charger
    serves one session, is held through disconnect turnaround, and is
    blocked during fault cooldown.  A fault-displaced job may preempt a
    lower-priority session after its disconnect turnaround.
    """
    intervals, step = _intervals(data)
    dt = step / 60
    site = data.get('site', {})
    eff = site.get('charge_efficiency', 1.0)
    aux = site.get('aux_kw_per_session', 0.0)
    turnaround = site.get('turnaround_minutes', 0)
    window_end = minutes(data['end'])
    vehicles = data['vehicles']
    chargers = {c['id']: c for c in data['chargers']}

    fixed_occ, fixed_busy = {}, {}
    for vehicle, script in (fixed or {}).items():
        for i, (charger, kw) in script.items():
            fixed_occ.setdefault(i, {})[charger] = kw
            fixed_busy[charger] = max(fixed_busy.get(charger, 0),
                                      intervals[i][1] + turnaround)

    dynamic = list(committed) + [v for v in deferred if v not in committed]
    rank = {v: (0 if v in committed else 1, demands[v]['departure'],
                PRIORITY.get(demands[v]['priority'], 9),
                -demands[v]['kwh_battery'], v) for v in dynamic}
    order = sorted(dynamic, key=rank.get)
    committed_set = set(committed)
    state = {v: {'got': 0.0, 'soc': vehicles[v]['soc']} for v in dynamic}
    holder = {c: None for c in chargers}
    vcharger = {v: None for v in dynamic}
    busy_until = {c: 0 for c in chargers}
    assignments = {v: {} for v in dynamic}
    runs, open_runs = [], {}
    load, cost = [], 0.0

    def close(vehicle, t):
        run = open_runs.pop(vehicle, None)
        if run:
            run['end'] = _fmt(t)
            run['kwh'] = round(run['kwh'], 6)
            run['kwh_site'] = round(run['kwh_site'], 6)
            runs.append(run)

    for i, (t0, t1) in enumerate(intervals):
        displaced = []
        for c, windows in faults.items():
            active = [f1 for f0, f1 in windows if f0 <= t0 < f1]
            if active:
                busy_until[c] = max(busy_until[c], max(active))
                if holder[c] is not None:
                    v = holder[c]
                    holder[c], vcharger[v] = None, None
                    displaced.append(v)
                    close(v, t0)
        for c in chargers:
            v = holder[c]
            if v is None:
                continue
            done = state[v]['got'] >= demands[v]['kwh_battery'] - EPS
            expired = v in committed_set and t0 >= demands[v]['departure']
            if done or expired:
                holder[c], vcharger[v] = None, None
                busy_until[c] = max(busy_until[c], t0 + turnaround)
                close(v, t0)
        occ = fixed_occ.get(i, {})
        site_kw = sum(kw / eff + aux for kw in occ.values() if kw > EPS)
        eligible = []
        for v in order:
            if state[v]['got'] >= demands[v]['kwh_battery'] - EPS:
                continue
            if v in committed_set:
                if t1 <= demands[v]['departure']:
                    eligible.append(v)
            elif t0 >= window_end:
                eligible.append(v)
        for v in eligible:
            if vcharger[v] is not None:
                continue
            for c in sorted(chargers):
                if (holder[c] is None and busy_until[c] <= t0 and c not in occ
                        and fixed_busy.get(c, 0) <= t0
                        and chargers[c]['connector'] == demands[v]['connector']):
                    holder[c], vcharger[v] = v, c
                    break
        for v in sorted(displaced, key=rank.get):
            if v not in eligible or vcharger[v] is not None:
                continue
            for c in sorted(chargers):
                other = holder[c]
                if (other is not None and rank[v] < rank[other]
                        and chargers[c]['connector'] == demands[v]['connector']):
                    vcharger[other] = None
                    close(other, t0)
                    holder[c], vcharger[v] = v, c
                    busy_until[c] = max(busy_until[c], t0 + turnaround)
                    break
        budget = caps[i] - site_kw
        for v in eligible:
            c = vcharger[v]
            if c is None or busy_until[c] > t0:
                continue
            st, vehicle = state[v], vehicles[v]
            kw = min(chargers[c]['kw'], accept_kw(vehicle, st['soc']),
                     (demands[v]['kwh_battery'] - st['got']) / dt,
                     (vehicle.get('soc_cap', 1.0) - st['soc'])
                     * vehicle['battery_kwh'] / dt)
            if kw <= EPS:
                continue
            if kw / eff + aux <= budget + EPS:
                take = kw
            elif budget > aux:
                take = min(kw, (budget - aux) * eff)
            else:
                continue
            if take <= EPS:
                continue
            got = take * dt
            st['got'] += got
            st['soc'] += got / vehicle['battery_kwh']
            budget -= take / eff + aux
            site_kw += take / eff + aux
            assignments[v][i] = (c, round(take, 6))
            run = open_runs.get(v)
            if run and run['charger'] != c:
                close(v, t0)
                run = None
            if run is None:
                run = open_runs[v] = {'vehicle': v, 'charger': c,
                                      'start': _fmt(t0), 'kw': 0.0,
                                      'kwh': 0.0, 'kwh_site': 0.0}
            run['kw'] = round(max(run['kw'], take), 6)
            run['kwh'] += got
            run['kwh_site'] += take / eff * dt + aux * dt
        cost += site_kw * dt * _tariff(data, t0)
        load.append({'start': _fmt(t0), 'end': _fmt(t1),
                     'kw': round(site_kw, 6), 'cap_kw': caps[i]})
    for v in list(open_runs):
        close(v, intervals[-1][1])
    return {'delivered': {v: state[v]['got'] for v in dynamic}, 'runs': runs,
            'load': load, 'assignments': assignments, 'cost': cost}


def _valid_substitution(data, subs, trip_id, used_reserve, rejected):
    """A substitute must be authorized inside its committed time limit and
    satisfy driver, time-window, condition and order commitments."""
    rv = data.get('reserve_vehicle')
    trip = trips_by_id(data).get(trip_id)
    if not rv or not trip:
        return None
    cover = rv.get('can_cover')
    cover = cover if isinstance(cover, list) else [cover]
    for sub in subs:
        if sub.get('trip') != trip_id:
            continue
        reason = None
        vehicle = data['vehicles'].get(sub.get('vehicle'), {})
        energy = vehicle.get('soc', 0.0) * vehicle.get('battery_kwh', 0.0)
        if sub.get('vehicle') != rv.get('vehicle') or not rv.get('charge_ready'):
            reason = 'not_ready'
        elif rv.get('condition') != 'inspected' or not rv.get('driver_certified'):
            reason = 'driver_or_condition'
        elif minutes(rv['available']) > minutes(trip['departure']):
            reason = 'time_window'
        elif trip_id not in cover:
            reason = 'coverage'
        elif minutes(sub['authorized_at']) > minutes(sub['commit_by']):
            reason = 'commit_window'
        elif energy < max(trip.get('min_soc', 0.0) * vehicle.get('battery_kwh', 0.0),
                          trip.get('reserve_kwh', 0.0)) - EPS:
            reason = 'reserve_energy'
        elif sub['vehicle'] in used_reserve:
            reason = 'reserve_busy'
        if reason is None:
            return sub
        rejected.append({'id': sub.get('id'), 'reason': reason})
    return None


def _conflicts(data, demands, caps, faults, committed):
    """Minimal job sets that are jointly infeasible in the peak window.

    A set is reported only when every proper subset is feasible on its
    own, so the list is the minimal proof of why the orders cannot all be
    honoured under the capacity in force.
    """
    from itertools import combinations

    def feasible(subset):
        sim = _simulate(data, demands, caps, faults, list(subset), [], {})
        return all(sim['delivered'][v] >= demands[v]['kwh_battery'] - EPS
                   for v in subset)

    window = [i for i, (t0, t1) in enumerate(_intervals(data)[0])
              if minutes(data['start']) <= t0 and t1 <= minutes(data['end'])]
    step_h = _intervals(data)[1] / 60
    available = round(sum(caps[i] for i in window) * step_h, 6)
    found = []
    vehicles = list(committed)
    for size in range(1, len(vehicles) + 1):
        for combo in combinations(vehicles, size):
            if feasible(combo):
                continue
            if any(not feasible(sub) for r in range(1, size)
                   for sub in combinations(combo, r)):
                continue
            demanded = round(sum(demands[v]['kwh_site_min'] for v in combo), 6)
            found.append({'jobs': sorted(demands[v]['trip'] for v in combo),
                          'window': [data['start'], data['end']],
                          'demanded_site_kwh': demanded,
                          'available_site_kwh': available,
                          'gap_kwh': round(demanded - available, 6),
                          'reason': 'energy' if demanded > available else 'time'})
    return found


def _occupancy_ok(slots, load):
    by_charger = {}
    for slot in slots:
        by_charger.setdefault(slot['charger'], []).append(slot)
    for charger, runs in by_charger.items():
        runs = sorted(runs, key=lambda r: r['start'])
        if any(minutes(b['start']) < minutes(a['end'])
               for a, b in zip(runs, runs[1:])):
            return False
    by_vehicle = {}
    for slot in slots:
        by_vehicle.setdefault(slot['vehicle'], []).append(slot)
    for vehicle, runs in by_vehicle.items():
        runs = sorted(runs, key=lambda r: r['start'])
        if any(minutes(b['start']) < minutes(a['end'])
               for a, b in zip(runs, runs[1:])):
            return False
    return all(p['kw'] <= p['cap_kw'] + 1e-6 for p in load)


def _effective_data(data, directives):
    extra = [d for d in directives if d['type'] == 'commit']
    if not extra:
        return data
    merged = dict(data)
    merged['vehicles'] = dict(data['vehicles'])
    merged['jobs'] = list(data['jobs'])
    merged['trips'] = list(data.get('trips', []))
    for d in extra:
        spec = dict(d['vehicle'])
        merged['vehicles'][spec.pop('id')] = spec
        merged['jobs'].append(d['job'])
        merged['trips'].append(d['trip'])
    return merged


def _build_revision(data, directives, trigger, number):
    data = _effective_data(data, directives)
    demands = required(data)['jobs']
    grants = [d for d in directives if d['type'] == 'capacity_grant']
    subs = [d for d in directives if d['type'] == 'substitution']
    faults = _fault_windows(directives)
    intervals, _ = _intervals(data)
    caps, used_grants = _effective_caps(data, grants, intervals)
    rejected = []
    violated = {v: d['violations'] for v, d in demands.items() if d['violations']}
    committed = [j['vehicle'] for j in data['jobs']
                 if j['vehicle'] not in violated]
    probe = _simulate(data, demands, caps, faults, committed, [], {})
    deferred, dispatch_subs, used_reserve = [], {}, set()
    unfulfilled, reasons = [], {}
    for job in data['jobs']:
        vehicle, trip_id = job['vehicle'], job['trip']
        if vehicle in violated:
            unfulfilled.append(trip_id)
            reasons[trip_id] = 'vehicle:' + ','.join(violated[vehicle])
        elif probe['delivered'][vehicle] >= demands[vehicle]['kwh_battery'] - EPS:
            pass
        else:
            sub = _valid_substitution(data, subs, trip_id, used_reserve, rejected)
            if sub:
                dispatch_subs[trip_id] = sub
                used_reserve.add(sub['vehicle'])
                deferred.append(vehicle)
            else:
                unfulfilled.append(trip_id)
                reasons[trip_id] = 'site_window'
    if deferred:
        survivors = [v for v in committed if v not in deferred]
        fixed = {v: probe['assignments'][v] for v in survivors}
        final = _simulate(data, demands, caps, faults, [], deferred, fixed)
        slots = [r for r in probe['runs'] if r['vehicle'] in survivors]
        slots += final['runs']
        load, cost = final['load'], final['cost']
        delivered = dict(probe['delivered'])
        delivered.update(final['delivered'])
    else:
        slots, load = probe['runs'], probe['load']
        delivered, cost = probe['delivered'], probe['cost']
    slots.sort(key=lambda r: (r['start'], r['charger']))

    dispatch, margins = [], {}
    for job in data['jobs']:
        vehicle, trip_id = job['vehicle'], job['trip']
        got = delivered.get(vehicle, 0.0)
        spec = data['vehicles'][vehicle]
        battery = spec['battery_kwh']
        soc = spec['soc'] + got / battery
        if trip_id in dispatch_subs:
            sub = dispatch_subs[trip_id]
            dispatch.append({'trip': trip_id, 'vehicle': sub['vehicle'],
                             'departure': job['departure'], 'substitute': True,
                             'ok': True, 'authorized_by': sub['id']})
        else:
            ok = (vehicle not in violated
                  and got >= demands[vehicle]['kwh_battery'] - EPS)
            dispatch.append({'trip': trip_id, 'vehicle': vehicle,
                             'departure': job['departure'], 'substitute': False,
                             'ok': ok, 'soc_at_departure': round(soc, 6),
                             'min_soc': demands[vehicle]['min_soc'],
                             'reserve_kwh': demands[vehicle]['reserve_kwh']})
        margin = {'kwh_delivered': round(got, 6),
                  'soc_at_departure': round(soc, 6),
                  'min_soc': demands[vehicle]['min_soc'],
                  'margin_kwh': round(spec['soc'] * battery + got
                                      - demands[vehicle]['min_soc'] * battery, 6),
                  'reserve_kwh': demands[vehicle]['reserve_kwh'],
                  'reserve_margin_kwh': round(spec['soc'] * battery + got
                                              - demands[vehicle]['reserve_kwh'], 6)}
        if vehicle in deferred:
            ends = [minutes(r['end']) for r in slots if r['vehicle'] == vehicle]
            margin['deferred'] = True
            margin['ready_at'] = _fmt(max(ends)) if ends else None
        margins[vehicle] = margin
    conflicts = _conflicts(data, demands, caps, faults, committed) \
        if unfulfilled else []
    approvals = [{'id': g['id'], 'type': 'capacity_grant', 'kw': g['kw'],
                  'window': g['window'], 'approved_at': g['approved_at'],
                  'approver': g.get('approver')}
                 for g in grants if g['id'] in used_grants]
    approvals += [{'id': s['id'], 'type': 'substitution', 'trip': s['trip'],
                   'vehicle': s['vehicle'], 'authorized_at': s['authorized_at'],
                   'commit_by': s['commit_by'], 'manager': s.get('manager')}
                  for s in dispatch_subs.values()]
    return {'revision': number, 'trigger': trigger, 'active': False,
            'ready': not unfulfilled and all(d['ok'] for d in dispatch),
            'slots': slots, 'dispatch': dispatch, 'load_curve': load,
            'margins': margins, 'unfulfilled': sorted(unfulfilled),
            'reasons': reasons, 'conflicts': conflicts,
            'approvals': approvals, 'rejected_directives': rejected,
            'directives': [d['id'] for d in directives],
            'cost': round(cost, 2),
            'occupancy_ok': _occupancy_ok(slots, load)}


def plan(data, events=None):
    """Replay the initial issue plus every directive into a revision log."""
    if events is None:
        events = data.get('events', data.get('directives', []))
    revisions = [_build_revision(data, [], 'initial', 1)]
    active_directives = []
    for event in events:
        if event.get('type') == 'cancel':
            kept = [d for d in active_directives if d['id'] != event.get('target')]
            revision = _build_revision(data, kept,
                                       event.get('id', 'cancel'),
                                       len(revisions) + 1)
            if len(kept) == len(active_directives):
                revision['rejected_directives'].append(
                    {'id': event.get('id'), 'reason': 'unknown_target'})
            active_directives = kept
        else:
            active_directives.append(event)
            revision = _build_revision(data, list(active_directives),
                                       event.get('id', event.get('type')),
                                       len(revisions) + 1)
        revisions.append(revision)
    revisions[-1]['active'] = True
    head = revisions[-1]
    result = {key: head[key] for key in
              ('revision', 'trigger', 'active', 'ready', 'slots', 'dispatch',
               'load_curve', 'margins', 'unfulfilled', 'reasons', 'conflicts',
               'approvals', 'rejected_directives', 'directives', 'cost',
               'occupancy_ok')}
    result['revisions'] = revisions
    result['required'] = required(_effective_data(data, active_directives))
    result['history'] = [{'revision': r['revision'], 'trigger': r['trigger'],
                          'ready': r['ready'], 'directives': r['directives']}
                         for r in revisions]
    return result
