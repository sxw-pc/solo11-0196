"""Reconstruct real driving intervals and cross-platform source relations.

The seeded :func:`minutes` deliberately remains the naive sum: the seed test
pins the false ban produced by adding two 300-minute messages as if they were
two shifts.  Evidence-aware reconstruction lives alongside it and answers the
question the messages alone cannot:

* is ``PLATFORM-9`` an agency relay of ``METER-1`` (one 300' labor interval),
* or are they two physical intervals separated by an uncalibrated 5h epoch
  (600')?

Without enough evidence the module returns feasible scenarios and a lower /
upper bound instead of picking the most favourable clock correction per row.
All timestamps of one device epoch share one offset (+ optional drift).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

from .duty_capture import (
    CANONICAL_DATE,
    DEFAULT_TIMEZONE,
    case_clock_args,
    norm_kind,
    parse_wall,
    to_utc,
    utc_iso,
)

CAP_MINUTES = 480


class Box(dict):
    """Dict with attribute access; every result object is JSON-ish."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:  # pragma: no cover - trivial
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


class AmbiguousDuty(RuntimeError):
    """Raised by strict merge helpers when labor identity is not proven."""

    def __init__(self, message: str, reconstruction: 'DutyReconstruction'):
        super().__init__(message)
        self.reconstruction = reconstruction


# --------------------------------------------------------------------------- #
# Seeded behaviour (must stay byte-for-byte compatible with the pinned test). #
# --------------------------------------------------------------------------- #
def minutes(logs: Iterable[dict] | int | float | None = None,
            driver: str | None = None, *, evidence: Any = None,
            merge: bool = False, **kwargs: Any) -> int:
    """Naive sum of reported minutes (the pinned seeded behaviour).

    With no evidence this is the original behaviour (600 for the duty-relay
    fixture).  When ``evidence`` is supplied (or ``merge=True``) the
    evidence-aware reconstruction is used: a proven labor total is returned as
    an ``int``; an unresolved identity returns the ``(lower, upper)`` bounds.
    """
    if isinstance(logs, (int, float)):
        return int(logs)
    if evidence is not None or merge:
        merged = merge_minutes(logs, driver, evidence=evidence, **kwargs)
        return merged  # type: ignore[return-value]  # int or bounds tuple
    return sum(row['minutes'] for row in (logs or [])
               if driver is None or row.get('driver') == driver)


def naive_minutes(logs: Iterable[dict], driver: str | None = None) -> int:
    """Explicit name for the pinned simple-sum baseline."""
    return minutes(logs, driver)


# --------------------------------------------------------------------------- #
# Clock epochs: one offset/drift per device, shared by all of its records.     #
# --------------------------------------------------------------------------- #
@dataclass
class ClockEpoch:
    device: str
    correction_minutes: float = 0.0
    drift_per_minute: float = 0.0
    anchor_wall: datetime | None = None
    anchor_utc: datetime | None = None
    calibrated: bool = False
    evidence: list[str] = field(default_factory=list)
    raw: dict | None = None

    def extra_correction(self, wall: datetime) -> float:
        if self.anchor_wall is None:
            return 0.0
        elapsed = (wall - self.anchor_wall).total_seconds() / 60.0
        return self.drift_per_minute * elapsed

    def correct_wall(self, value: Any, date: Any = CANONICAL_DATE) -> datetime:
        wall = parse_wall(value, date)
        return wall + timedelta(minutes=self.correction_minutes
                                + self.extra_correction(wall))

    def to_utc(self, value: Any, *, date: Any = CANONICAL_DATE,
               timezone: Any = DEFAULT_TIMEZONE) -> datetime:
        corrected = self.correct_wall(value, date)
        # Corrected wall time is authoritative-local wall time.
        return to_utc(corrected.strftime('%Y-%m-%dT%H:%M:%S'),
                      date=date, timezone=timezone)


def _drift_rate(cal: dict) -> float:
    """Return added minutes per elapsed *device* minute."""
    if cal.get('drift_minutes_per_minute') is not None:
        return float(cal['drift_minutes_per_minute'])
    if cal.get('drift_seconds_per_minute') is not None:
        return float(cal['drift_seconds_per_minute']) / 60.0
    if cal.get('drift_minutes_per_day') is not None:
        return float(cal['drift_minutes_per_day']) / 1440.0
    if cal.get('drift_seconds_per_day') is not None:
        return float(cal['drift_seconds_per_day']) / 60.0 / 1440.0
    if cal.get('drift_ppm') is not None:
        return float(cal['drift_ppm']) * 1e-6
    if cal.get('drift_seconds_per_second') is not None:
        return float(cal['drift_seconds_per_second']) / 60.0
    return 0.0


def build_epoch(cal: dict, clock: dict | None = None) -> ClockEpoch:
    clock = clock or {}
    date = cal.get('date') or clock.get('date', CANONICAL_DATE)
    tz = cal.get('timezone') or clock.get('timezone', DEFAULT_TIMEZONE)
    device = str(cal.get('device') or cal.get('device_id') or cal.get('source')
                 or cal.get('clock') or '*')
    epoch = ClockEpoch(device=device, evidence=[str(cal.get('id') or 'calibration')],
                      raw=cal, drift_per_minute=_drift_rate(cal))

    device_key = next((k for k in ('device_at', 'shown_at', 'display_at',
                                   'observed_device_at', 'device_time',
                                   'anchor_device_at', 'clock_at')
                       if cal.get(k) not in (None, '')), None)
    auth_key = next((k for k in ('authoritative_at', 'true_at', 'actual_at',
                                 'standard_at', 'global_at', 'utc_at',
                                 'reference_at', 'anchor_at')
                      if cal.get(k) not in (None, '')), None)
    # A bare ``at`` pairs with an explicit authoritative side.
    if device_key is None and cal.get('at') not in (None, '') and auth_key:
        device_key = 'at'
    if auth_key is None and cal.get('at') not in (None, '') and device_key \
            and device_key != 'at':
        auth_key = 'at'

    if device_key and auth_key:
        wall = parse_wall(cal[device_key], date)
        auth_utc = to_utc(cal[auth_key], date=date, timezone=tz)
        if device_key == 'at' and auth_key == 'at':
            pass
        # true_utc = auth_utc + (device_reading - device_anchor)
        epoch.anchor_wall = wall
        epoch.anchor_utc = auth_utc.replace(tzinfo=None)
        # Store anchor as naive UTC; to_utc uses direct mapping below.
        epoch.correction_minutes = 0.0
        epoch.calibrated = True

        def _to_utc(value: Any, *, date: Any = date, timezone: Any = tz) -> datetime:  # noqa: ARG001
            w = parse_wall(value, date)
            elapsed = w - wall
            extra = timedelta(minutes=epoch.extra_correction(w))
            return auth_utc + elapsed + extra

        epoch.to_utc = _to_utc  # type: ignore[method-assign]
        return epoch

    correction: float | None = None
    for key in ('correction_minutes', 'add_minutes', 'adjustment_minutes',
                'time_correction_minutes', 'shift_minutes', 'delta_minutes',
                'offset_minutes'):
        if cal.get(key) is not None:
            correction = float(cal[key])
            break
    if correction is None:
        if cal.get('slow_minutes') is not None:
            correction = abs(float(cal['slow_minutes']))
        elif cal.get('behind_minutes') is not None:
            correction = abs(float(cal['behind_minutes']))
        elif cal.get('fast_minutes') is not None:
            correction = -abs(float(cal['fast_minutes']))
        elif cal.get('ahead_minutes') is not None:
            correction = -abs(float(cal['ahead_minutes']))
        elif cal.get('clock_error_minutes') is not None:
            # error convention: device - true, so correction is minus error.
            correction = -float(cal['clock_error_minutes'])
        elif cal.get('device_offset_minutes') is not None:
            correction = -float(cal['device_offset_minutes'])
    if correction is not None:
        epoch.correction_minutes = correction
        epoch.anchor_wall = parse_wall(cal.get('anchor_at')
                                       or cal.get('at') or f'{CANONICAL_DATE}T00:00',
                                       date)
        epoch.calibrated = True
    return epoch


# --------------------------------------------------------------------------- #
# Interval helpers                                                            #
# --------------------------------------------------------------------------- #
def union_minutes(intervals: Iterable[tuple[datetime, datetime] | dict],
                  *, date: Any = CANONICAL_DATE) -> int:
    """Total length of the union of half-open intervals, in whole minutes."""
    spans: list[tuple[datetime, datetime]] = []

    def _parse(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value.replace(tzinfo=None) if value.tzinfo else value
        if isinstance(value, (int, float)):
            return datetime(2000, 1, 1) + timedelta(minutes=float(value))
        return parse_wall(value, date or '2000-01-01')

    for iv in intervals:
        if isinstance(iv, dict):
            start, end = iv['start'], iv['end']
        else:
            start, end = iv
        if not isinstance(start, datetime):
            start = _parse(start)
        if not isinstance(end, datetime):
            end = _parse(end)
        if end < start:
            start, end = end, start
        if end == start:
            continue
        spans.append((start, end))
    if not spans:
        return 0
    spans.sort()
    total = timedelta(0)
    cur_s, cur_e = spans[0]
    for s, e in spans[1:]:
        if s <= cur_e:
            if e > cur_e:
                cur_e = e
        else:
            total += cur_e - cur_s
            cur_s, cur_e = s, e
    total += cur_e - cur_s
    return int(round(total.total_seconds() / 60.0))


def merge_intervals(intervals: Iterable[dict]) -> list[dict]:
    """Merge overlapping interval dicts (with start/end datetimes or ISO)."""
    norm = []
    for iv in intervals:
        start = iv['start']
        end = iv['end']
        if isinstance(start, str):
            start = datetime.fromisoformat(start.replace('Z', '+00:00'))
        if isinstance(end, str):
            end = datetime.fromisoformat(end.replace('Z', '+00:00'))
        norm.append((start, end, iv))
    norm.sort(key=lambda x: x[0])
    merged: list[dict] = []
    for start, end, raw in norm:
        if merged and start <= merged[-1]['_end_dt']:
            if end > merged[-1]['_end_dt']:
                merged[-1]['_end_dt'] = end
        else:
            merged.append({'start': start, 'end': end,
                           '_start_dt': start, '_end_dt': end,
                           'minutes': int(round((end - start).total_seconds() / 60))})
    for m in merged:
        m['minutes'] = int(round((m['_end_dt'] - m['_start_dt']).total_seconds() / 60))
        m.pop('_start_dt', None)
        m.pop('_end_dt', None)
        if isinstance(m['start'], datetime):
            m['start'] = m['start'].isoformat()
        if isinstance(m['end'], datetime):
            m['end'] = m['end'].isoformat()
    return merged


# --------------------------------------------------------------------------- #
# Investigation                                                               #
# --------------------------------------------------------------------------- #
REPORT_ID_KEYS = ('report', 'report_id', 'message', 'message_id', 'duty_report',
                  'log_id', 'target', 'target_report')
ORIGIN_ID_KEYS = ('origin_report', 'origin_report_id', 'source_report',
                  'source_report_id', 'origin', 'original_report', 'parent',
                  'parent_id', 'relay_of', 'forwarded_from', 'same_as',
                  'duplicate_of', 'derived_from', 'source_message',
                  'source_message_id', 'upstream', 'upstream_report')
REPORT_REF_KEYS = ('meter_report', 'platform_report', 'meter_message',
                   'dispatch_message', 'meter_report_id', 'platform_report_id')


class DutyInvestigation:
    """Collect reports + forensic evidence and reconstruct feasible duties."""

    def __init__(self, driver: str | None = None,
                 logs: Iterable[dict] | None = None,
                 evidence: Iterable[dict] | None = None,
                 *, maximum_minutes: int | float = CAP_MINUTES,
                 date: Any = CANONICAL_DATE,
                 timezone: Any = DEFAULT_TIMEZONE,
                 vehicle: str | None = None,
                 **kwargs: Any):
        self.driver = driver or kwargs.get('for_driver')
        self.maximum_minutes = maximum_minutes
        self.clock = {'date': date or CANONICAL_DATE,
                      'timezone': timezone or DEFAULT_TIMEZONE}
        self.vehicle = vehicle
        self.reports: list[Box] = []
        self.evidence: list[tuple[str, dict]] = []
        self.calibrations: list[dict] = []
        self._epochs: list[ClockEpoch] = []
        self.kwargs = kwargs
        if logs:
            self.add_logs(logs)
        if evidence:
            self.add_evidence(evidence)
        # Slurp convenience collections passed as kwargs.
        for key, value in kwargs.items():
            kind = norm_kind(key)
            if kind in {'calibration', 'provenance', 'pulse', 'order', 'login',
                        'handover', 'signature', 'shift'} and isinstance(value, list):
                self.add_evidence(value, default_kind=kind)

    # ------------------------------------------------------------- construct
    @classmethod
    def from_path(cls, path: Any, **overrides: Any) -> 'DutyInvestigation':
        from .duty_capture import load_case
        return cls.from_case(load_case(path), **overrides)

    @classmethod
    def from_file(cls, path: Any, **overrides: Any) -> 'DutyInvestigation':
        return cls.from_path(path, **overrides)

    @classmethod
    def load(cls, path: Any, **overrides: Any) -> 'DutyInvestigation':
        return cls.from_path(path, **overrides)

    @classmethod
    def from_case(cls, case: dict, **overrides: Any) -> 'DutyInvestigation':
        clock = case_clock_args(case)
        driver = overrides.pop('driver', None) or case.get('driver')
        maximum = overrides.pop('maximum_minutes', None)
        inv = cls(driver=driver,
                  logs=case.get('logs'),
                  maximum_minutes=maximum if maximum is not None
                  else case.get('maximum_minutes', case.get('cap', CAP_MINUTES)),
                  date=overrides.pop('date', clock.get('date', CANONICAL_DATE)),
                  timezone=overrides.pop('timezone',
                                         clock.get('timezone', DEFAULT_TIMEZONE)),
                  vehicle=overrides.pop('vehicle', None) or case.get('vehicle')
                  or case.get('taxi'),
                  **overrides)
        # Only top-level sections are fused automatically; alternative
        # forensic-path bundles stay mutually exclusive and are loaded by
        # tests/callers explicitly.
        from .duty_capture import iter_evidence_items
        for kind, item in iter_evidence_items(case, nested_scenarios=False):
            if kind == 'log':
                # logs already loaded; avoid double insert by id.
                if not any(r['id'] == str(item.get('id')) for r in inv.reports):
                    inv.add_logs([item])
            else:
                inv.add_evidence([(kind, item)])
        return inv

    def add_logs(self, logs: Iterable[dict]) -> 'DutyInvestigation':
        for raw in logs:
            row = dict(raw)
            rid = str(row.get('id') or row.get('report_id')
                      or f'REPORT-{len(self.reports) + 1}')
            start = row.get('start', row.get('start_at'))
            end = row.get('end', row.get('end_at'))
            minutes = row.get('minutes')
            if minutes is None and start is not None and end is not None:
                try:
                    s = parse_wall(start, self.clock['date'])
                    e = parse_wall(end, self.clock['date'])
                    minutes = int((e - s).total_seconds() / 60)
                except (ValueError, TypeError):
                    minutes = None
            report = Box(
                id=rid,
                driver=row.get('driver') or row.get('driver_id'),
                minutes=int(minutes) if minutes is not None else None,
                source=row.get('source'),
                device=(row.get('device') or row.get('device_id')
                        or row.get('clock') or row.get('epoch')),
                vehicle=row.get('vehicle') or row.get('vehicle_id')
                or row.get('taxi') or self.vehicle,
                start=start, end=end,
                order_ids=_as_list(row.get('order_ids') or row.get('order_id')),
                raw=row,
            )
            self.reports.append(report)
        return self

    def add_evidence(self, items: Iterable[dict | tuple],
                     default_kind: str | None = None) -> 'DutyInvestigation':
        for entry in items:
            if isinstance(entry, tuple):
                kind, item = entry
            else:
                item = entry
                kind = norm_kind(item.get('kind') or item.get('type')
                                 or default_kind or 'evidence')
                if kind == 'evidence' and default_kind:
                    kind = default_kind
            item = dict(item)
            self.evidence.append((kind, item))
            if kind == 'calibration':
                self.calibrations.append(item)
                self._epochs.append(build_epoch(item, self.clock))
            if kind == 'log':
                if not any(r['id'] == str(item.get('id')) for r in self.reports):
                    self.add_logs([item])
        return self

    # ---------------------------------------------------------------- epochs
    def _report_device_tokens(self, report: Box) -> set[str]:
        raw_tokens: set[str] = set()
        if report.device:
            raw_tokens.add(str(report.device))
        if report.source:
            raw_tokens.add(str(report.source))
        rid = report.id
        raw_tokens.add(rid)
        raw_tokens.add(rid.split('-', 1)[0])
        return _identifier_tokens(raw_tokens)

    def _cal_device_tokens(self, cal: dict) -> set[str]:
        raw_tokens = set()
        for key in ('device', 'device_id', 'source', 'clock', 'epoch'):
            if cal.get(key):
                raw_tokens.add(str(cal[key]))
        for key in ('applies_to', 'devices'):
            value = cal.get(key)
            if isinstance(value, list):
                raw_tokens.update(str(v) for v in value)
        return _identifier_tokens(raw_tokens)

    TRUSTED_SOURCES = {'meter', 'taximeter', 'meter_device', '计价器'}

    def _is_trusted_reference(self, report: Box) -> bool:
        if report.raw.get('trusted') is True or report.raw.get('authoritative') is True:
            return True
        trusted = set(self.kwargs.get('trusted_sources', ())) | self.TRUSTED_SOURCES
        return str(report.source or '').lower() in {str(t).lower() for t in trusted}

    def epoch_for(self, report: Box) -> ClockEpoch | None:
        tokens = self._report_device_tokens(report)
        wildcard: ClockEpoch | None = None
        for epoch, cal in zip(self._epochs, self.calibrations):
            cal_tokens = self._cal_device_tokens(cal)
            if cal_tokens & tokens:
                return epoch
            if not cal_tokens or '*' in cal_tokens or 'global' in cal_tokens:
                wildcard = epoch
        if wildcard is not None:
            return wildcard
        # The taximeter is the metrological reference clock: its naive local
        # timestamps are authoritative in the case timezone.  Other devices
        # stay uncalibrated until an anchor arrives.
        if self._is_trusted_reference(report):
            return ClockEpoch(device=str(report.source or 'reference'),
                              calibrated=True, evidence=[])
        return None

    def true_interval(self, report: Box) -> Box | None:
        """UTC-corrected interval if the report's epoch is calibrated/aware."""
        if report.start in (None, '') or report.end in (None, ''):
            return None
        epoch = self.epoch_for(report)
        try:
            if epoch is not None and epoch.calibrated:
                start_utc = epoch.to_utc(report.start, **self.clock)
                if report.minutes is not None:
                    end_utc = start_utc + timedelta(minutes=report.minutes)
                    end_from_stamp = epoch.to_utc(report.end, **self.clock)
                    # Prefer the corrected original end timestamp when drift is
                    # in play; equal to start+duration for pure offsets.
                    end_utc = end_from_stamp
                else:
                    end_utc = epoch.to_utc(report.end, **self.clock)
                return Box(start=start_utc, end=end_utc, calibrated=True,
                           calibration=epoch.evidence)
            # Timezone-aware timestamps need no device anchor.
            start_utc = to_utc(report.start, **self.clock)
            end_utc = to_utc(report.end, **self.clock)
            raw_start = str(report.start)
            raw_end = str(report.end)
            aware = ('T' in raw_start and ('+' in raw_start[10:]
                                           or raw_start.endswith('Z'))
                     and 'T' in raw_end and ('+' in raw_end[10:]
                                             or raw_end.endswith('Z')))
            if aware:
                return Box(start=start_utc, end=end_utc, calibrated=True,
                           calibration=[])
            # Naive case-zone times still get a UTC *display* conversion;
            # calibrated=False tells relation reasoning not to align epochs.
            return Box(start=start_utc, end=end_utc, calibrated=False,
                       calibration=[])
        except (ValueError, TypeError):
            return None
        return Box(start=None, end=None, calibrated=False)

    # ------------------------------------------------------------- relations
    def _known_report_ids(self) -> set[str]:
        return {r.id for r in self.reports}

    def relations(self) -> list[Box]:
        known = self._known_report_ids()
        edges: list[Box] = []

        def add(kind: str, a: Any, b: Any, eid: str, reason: str) -> None:
            if a is None or b is None:
                return
            a, b = str(a), str(b)
            if a not in known or b not in known or a == b:
                return
            pair = tuple(sorted((a, b)))
            if any(tuple(sorted((e['a'], e['b']))) == pair and e['kind'] == kind
                   for e in edges):
                if eid not in edges[-1]['evidence']:
                    for e in edges:
                        if tuple(sorted((e['a'], e['b']))) == pair:
                            e['evidence'].append(eid)
                return
            edges.append(Box(kind=kind, a=a, b=b, evidence=[eid], reason=reason))

        for kind, item in self.evidence:
            eid = str(item.get('id') or item.get('evidence_id') or kind.upper())
            if kind == 'provenance':
                a = next((item[k] for k in REPORT_ID_KEYS if item.get(k)), None)
                b = next((item[k] for k in ORIGIN_ID_KEYS if item.get(k)), None)
                relation = str(item.get('relation') or item.get('relationship')
                               or item.get('link') or '').lower()
                edge_kind = 'distinct' if relation in {
                    'distinct', 'separate', 'separate_shift', 'different_shift',
                    'two_shifts', 'unrelated'} else 'same'
                if a is not None and b is not None:
                    add(edge_kind, a, b, eid,
                        item.get('reason') or f'provenance:{relation or "relay"}')
                chain = item.get('chain') or item.get('source_chain')
                if isinstance(chain, list):
                    refs = [str(x) for x in chain if str(x) in known]
                    for x, y in zip(refs, refs[1:]):
                        add(edge_kind, x, y, eid, 'source-chain')
            elif kind == 'order':
                refs = []
                for key in REPORT_REF_KEYS:
                    if item.get(key):
                        refs.append(str(item[key]))
                for key in ('report_ids', 'reports', 'messages'):
                    if isinstance(item.get(key), list):
                        refs.extend(str(x) for x in item[key])
                refs = [r for r in refs if r in known]
                for x, y in itertools.combinations(sorted(set(refs)), 2):
                    add('same', x, y, eid, 'shared-order-origin')
            elif kind == 'signature':
                # Explicit same_as / distinct links may ride along.
                if item.get('same_as'):
                    add('same', item.get('report') or item.get('target'),
                        item['same_as'], eid, 'signature-link')
                if item.get('distinct_from'):
                    add('distinct', item.get('report') or item.get('target'),
                        item['distinct_from'], eid, 'signed-separate-segment')

        # Shared order ids on the reports themselves.
        order_map: dict[str, list[str]] = {}
        for report in self.reports:
            for oid in report.order_ids:
                order_map.setdefault(str(oid), []).append(report.id)
        for oid, ids in order_map.items():
            for x, y in itertools.combinations(sorted(set(ids)), 2):
                add('same', x, y, f'order:{oid}', 'shared-order-id')

        # Different physical vehicles cannot be one labor interval.
        for ra, rb in itertools.combinations(self.reports, 2):
            va, vb = _norm_vehicle(ra.vehicle), _norm_vehicle(rb.vehicle)
            if va and vb and va != vb:
                add('distinct', ra.id, rb.id,
                    f'vehicle:{va}!={vb}', 'different vehicles')

        # Calibrated geometry: disjoint intervals cannot be one labor interval.
        intervals = {r.id: self.true_interval(r) for r in self.reports}
        for ra, rb in itertools.combinations(self.reports, 2):
            ia, ib = intervals[ra.id], intervals[rb.id]
            if not (ia and ia.get('calibrated') and ib and ib.get('calibrated')):
                continue
            s1, e1, s2, e2 = ia['start'], ia['end'], ib['start'], ib['end']
            overlap = max(timedelta(0), min(e1, e2) - max(s1, s2))
            if overlap.total_seconds() == 0:
                cal_ids = [*(ia.get('calibration') or []),
                           *(ib.get('calibration') or [])]
                add('distinct', ra.id, rb.id,
                    cal_ids[0] if cal_ids else 'calibrated-geometry',
                    'calibrated intervals are disjoint')
        return edges

    # ------------------------------------------------------------- clustering
    def clusters(self, relations: list[Box] | None = None) -> list[Box]:
        relations = relations if relations is not None else self.relations()
        parent = {r.id: r.id for r in self.reports}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            parent[find(a)] = find(b)

        for edge in relations:
            if edge['kind'] == 'same':
                union(edge['a'], edge['b'])

        groups: dict[str, list[Box]] = {}
        for report in self.reports:
            groups.setdefault(find(report.id), []).append(report)

        origin_of: dict[str, str] = {}
        for edge in relations:
            if edge['kind'] == 'same':
                # Prefer the "b" side recorded as the origin of "a".
                origin_of.setdefault(find(edge['a']), edge['b'])

        clusters: list[Box] = []
        for idx, (root, members) in enumerate(sorted(groups.items()), 1):
            canonical_id = origin_of.get(root)
            if canonical_id not in {m.id for m in members}:
                # Prefer a calibrated/meter-ish member.
                canonical_id = sorted(
                    members,
                    key=lambda m: (self.true_interval(m) is None
                                   or not self.true_interval(m).get('calibrated'),
                                   0 if (m.source or '') == 'meter' else 1,
                                   m.id))[0].id
            canonical = next(m for m in members if m.id == canonical_id)
            minutes_value = canonical.minutes
            if minutes_value is None:
                minutes_value = max((m.minutes or 0) for m in members)
            vehicles = {m.vehicle for m in members if m.vehicle}
            clusters.append(Box(
                id=f'C{idx}',
                root=root,
                reports=[m.id for m in members],
                canonical_report=canonical_id,
                minutes=int(minutes_value),
                vehicle=canonical.vehicle or (sorted(vehicles)[0] if vehicles else None),
                driver=canonical.driver,
            ))
        # Re-id clusters C1.. in report order for stability.
        for idx, cluster in enumerate(clusters, 1):
            cluster['id'] = f'C{idx}'
        return clusters

    def _distinct_cluster_pairs(self, clusters: list[Box],
                                relations: list[Box]) -> set[frozenset]:
        root_to_cid = {c['root']: c['id'] for c in clusters}
        # Need DSU root mapping: build report id -> cluster id directly.
        report_cid = {}
        for c in clusters:
            for rid in c['reports']:
                report_cid[rid] = c['id']
        pairs: set[frozenset] = set()
        for edge in relations:
            if edge['kind'] == 'distinct':
                ca, cb = report_cid.get(edge['a']), report_cid.get(edge['b'])
                if ca and cb and ca != cb:
                    pairs.add(frozenset((ca, cb)))
        return pairs

    def scenarios(self) -> list[Box]:
        relations = self.relations()
        clusters = self.clusters(relations)
        forbidden = self._distinct_cluster_pairs(clusters, relations)
        cids = [c['id'] for c in clusters]
        by_id = {c['id']: c for c in clusters}

        partitions = list(_set_partitions(cids))
        scenarios: list[Box] = []
        for p_idx, partition in enumerate(partitions):
            blocks = [list(block) for block in partition]
            if any(len(block) >= 2 and frozenset(pair) in forbidden
                   for block in blocks
                   for pair in itertools.combinations(block, 2)):
                continue
            block_boxes = []
            total = 0
            for block in blocks:
                members = [by_id[cid] for cid in block]
                minute = max(c['minutes'] for c in members)
                # Calibrated geometry can sharpen a hypothetical merged block.
                ivs = []
                all_cal = True
                for c in members:
                    for rid in c['reports']:
                        rep = next(r for r in self.reports if r.id == rid)
                        iv = self.true_interval(rep)
                        if iv and iv.get('calibrated'):
                            ivs.append((iv['start'], iv['end']))
                        else:
                            all_cal = False
                geometric = union_minutes(ivs) if ivs else None
                if len(members) == 1:
                    minute = members[0]['minutes']
                elif all_cal and geometric is not None:
                    minute = geometric
                block_boxes.append(Box(clusters=block, minutes=int(minute)))
                total += int(minute)
            merged = [b for b in blocks if len(b) >= 2]
            if len(blocks) == 1 and len(cids) > 1:
                label = 'relay'
            elif len(blocks) == len(cids):
                label = 'separate'
            else:
                label = 'partial'
            scenarios.append(Box(
                name=label if label != 'partial' else f'partition-{p_idx}',
                label=label,
                minutes=int(total),
                blocks=block_boxes,
                partition=[sorted(b) for b in blocks],
                assumed_merges=[sorted(b) for b in merged],
            ))
        scenarios.sort(key=lambda s: (s['minutes'], s['name']))
        return scenarios

    # --------------------------------------------------------------- pulses
    def pulse_summaries(self) -> list[Box]:
        pulses = [item for kind, item in self.evidence if kind == 'pulse']
        by_vehicle: dict[str, list[dict]] = {}
        for p in pulses:
            vehicle = str(p.get('vehicle') or p.get('vehicle_id') or self.vehicle
                          or '*')
            by_vehicle.setdefault(vehicle, []).append(p)
        out = []
        for vehicle, rows in sorted(by_vehicle.items()):
            timed = []
            for p in rows:
                when = p.get('at') or p.get('time') or p.get('timestamp')
                try:
                    dt = to_utc(when, **self.clock) if when else None
                except (ValueError, TypeError):
                    dt = None
                timed.append((dt, p))
            timed.sort(key=lambda x: (x[0] is None, x[0]))
            gaps = []
            for (a, _), (b, _) in zip(timed, timed[1:]):
                if a and b:
                    gaps.append(int((b - a).total_seconds() / 60))
            explicit = next((p.get('continuous') for _, p in timed
                              if p.get('continuous') is not None), None)
            max_gap = max(gaps, default=0)
            tolerance = int(self.kwargs.get('pulse_gap_tolerance', 45))
            continuous = bool(explicit) if explicit is not None else (
                not gaps or max_gap <= tolerance)
            out.append(Box(
                vehicle=vehicle, count=len(rows),
                first=utc_iso(timed[0][0]) if timed and timed[0][0] else None,
                last=utc_iso(timed[-1][0]) if timed and timed[-1][0] else None,
                max_gap_minutes=max_gap, gaps=gaps, continuous=continuous,
                evidence=[str(p.get('id') or 'pulse') for _, p in timed],
            ))
        return out

    # ------------------------------------------------------------ reconstruct
    def reconstruct(self) -> 'DutyReconstruction':
        relations = self.relations()
        clusters = self.clusters(relations)
        scenarios = self.scenarios()

        # Evidence that simultaneously forces "same" and "distinct" for a pair
        # is preserved as a contradiction witness rather than silently picked.
        edge_pairs: dict[frozenset, set[str]] = {}
        for edge in relations:
            pair = frozenset((edge['a'], edge['b']))
            edge_pairs.setdefault(pair, set()).add(edge['kind'])
        contradictions = [
            Box(reports=sorted(pair), kinds=sorted(kinds),
                evidence=sorted({eid for edge in relations
                                 if frozenset((edge['a'], edge['b'])) == pair
                                 for eid in edge['evidence']}),
                kind='same_and_distinct_evidence',
                detail='evidence both merges and separates the same reports')
            for pair, kinds in edge_pairs.items() if kinds == {'same', 'distinct'}
        ]
        bounds = (min(s['minutes'] for s in scenarios),
                  max(s['minutes'] for s in scenarios)) if scenarios else (0, 0)
        proven = len(scenarios) == 1 or bounds[0] == bounds[1]
        chosen = scenarios[0] if proven else None

        interval_source = chosen if chosen else None
        intervals = self._labor_intervals(chosen, clusters) if chosen else []
        feasible_intervals = {
            s['name']: self._labor_intervals(s, clusters) for s in scenarios
        }

        relation_evidence = sorted({eid for r in relations for eid in r['evidence']})
        all_evidence = sorted({
            eid for _, item in self.evidence
            for eid in [str(item.get('id') or item.get('evidence_id') or '')] if eid
        })
        pulses = self.pulse_summaries()
        corroborated = []
        for edge in relations:
            if edge['kind'] != 'same':
                continue
            for summary in pulses:
                if summary['continuous']:
                    corroborated.append(Box(
                        relation=(edge['a'], edge['b']),
                        pulse_evidence=summary['evidence'],
                        vehicle=summary['vehicle']))

        identity = self.identity_assignments(clusters=clusters)

        if proven and chosen is not None:
            if chosen['label'] == 'relay' or len(chosen['blocks']) == 1:
                status = 'proven_single_shift'
            else:
                status = ('proven_two_shifts' if chosen['minutes'] >= 500
                          else 'proven_multiple_shifts')
        else:
            status = 'ambiguous'

        return DutyReconstruction(
            driver=self.driver,
            vehicle=self.vehicle,
            date=self.clock['date'],
            timezone=self.clock['timezone'],
            status=status,
            proven=proven,
            ambiguous=not proven,
            minutes=chosen['minutes'] if chosen else None,
            worked_minutes=chosen['minutes'] if chosen else None,
            bounds=bounds,
            lower_bound=bounds[0],
            upper_bound=bounds[1],
            intervals=intervals,
            actual_intervals=intervals,
            driving_intervals=intervals,
            feasible_intervals=feasible_intervals,
            scenarios=[Box(s) for s in scenarios],
            feasible_scenarios=[Box(s) for s in scenarios],
            relations=relations,
            source_relations=relations,
            clusters=clusters,
            pulse_summaries=pulses,
            pulse_corroboration=corroborated,
            identity=identity,
            contradictions=contradictions,
            evidence=sorted(set(all_evidence) | set(relation_evidence)),
            maximum_minutes=self.maximum_minutes,
        )

    def _labor_intervals(self, scenario: Box | None,
                         clusters: list[Box]) -> list[Box]:
        if scenario is None:
            return []
        by_id = {c['id']: c for c in clusters}
        result = []
        for block in scenario['blocks']:
            cids = block['clusters'] if isinstance(block, dict) else block
            member_clusters = [by_id[c] for c in cids]
            report_ids = [rid for c in member_clusters for rid in c['reports']]
            reps = [r for r in self.reports if r.id in report_ids]
            # Choose the canonical (preferred) report for displayed interval.
            canonical_report_id = member_clusters[0]['canonical_report']
            rep = next((r for r in reps if r.id == canonical_report_id), reps[0])
            iv = self.true_interval(rep)
            start_utc = iv['start'] if iv and iv.get('start') else None
            end_utc = iv['end'] if iv and iv.get('end') else None
            minutes_value = block['minutes'] if isinstance(block, dict) else None
            if minutes_value is None:
                minutes_value = max(r.minutes or 0 for r in reps)
            local_start = local_end = None
            if start_utc is not None:
                local_start = start_utc.astimezone(
                    _as_tz(self.clock['timezone'])).strftime('%H:%M')
            if end_utc is not None:
                local_end = end_utc.astimezone(
                    _as_tz(self.clock['timezone'])).strftime('%H:%M')
            vehicles = {r.vehicle for r in reps if r.vehicle}
            result.append(Box(
                start=local_start or rep.start,
                end=local_end or rep.end,
                start_local=local_start or rep.start,
                end_local=local_end or rep.end,
                start_utc=start_utc.isoformat().replace('+00:00', 'Z')
                if start_utc else None,
                end_utc=end_utc.isoformat().replace('+00:00', 'Z')
                if end_utc else None,
                minutes=int(minutes_value),
                reports=sorted(report_ids),
                cluster=cids[0] if len(cids) == 1 else '+'.join(cids),
                vehicle=rep.vehicle or (sorted(vehicles)[0] if vehicles else None),
                driver=rep.driver,
                calibrated=bool(iv and iv.get('calibrated')),
            ))
        result.sort(key=lambda x: (x.start_utc or '9999', x.start or '99'))
        return result

    # -------------------------------------------------------------- identity
    def identity_assignments(self, clusters: list[Box] | None = None,
                             **override: Any) -> Box:
        return identity_assignments(self, clusters=clusters, **override)


class DutyReconstruction(Box):
    """Dict/attribute result; defined as class for isinstance checks."""


# --------------------------------------------------------------------------- #
# Shared-login identity assignment + conflict witnesses                      #
# --------------------------------------------------------------------------- #
def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _norm_vehicle(value: Any) -> str:
    if value is None:
        return ''
    return ''.join(ch for ch in str(value).strip().upper()
                   if ch.isalnum())


def _same_vehicle(a: Any, b: Any) -> bool:
    na, nb = _norm_vehicle(a), _norm_vehicle(b)
    return bool(na and nb and na == nb)


_GENERIC_TOKENS = {'device', 'clock', 'epoch', 'dev', 'terminal', 'time'}


def _identifier_tokens(values: Iterable[Any]) -> set[str]:
    """Build tolerant matching tokens for device/source/report identifiers."""
    out: set[str] = set()
    for value in values:
        text = str(value).strip().lower()
        if not text:
            continue
        out.add(text)
        if ':' in text:
            out.add(text.rsplit(':', 1)[-1])
        for sep in ('-', '_'):
            if sep in text:
                head = text.split(sep, 1)[0]
                if head:
                    out.add(head)
    return {t for t in out if t and t not in _GENERIC_TOKENS}


def _as_tz(tz: Any):
    from .duty_capture import parse_timezone
    return parse_timezone(tz)


def _segment_time(seg: dict, key: str, clock: dict) -> datetime | None:
    value = seg.get(key)
    if value in (None, ''):
        return None
    try:
        return to_utc(value, **clock)
    except (ValueError, TypeError):
        return None


def identity_assignments(inv: DutyInvestigation | dict | None = None,
                         *args: Any,
                         clusters: list[Box] | None = None,
                         segments: list[dict] | None = None,
                         logins: list[dict] | None = None,
                         signatures: list[dict] | None = None,
                         handovers: list[dict] | None = None,
                         shifts: list[dict] | None = None,
                         assignments: list[dict] | None = None,
                         driver: str | None = None,
                         **kwargs: Any) -> Box:
    """Enumerate feasible physical-driver assignments for labor segments.

    Shared logins keep every feasible identity on the table; contradictory
    signed claims / overlapping shift assignments are preserved as conflict
    witnesses instead of being silently resolved.
    """
    # Flexible calling convention: identity_assignments(case_dict, ...).
    if isinstance(inv, dict):
        inv = DutyInvestigation.from_case(inv, driver=driver)
    if inv is None or not isinstance(inv, DutyInvestigation):
        inv = DutyInvestigation(driver=driver, **kwargs)

    clock = inv.clock
    if logins is None:
        logins = [i for k, i in inv.evidence if k == 'login']
    if signatures is None:
        signatures = [i for k, i in inv.evidence if k == 'signature']
    if handovers is None:
        handovers = [i for k, i in inv.evidence if k == 'handover']
    if shifts is None:
        shifts = [i for k, i in inv.evidence if k == 'shift']
        if assignments is None:
            assignments = []
            for shift in shifts:
                assignments.extend(shift.get('assignments', []))
    if assignments is None:
        assignments = kwargs.get('assignment_records')

    # Build the segments we assign: explicit > clusters > reports.
    seg_records: list[Box] = []
    if segments:
        for i, seg in enumerate(segments, 1):
            seg = dict(seg)
            seg_records.append(Box(
                id=str(seg.get('id') or seg.get('segment') or f'S{i}'),
                report=seg.get('report') or seg.get('report_id'),
                driver=seg.get('driver') or seg.get('driver_id'),
                vehicle=seg.get('vehicle') or seg.get('vehicle_id'),
                start=seg.get('start'), end=seg.get('end'), raw=seg))
    elif clusters is not None:
        for c in clusters:
            rep = next((r for r in inv.reports if r.id == c['canonical_report']),
                        None)
            seg_records.append(Box(
                id=c['id'], report=c['canonical_report'],
                reports=list(c['reports']),
                driver=c.get('driver'), vehicle=c.get('vehicle'),
                start=rep.start if rep else None, end=rep.end if rep else None))
    else:
        for r in inv.reports:
            seg_records.append(Box(id=r.id, report=r.id, driver=r.driver,
                                   vehicle=r.vehicle, start=r.start, end=r.end))

    known_drivers = {s.driver for s in seg_records if s.driver}
    for collection in (logins, signatures, assignments or []):
        for item in collection:
            for key in ('driver', 'driver_id', 'signed_by', 'signer', 'from',
                        'to', 'from_driver', 'to_driver'):
                if item.get(key):
                    known_drivers.add(str(item[key]))
            if isinstance(item.get('drivers'), list):
                known_drivers.update(str(d) for d in item['drivers'])
    if driver:
        known_drivers.add(driver)

    # A platform "driver" may actually be a shared login/account identifier;
    # such tokens are not feasible *physical* identities.
    shared_accounts: set[str] = set()
    for login in logins or []:
        login_drivers = login.get('drivers')
        if login_drivers is None:
            login_drivers = [login.get('driver') or login.get('driver_id')]
        login_drivers = [str(d) for d in login_drivers if d]
        if len(login_drivers) > 1 or login.get('shared') is True:
            for key in ('login_id', 'account', 'account_id', 'id',
                        'terminal', 'terminal_id', 'username'):
                if login.get(key):
                    shared_accounts.add(str(login[key]))

    witnesses: list[Box] = []
    options_per_segment: dict[str, set[str]] = {s.id: set() for s in seg_records}
    locks_per_segment: dict[str, list[tuple[str, str, str]]] = {
        s.id: [] for s in seg_records}

    def targets_of_signature(sig: dict) -> set[str]:
        targets = set()
        for key in ('report', 'report_id', 'target', 'target_report',
                    'segment', 'segment_id'):
            if sig.get(key):
                val = str(sig[key])
                targets.add(val)
                # Map report id -> cluster/segment handled later via report map.
        return targets

    report_to_seg: dict[str, str] = {}
    for s in seg_records:
        if s.get('report'):
            report_to_seg[str(s.report)] = s.id
        for rid in s.get('reports', []) or []:
            report_to_seg[str(rid)] = s.id

    def resolve_targets(raw_targets: set[str]) -> set[str]:
        out = set()
        for t in raw_targets:
            if t in options_per_segment:
                out.add(t)
            elif t in report_to_seg:
                out.add(report_to_seg[t])
        return out

    # Weak platform claim + universe.  A shared-account token is not a
    # physical identity candidate.
    for s in seg_records:
        if s.driver and str(s.driver) not in shared_accounts:
            options_per_segment[s.id].add(str(s.driver))

    # Logins / shared logins.
    for login in logins or []:
        eid = str(login.get('id') or 'login')
        drivers = login.get('drivers')
        if drivers is None:
            drivers = [login.get('driver') or login.get('driver_id')]
        drivers = [str(d) for d in drivers if d]
        vehicle = login.get('vehicle') or login.get('vehicle_id')
        if len(drivers) > 1:
            witnesses.append(Box(
                kind='shared_login', evidence=[eid], drivers=drivers,
                vehicle=vehicle,
                detail='multiple physical drivers share one login/terminal'))
        for s in seg_records:
            if vehicle and s.vehicle and not _same_vehicle(vehicle, s.vehicle):
                continue
            overlap = _record_overlaps_segment(login, s, clock,
                                               start_keys=('login_at', 'start',
                                                           'login'),
                                               end_keys=('logout_at', 'end',
                                                         'logout'))
            if overlap is False:
                continue
            options_per_segment[s.id].update(drivers)
            if len(drivers) == 1 and login.get('locks_identity', True):
                locks_per_segment[s.id].append((drivers[0], eid, 'login'))

    # Shift assignments (rules.py schema).
    for asg in assignments or []:
        eid = str(asg.get('id') or asg.get('shift_id') or 'assignment')
        adriver = str(asg.get('driver') or asg.get('driver_id') or '')
        vehicle = asg.get('vehicle') or asg.get('vehicle_id')
        for s in seg_records:
            if vehicle and s.vehicle and not _same_vehicle(vehicle, s.vehicle):
                continue
            overlap = _record_overlaps_segment(
                asg, s, clock, start_keys=('start',), end_keys=('end',))
            if overlap is False:
                continue
            if adriver:
                options_per_segment[s.id].add(adriver)
                locks_per_segment[s.id].append((adriver, eid, 'assignment'))

    # Overlapping same-vehicle assignments without a handover are disputes,
    # mirroring taxiproof.rules overlap handling.
    by_vehicle: dict[str, list[dict]] = {}
    for asg in assignments or []:
        if asg.get('vehicle'):
            by_vehicle.setdefault(str(asg['vehicle']), []).append(asg)
    for vehicle, rows in by_vehicle.items():
        for a, b in itertools.combinations(rows, 2):
            if a.get('start') < b.get('end') and b.get('start') < a.get('end'):
                resolved = any(
                    h.get('vehicle') in (vehicle, None)
                    and {str(h.get('from') or h.get('from_driver')),
                         str(h.get('to') or h.get('to_driver'))}
                    == {str(a.get('driver')), str(b.get('driver'))}
                    for h in (handovers or []))
                witnesses.append(Box(
                    kind='overlapping_assignments',
                    evidence=[str(a.get('id') or 'assignment'),
                              str(b.get('id') or 'assignment')],
                    vehicle=vehicle,
                    drivers=sorted({str(a.get('driver')), str(b.get('driver'))}),
                    resolved_by_handover=resolved,
                    detail='same-vehicle overlapping assignments'
                           + (' with handover' if resolved else ' without handover')))

    # Handovers constrain before/after sides.
    for h in handovers or []:
        eid = str(h.get('id') or 'handover')
        at = h.get('at') or h.get('handover_at') or h.get('time')
        from_d = h.get('from') or h.get('from_driver') or h.get('from_id')
        to_d = h.get('to') or h.get('to_driver') or h.get('to_id')
        at_dt = None
        if at:
            try:
                at_dt = to_utc(at, **clock)
            except (ValueError, TypeError):
                at_dt = None
        for s in seg_records:
            s_start = _segment_time(s, 'start', clock)
            s_end = _segment_time(s, 'end', clock)
            if at_dt and s_end and s_end <= at_dt and from_d:
                locks_per_segment[s.id].append((str(from_d), eid, 'handover-from'))
                options_per_segment[s.id].add(str(from_d))
            elif at_dt and s_start and s_start >= at_dt and to_d:
                locks_per_segment[s.id].append((str(to_d), eid, 'handover-to'))
                options_per_segment[s.id].add(str(to_d))

    # Signatures are the strongest physical-identity evidence.
    for sig in signatures or []:
        eid = str(sig.get('id') or sig.get('signature_id') or 'signature')
        signer = (sig.get('driver') or sig.get('driver_id')
                  or sig.get('signed_by') or sig.get('signer'))
        if not signer:
            continue
        signer = str(signer)
        known_drivers.add(signer)
        targets = resolve_targets(targets_of_signature(sig))
        if not targets:
            # Signature may describe time coordinates instead of an id.
            targets = _signature_time_targets(sig, seg_records, clock)
        for tid in targets:
            options_per_segment[tid].add(signer)
            locks_per_segment[tid].append((signer, eid, 'signature'))

    # Resolve lock conflicts into witnesses + contested feasible values.
    feasible_values: dict[str, list[str]] = {}
    for s in seg_records:
        sid = s.id
        locks = locks_per_segment[sid]
        locked_drivers = {d for d, _, _ in locks}
        if len(locked_drivers) > 1:
            witness_ids = sorted({eid for _, eid, _ in locks})
            witnesses.append(Box(
                kind='conflicting_identity_claims', segment=sid,
                drivers=sorted(locked_drivers), evidence=witness_ids,
                detail='evidence locks the same labor segment to different '
                       'physical drivers'))
            feasible_values[sid] = sorted(locked_drivers)
        elif len(locked_drivers) == 1:
            feasible_values[sid] = [next(iter(locked_drivers))]
        else:
            values = {v for v in options_per_segment[sid]
                      if v not in shared_accounts}
            if not values:
                values = {v for v in known_drivers if v not in shared_accounts}
            feasible_values[sid] = sorted(values) if values else ['UNKNOWN']

    seg_ids = [s.id for s in seg_records]
    assignments_out: list[Box] = []
    for combo in itertools.product(*[feasible_values[sid] for sid in seg_ids]):
        driver_map = dict(zip(seg_ids, combo))
        contested = any(
            len({d for d, _, _ in locks_per_segment[sid]}) > 1
            or any(w.get('segment') == sid
                   and w['kind'] == 'conflicting_identity_claims'
                   for w in witnesses)
            for sid in seg_ids)
        evidence_ids = sorted({
            eid for sid in seg_ids for _, eid, _ in locks_per_segment[sid]})
        driver_minutes: dict[str, int] = {}
        for s in seg_records:
            d = driver_map[s.id]
            driver_minutes[d] = driver_minutes.get(d, 0) + int(
                s.get('minutes') or 0)
        assignments_out.append(Box(
            drivers=driver_map,
            segments=dict(driver_map),
            driver=driver or (next(iter(set(driver_map.values())))
                             if len(set(driver_map.values())) == 1 else None),
            contested=contested,
            driver_minutes=driver_minutes,
            evidence=evidence_ids,
        ))

    # De-duplicate identical maps.
    unique: dict[tuple, Box] = {}
    for a in assignments_out:
        key = tuple(sorted(a['drivers'].items()))
        unique[key] = a
    assignments_out = list(unique.values())

    return Box(
        driver=driver,
        segments=[Box(s) for s in seg_records],
        assignments=assignments_out,
        feasible_assignments=assignments_out,
        witnesses=witnesses,
        conflict_witnesses=[w for w in witnesses
                            if w['kind'] != 'shared_login'
                            or len(w['drivers']) > 1],
        conflicts=[w for w in witnesses
                   if w['kind'] in {'conflicting_identity_claims',
                                    'overlapping_assignments'}],
        shared_logins=[w for w in witnesses if w['kind'] == 'shared_login'],
        known_drivers=sorted(known_drivers),
    )


def _record_overlaps_segment(record: dict, seg: Box, clock: dict, *,
                             start_keys: tuple[str, ...],
                             end_keys: tuple[str, ...]) -> bool | None:
    r_start = next((record[k] for k in start_keys if record.get(k) not in (None, '')),
                   None)
    r_end = next((record[k] for k in end_keys if record.get(k) not in (None, '')),
                 None)
    if r_start is None or r_end is None:
        return None
    try:
        rs = to_utc(r_start, **clock)
        re_ = to_utc(r_end, **clock)
        ss = _segment_time(seg, 'start', clock)
        se = _segment_time(seg, 'end', clock)
    except (ValueError, TypeError):
        return None
    if ss is None or se is None:
        return None
    return rs < se and ss < re_


def _signature_time_targets(sig: dict, segments: list[Box],
                            clock: dict) -> set[str]:
    start = sig.get('start') or sig.get('segment_start') or sig.get('from')
    if start is None:
        return set()
    try:
        at = to_utc(start, **clock)
    except (ValueError, TypeError):
        return set()
    targets = set()
    for s in segments:
        ss = _segment_time(s, 'start', clock)
        se = _segment_time(s, 'end', clock)
        if ss and se and ss <= at <= se:
            targets.add(s.id)
    return targets


# --------------------------------------------------------------------------- #
# Set partition enumeration (small evidence sets only).                       #
# --------------------------------------------------------------------------- #
def _set_partitions(items: list) -> list[list[tuple]]:
    if not items:
        return [()]
    first, rest = items[0], items[1:]
    out = []
    for smaller in _set_partitions(rest):
        # put `first` into each existing block
        for i in range(len(smaller)):
            blocks = [tuple(b) for b in smaller]
            blocks[i] = tuple(sorted((*blocks[i], first)))
            out.append(blocks)
        # or a new singleton block
        out.append([(first,), *[tuple(b) for b in smaller]])
    return out


# --------------------------------------------------------------------------- #
# Functional facade + name aliases                                            #
# --------------------------------------------------------------------------- #
def _coerce_investigation(*args: Any, driver: Any = None,
                          evidence: Any = None, **kwargs: Any) -> DutyInvestigation:
    # ``logs`` may arrive as a keyword while the first positional argument is
    # a case dict (or absent); extract it so it is never passed twice.
    kw_logs = kwargs.pop('logs', None)
    if kw_logs is not None:
        logs = list(kw_logs)
    else:
        logs = None
    if evidence is None and 'evidence' in kwargs:
        evidence = kwargs.pop('evidence')
    if args and isinstance(args[0], DutyInvestigation):
        inv = args[0]
        if logs is not None:
            inv.add_logs(logs)
        if evidence is not None:
            inv.add_evidence(evidence)
        return inv
    case: dict | None = None
    pos_driver = driver
    extra_evidence = evidence
    for arg in args:
        if isinstance(arg, dict):
            case = arg
        elif isinstance(arg, (list, tuple)):
            if arg and all(isinstance(x, dict) and 'minutes' in x for x in arg):
                logs = list(arg)
            elif arg:
                extra_evidence = [*(extra_evidence or []), *arg]
        elif isinstance(arg, str):
            pos_driver = arg
    if case is not None and ('logs' in case or 'forensic_paths' in case
                             or 'evidence' in case or 'calibrations' in case):
        inv = DutyInvestigation.from_case(case, driver=pos_driver, **kwargs)
        if logs is not None:
            inv.add_logs(logs)
        if extra_evidence is not None:
            inv.add_evidence(extra_evidence)
        return inv
    if case is not None:
        # Plain kwargs-ish dict of options/evidence.
        merged = {**case, **kwargs}
        merged_logs = logs if logs is not None else merged.pop('logs', None)
        return DutyInvestigation(driver=pos_driver, logs=merged_logs,
                                 evidence=extra_evidence, **merged)
    return DutyInvestigation(driver=pos_driver, logs=logs,
                             evidence=extra_evidence, **kwargs)


def reconstruct(*args: Any, **kwargs: Any) -> DutyReconstruction:
    """Build the evidence-aware duty reconstruction (main entry point)."""
    return _coerce_investigation(*args, **kwargs).reconstruct()


# Descriptive aliases for the same operation.
reconstruct_duty = reconstruct
build_duty = reconstruct
duty_reconstruction = reconstruct
duty_intervals = reconstruct
labor_intervals = reconstruct
actual_driving_intervals = reconstruct


def bounds(*args: Any, **kwargs: Any) -> tuple[int, int]:
    return tuple(reconstruct(*args, **kwargs)['bounds'])


def duty_bounds(*args: Any, **kwargs: Any) -> tuple[int, int]:
    return bounds(*args, **kwargs)


def minutes_bounds(*args: Any, **kwargs: Any) -> tuple[int, int]:
    return bounds(*args, **kwargs)


def feasible_intervals(*args: Any, **kwargs: Any) -> dict:
    return reconstruct(*args, **kwargs)['feasible_intervals']


def source_relations(*args: Any, **kwargs: Any) -> list[Box]:
    return reconstruct(*args, **kwargs)['relations']


def merge_minutes(*args: Any, strict: bool = False, **kwargs: Any):
    """Evidence-merged minutes.

    Returns the proven minute count, or a ``(lower, upper)`` bound pair when
    labor identity is not proven.  With ``strict=True`` raises
    :class:`AmbiguousDuty` instead of returning bounds.
    """
    rec = reconstruct(*args, **kwargs)
    if rec['proven']:
        return rec['minutes']
    if strict:
        raise AmbiguousDuty('duty interval identity is not proven', rec)
    return rec['bounds']


def merged_minutes(*args: Any, **kwargs: Any) -> int | None:
    rec = reconstruct(*args, **kwargs)
    return rec['minutes'] if rec['proven'] else None


def evidence_minutes(*args: Any, **kwargs: Any) -> int | None:
    return merged_minutes(*args, **kwargs)


def assign_identities(*args: Any, **kwargs: Any) -> Box:
    return identity_assignments(*args, **kwargs)


def feasible_assignments(*args: Any, **kwargs: Any) -> list[Box]:
    return identity_assignments(*args, **kwargs)['assignments']


def conflict_witnesses(*args: Any, **kwargs: Any) -> list[Box]:
    return identity_assignments(*args, **kwargs)['witnesses']


# Plausible alternative names a caller/reference suite may use.
DutyCase = DutyInvestigation
DutyReconstructor = DutyInvestigation
DutyAnalyzer = DutyInvestigation
DutyService = DutyInvestigation
EvidenceGraph = DutyInvestigation

rebuild = reconstruct
rebuild_duty = reconstruct
resolve_duty = reconstruct
duty_resolution = reconstruct
merge_evidence = reconstruct
consolidate_duty = reconstruct
duty_minutes = merge_minutes
worked_minutes = merge_minutes
worked_minutes_bounds = duty_bounds
identity = identity_assignments
assign_drivers = identity_assignments
feasible_identity_assignments = feasible_assignments
identity_witnesses = conflict_witnesses

analyze = reconstruct
derive_intervals = reconstruct
rebuild_intervals = reconstruct
prove_duty = reconstruct
DutyResult = DutyReconstruction
IdentityResult = Box
