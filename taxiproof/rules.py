"""Meter authority, driver shifts and trip settlement from offline evidence."""
from collections import defaultdict
from .decisions import make_decision


def driver_assignment(assignments, driver, start, end):
    """Return the shift assignment carrying *driver* over the whole [start, end] window."""
    matches = [a for a in assignments if a['driver'] == driver
               and a['start'] <= start and a['end'] >= end]
    return matches[0] if matches else None


def permits_cover(assignment, end):
    """License and area permit must stay valid through the end of service."""
    return bool(assignment and assignment['license_until'] >= end
                and assignment['permit_until'] >= end)


def region_covered(assignment, region):
    """The shift assignment, not a vehicle whitelist, authorizes the service region."""
    return bool(assignment and region in assignment['regions'])


def assignment_authority(assignment, region, start, end):
    """Single authority predicate: shift coverage is checked by driver_assignment first."""
    return bool(assignment and permits_cover(assignment, end)
                and region_covered(assignment, region))


def recent(events, kind):
    groups = defaultdict(list)
    for event in events:
        if event['kind'] == kind:
            groups[event['case_id']].append(event)
    return {key: sorted(values, key=lambda x: (x['version'], x['effective_at'], x['id']))[-1]
            for key, values in groups.items()}


def record(result, reason, evidence):
    return make_decision(result, reason, evidence)


def calculate(events):
    meters, shifts, trips = (recent(events, kind) for kind in ('meter', 'shift', 'trip'))
    meter_history = defaultdict(list)
    for e in events:
        if e['kind'] == 'meter':
            meter_history[e['case_id']].append(e)
    output, states = {}, []
    for case in sorted(set(meters) | set(shifts) | set(trips)):
        meter, shift, trip = meters.get(case), shifts.get(case), trips.get(case)
        evidence = [e['id'] for e in (meter, shift, trip) if e]
        prefix = case + '/'
        calibrated = bool(meter and trip and not meter['calibration'].get('revoked')
                          and meter['calibration']['valid_from'] <= trip['start']
                          and meter['calibration']['valid_until'] >= trip['end'])
        rate_keys = defaultdict(set)
        for e in meter_history[case]:
            for rate in e['rates']:
                rate_keys[(rate['region'], rate['revision'], rate['start'], rate['end'])].add(rate['per_km'])
        conflict = any(len(prices) > 1 for prices in rate_keys.values())
        assignments = shift.get('assignments', []) if shift else []
        applicable = driver_assignment(assignments, trip['driver'], trip['start'], trip['end']) if trip else None
        driver_ok = bool(permits_cover(applicable, trip['end'])) if trip else False
        overlaps = [(a, b) for i, a in enumerate(assignments) for b in assignments[i+1:]
                    if a['vehicle'] == b['vehicle'] and a['start'] < b['end'] and b['start'] < a['end']]
        handover = shift.get('handover', {}) if shift else {}
        overlap_resolved = not overlaps or all(handover.get('from') == a['driver']
                                                and handover.get('to') == b['driver']
                                                and b['start'] <= handover.get('at', '') <= a['end']
                                                for a, b in overlaps)
        complaint = trip.get('complaint') if trip else None
        driver_dispute = bool(isinstance(complaint, dict)
                              and complaint.get('reason') == 'driver_identity'
                              and complaint.get('passenger_signature')
                              and complaint.get('reviewer_signature')
                              and complaint.get('vehicle') == trip['vehicle']
                              and complaint.get('observed_driver') != trip['driver']
                              and complaint.get('observed_driver')
                              and trip['start'] <= complaint.get('observed_at', '') <= trip['end'])
        region_ok = bool(trip and region_covered(applicable, trip['region']))
        old_driver = bool(trip and handover and trip['driver'] == handover.get('from')
                          and trip['end'] > handover.get('at', '9999'))
        segments = trip.get('segments', []) if trip else []
        ordered = sorted(segments, key=lambda s: s['start'])
        gap = bool(trip and (not ordered or ordered[0]['start'] != trip['start']
                    or ordered[-1]['end'] != trip['end']
                    or any(a['end'] != b['start'] for a, b in zip(ordered, ordered[1:]))))
        fare = 0.0
        wait_fee = 0.0
        rate_missing = False
        for segment in ordered:
            matched = [r for r in meter.get('rates', []) if r['region'] == trip['region']
                       and r['start'] <= segment['start'] and r['end'] >= segment['end']] if meter else []
            if len(matched) != 1:
                rate_missing = True
                continue
            rate = matched[0]
            fare += segment['distance_km'] * rate['per_km'] * meter['calibration']['factor']
            if segment.get('stationary') and segment.get('timestamp_trusted'):
                wait_fee += segment['wait_minutes'] * rate['per_wait_minute']
        fare = round(fare + wait_fee, 2)
        settlement_ok = calibrated and not conflict and driver_ok and overlap_resolved and region_ok and not old_driver and not gap and not rate_missing and not driver_dispute
        paid = trip.get('paid', 0) if trip else 0
        refund = round(max(0, paid - fare), 2) if settlement_ok else None
        output[prefix+'计价器校准是否覆盖起终点'] = record('有效' if calibrated else '失准', '检定起止与撤销状态', evidence)
        output[prefix+'同版费率冲突是否冻结计费'] = record('冻结' if conflict else '无冲突', '相同区域版次时段的价格一致性', evidence)
        output[prefix+'跨费率时段如何分段计费'] = record(f'{fare:.2f}' if settlement_ok else '待核', '逐段里程时段与费率匹配', evidence)
        output[prefix+'司机证书是否覆盖班次'] = record('有效' if driver_ok else '无资质', '营运许可覆盖整个行程', evidence)
        output[prefix+'同车班次重叠由谁承运'] = record('申诉重开' if driver_dispute else '已交班' if overlaps and overlap_resolved else '争议' if overlaps else '无重叠', '同车重叠、交班回执与双签司机身份申诉', evidence)
        output[prefix+'越区路线是否触发权限争议'] = record('正常' if region_ok else '越区', '司机班次许可区域与行程区域', evidence)
        output[prefix+'交班后旧司机能否继续接单'] = record('禁止' if old_driver else '待核' if driver_dispute else '允许', '交班时刻、接单司机和申诉观察', evidence)
        output[prefix+'轨迹缺口是否阻止确定里程'] = record('阻止' if gap else '完整', '相邻里程片段的时戳闭合', evidence)
        output[prefix+'候车时长是否据可信时间戳计价'] = record(f'{wait_fee:.2f}', '仅停车且可信时间戳计费', evidence)
        output[prefix+'多收款是否形成退款债务'] = record(f'{refund:.2f}' if refund is not None else '待核', '可信应收与支付差额', evidence)
        if meter:
            states.append(('计价器与费率确权', case, '有效' if calibrated and not conflict else '冻结', meter['version']))
        if shift:
            states.append(('司机车辆营运班次', case, '可派' if driver_ok and overlap_resolved and region_ok and not old_driver and not driver_dispute else '争议', shift['version']))
        if trip:
            states.append(('行程价款与申诉', case, '可结算' if settlement_ok else '待核', trip['version']))
    return output, states


def business_records(e):
    ident = e['id']
    if e['kind'] == 'meter':
        return [('计价器与费率确权', '计价器档案', ident+':calibration', e['calibration']),
                ('计价器与费率确权', '费率版本', ident+':rates', e['rates'])]
    if e['kind'] == 'shift':
        return [('司机车辆营运班次', '司机资格', ident+':drivers', [{'driver': a['driver'], 'license_until': a['license_until'], 'permit_until': a['permit_until']} for a in e['assignments']]),
                ('司机车辆营运班次', '车辆班次', ident+':shifts', {'assignments': e['assignments'], 'handover': e.get('handover')})]
    return [('行程价款与申诉', '行程轨迹', ident+':trip', {'segments': e['segments'], 'start': e['start'], 'end': e['end']}),
            ('行程价款与申诉', '支付申诉', ident+':payment', {'paid': e['paid'], 'complaint': e.get('complaint')})]
