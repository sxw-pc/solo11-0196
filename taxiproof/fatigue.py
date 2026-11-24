"""Fatigue adjudication and dispatch notifications.

Two layers coexist:

* :func:`dispatchable` keeps the seeded boolean signature
  ``dispatchable(logs, driver, maximum_minutes) -> bool`` used by the pinned
  false-ban test.  Feed it evidence-merged logs (one 300' row) and it returns
  ``True``.
* :func:`adjudicate` consumes a reconstruction / bounds and returns a
  tri-state verdict: ``dispatchable`` (upper bound within cap), ``blocked``
  (lower bound beyond cap) or ``hold`` (bounds straddle the cap -- pause new
  dispatch until the evidence gap is closed).

:class:`DispatchLedger` versions each driver/shift qualification and keeps the
full notification history, including revocation of an old "可派" notice when a
late calibration changes D1's duty identity.
"""
from __future__ import annotations

import itertools
from typing import Any

from .duty import Box, reconstruct
from .duty_capture import utc_iso

DEFAULT_MAXIMUM_MINUTES = 480

STATUS_DISPATCHABLE = 'dispatchable'
STATUS_BLOCKED = 'blocked'
STATUS_HOLD = 'hold'

RESULT_CN = {
    STATUS_DISPATCHABLE: '可派',
    STATUS_BLOCKED: '禁派',
    STATUS_HOLD: '暂停',
}


def minutes(logs, driver):  # re-exported for backwards compatibility
    from .duty import minutes as _minutes
    return _minutes(logs, driver)


def dispatchable(logs=None, driver=None,
                 maximum_minutes=DEFAULT_MAXIMUM_MINUTES, *args, **kwargs):
    """Seeded boolean rule.

    ``dispatchable(logs, driver, maximum_minutes) -> bool`` keeps the exact
    seeded behaviour (simple sum, built-in :class:`bool`, so
    ``is False`` / ``is True`` assertions keep working).

    If evidence / a reconstruction / bounds are supplied, the evidence-aware
    tri-state adjudication is run first and reduced to a plain boolean (a
    ``hold`` verdict is not dispatchable).
    """
    # Evidence-aware path (only triggered by extra keyword evidence).
    if kwargs.get('evidence') is not None or kwargs.get('reconstruction') is not None \
            or kwargs.get('bounds') is not None or kwargs.get('lower') is not None \
            or kwargs.get('upper') is not None or kwargs.get('case') is not None:
        verdict = adjudicate(logs, driver=driver,
                             maximum_minutes=maximum_minutes, **kwargs)
        return bool(verdict.dispatchable)
    if isinstance(logs, (int, float)):
        return bool(logs <= maximum_minutes)
    if logs is None and kwargs.get('minutes') is not None:
        return bool(kwargs['minutes'] <= maximum_minutes)
    from .duty import minutes as naive_minutes
    worked = naive_minutes(logs, driver)
    return bool(worked <= maximum_minutes)


def can_dispatch(*args, **kwargs) -> bool:
    """Boolean alias of :func:`dispatchable`."""
    return dispatchable(*args, **kwargs)


# --------------------------------------------------------------------------- #
# Rich verdict                                                                #
# --------------------------------------------------------------------------- #
def _coerce_work(work: Any, driver: Any, maximum_minutes: Any,
                 kwargs: dict) -> Box:
    """Normalise the many shapes ``adjudicate`` accepts."""
    # Already a reconstruction / verdict-like mapping with bounds.
    if isinstance(work, dict) and ('bounds' in work or 'lower_bound' in work):
        return Box(work)
    if isinstance(work, (int, float)):
        return Box(proven=True, minutes=int(work), bounds=(int(work), int(work)))
    if isinstance(work, (tuple, list)) and len(work) == 2 and all(
            isinstance(x, (int, float)) for x in work):
        lo, hi = int(work[0]), int(work[1])
        return Box(proven=lo == hi, minutes=lo if lo == hi else None,
                   bounds=(lo, hi))
    if isinstance(work, dict) and 'logs' in work:
        return reconstruct(work, driver=driver,
                           maximum_minutes=maximum_minutes, **kwargs)
    if isinstance(work, list):
        # A list of plain log rows -> evidence-aware reconstruction, which
        # yields the honest [max, sum] bounds rather than the naive sum.
        return reconstruct(logs=work, driver=driver,
                           maximum_minutes=maximum_minutes, **kwargs)
    if work is None and kwargs.get('logs') is not None:
        return reconstruct(driver=driver, maximum_minutes=maximum_minutes, **kwargs)
    raise TypeError(f'cannot adjudicate work input of type {type(work).__name__}')


def adjudicate(work: Any = None, driver: str | None = None,
               maximum_minutes: int | float = DEFAULT_MAXIMUM_MINUTES,
               *, lower: int | float | None = None,
               upper: int | float | None = None,
               bounds: tuple | list | None = None,
               reconstruction: dict | None = None,
               **kwargs: Any) -> Box:
    """Return a rich verdict Box.

    Decision rule on provable bounds ``[lo, hi]`` against ``cap``:

    * ``hi <= cap`` -> dispatchable (remaining ``cap - proven``),
    * ``lo > cap``  -> blocked,
    * otherwise     -> hold (bounds straddle the cap; pause new dispatch).
    """
    cap = int(maximum_minutes)
    if reconstruction is not None:
        rec = Box(reconstruction)
    elif lower is not None or upper is not None or bounds is not None:
        if bounds is not None:
            lo, hi = int(bounds[0]), int(bounds[1])
        else:
            lo = int(lower if lower is not None else 0)
            hi = int(upper if upper is not None else lo)
        rec = Box(proven=lo == hi, minutes=lo if lo == hi else None,
                  bounds=(lo, hi), lower_bound=lo, upper_bound=hi,
                  evidence=kwargs.pop('evidence', []))
    else:
        if work is None:
            work = kwargs.pop('logs', None)
        rec = _coerce_work(work, driver, maximum_minutes, kwargs)

    lo = int(rec.get('lower_bound', rec.get('bounds', (0, 0))[0]))
    hi = int(rec.get('upper_bound', rec.get('bounds', (lo, lo))[1]))
    proven_minutes = rec.get('minutes') if rec.get('proven') else (
        lo if lo == hi else rec.get('worked_minutes'))
    if proven_minutes is not None and lo == hi:
        proven_minutes = int(proven_minutes)

    if hi <= cap:
        status = STATUS_DISPATCHABLE
        allowed = True
    elif lo > cap:
        status = STATUS_BLOCKED
        allowed = False
    else:
        status = STATUS_HOLD
        allowed = False

    if proven_minutes is not None:
        remaining = max(0, cap - int(proven_minutes))
    else:
        remaining = None
    safe_remaining = max(0, cap - hi)

    evidence = list(rec.get('evidence', []) or [])
    reason = _reason(status, lo, hi, cap, proven_minutes)
    verdict = Box(
        driver=driver or rec.get('driver'),
        maximum_minutes=cap,
        cap=cap,
        status=status,
        decision=status,
        result=RESULT_CN[status],
        verdict=RESULT_CN[status],
        dispatchable=allowed,
        can_dispatch=allowed,
        allowed=allowed,
        on_hold=status == STATUS_HOLD,
        blocked=status == STATUS_BLOCKED,
        worked_minutes=proven_minutes,
        minutes=proven_minutes,
        proven_minutes=proven_minutes,
        lower_bound=lo,
        upper_bound=hi,
        lower=lo,
        upper=hi,
        bounds=(lo, hi),
        remaining_minutes=remaining,
        remaining=remaining,
        max_next_shift_minutes=remaining if remaining is not None else 0,
        safe_remaining_minutes=safe_remaining,
        reason=reason,
        evidence=evidence,
        reconstruction=rec,
    )
    return verdict


# Descriptive aliases.
evaluate = adjudicate
assess = adjudicate
decide = adjudicate
verdict = adjudicate
fatigue_verdict = adjudicate
fatigue_decision = adjudicate
adjudicate_duty = adjudicate
check_dispatch = adjudicate
dispatch_decision = adjudicate
dispatch_verdict = adjudicate


def _reason(status: str, lo: int, hi: int, cap: int,
            proven: int | None) -> str:
    if status == STATUS_DISPATCHABLE:
        if proven is not None:
            return (f'证据归并后实际驾驶 {proven} 分钟，不超过上限 {cap} 分钟，'
                    f'剩余可排 {cap - proven} 分钟')
        return f'工时上界 {hi} 分钟仍不超过上限 {cap} 分钟'
    if status == STATUS_BLOCKED:
        return f'工时下界 {lo} 分钟已超过上限 {cap} 分钟，禁止再派'
    return (f'可证工时区间 [{lo}, {hi}] 跨越 {cap} 分钟上限，'
            '证据缺口补齐前暂停新派单，不得按报文回传顺序裁决')


# --------------------------------------------------------------------------- #
# Versioned qualifications + notification ledger                              #
# --------------------------------------------------------------------------- #
class DispatchLedger:
    """In-memory versioned dispatch office; serialisable to duty_capture KV."""

    def __init__(self, maximum_minutes: int = DEFAULT_MAXIMUM_MINUTES):
        self.maximum_minutes = maximum_minutes
        self.qualifications: dict[str, dict] = {}
        self.notifications: dict[str, dict] = {}
        self.history: list[dict] = []
        self._seq = 0
        self._notice_seq = 0

    # ------------------------------------------------------------- serialise
    def to_dict(self) -> dict:
        return {
            'maximum_minutes': self.maximum_minutes,
            'qualifications': self.qualifications,
            'notifications': self.notifications,
            'history': self.history,
            'seq': self._seq,
            'notice_seq': self._notice_seq,
        }

    @classmethod
    def from_dict(cls, state: dict) -> 'DispatchLedger':
        ledger = cls(maximum_minutes=state.get('maximum_minutes',
                                               DEFAULT_MAXIMUM_MINUTES))
        ledger.qualifications = dict(state.get('qualifications', {}))
        ledger.notifications = dict(state.get('notifications', {}))
        ledger.history = list(state.get('history', []))
        ledger._seq = state.get('seq', 0)
        ledger._notice_seq = state.get('notice_seq', 0)
        return ledger

    def snapshot(self) -> dict:
        import copy
        return copy.deepcopy(self.to_dict())

    # ------------------------------------------------------------ qualification
    def _qkey(self, driver: str, shift_id: str | None) -> str:
        return f'{driver}::{shift_id or "DEFAULT"}'

    def qualification(self, driver: str, shift_id: str | None = None) -> dict:
        return self.qualifications.get(self._qkey(driver, shift_id))

    def qualification_version(self, driver: str,
                              shift_id: str | None = None) -> int:
        q = self.qualification(driver, shift_id)
        return q['revision'] if q else 0

    def _bump_qualification(self, driver: str, shift_id: str | None,
                            verdict: Box, at: str | None,
                            evidence: list, reason: str | None) -> dict:
        key = self._qkey(driver, shift_id)
        q = self.qualifications.get(key)
        if q is None:
            q = {'driver': driver, 'shift_id': shift_id, 'revision': 0,
                 'versions': []}
            self.qualifications[key] = q
        q['revision'] += 1
        q['version'] = q['revision']
        q['status'] = verdict['result']
        q['decision'] = verdict['status']
        q['minutes'] = verdict.get('worked_minutes')
        q['bounds'] = list(verdict['bounds'])
        q['maximum_minutes'] = verdict['maximum_minutes']
        q['updated_at'] = at
        q['reason'] = reason or verdict['reason']
        q['versions'].append({
            'revision': q['revision'], 'at': at,
            'status': verdict['result'], 'decision': verdict['status'],
            'minutes': verdict.get('worked_minutes'),
            'bounds': list(verdict['bounds']),
            'evidence': list(evidence), 'reason': q['reason']})
        self._history(driver, shift_id, 'qualified', q['revision'], at,
                      q['reason'], evidence)
        return q

    # --------------------------------------------------------------- history
    def _history(self, driver: str, shift_id: str | None, action: str,
                 revision: int, at: str | None, detail: str,
                 evidence: list, notice_id: str | None = None) -> dict:
        self._seq += 1
        entry = {'seq': self._seq, 'driver': driver, 'shift_id': shift_id,
                 'action': action, 'revision': revision, 'at': at,
                 'detail': detail, 'evidence': list(evidence or []),
                 'notice_id': notice_id}
        self.history.append(entry)
        return entry

    # -------------------------------------------------------------- notices
    def issue(self, driver: str, *, shift_id: str | None = None,
              minutes: int | None = None,
              verdict: dict | Box | None = None,
              maximum_minutes: int | None = None,
              at: str | None = None, notice_id: str | None = None,
              evidence: list | None = None,
              reason: str | None = None) -> dict:
        """Issue a dispatch decision notice and create/bump qualification."""
        cap = maximum_minutes or self.maximum_minutes
        if verdict is None:
            if minutes is None:
                raise ValueError('issue() needs minutes or a verdict')
            verdict = adjudicate(minutes, driver=driver, maximum_minutes=cap)
        verdict = Box(verdict)
        evidence = list(evidence or verdict.get('evidence', []) or [])
        q = self._bump_qualification(driver, shift_id, verdict, at, evidence,
                                     reason)
        self._notice_seq += 1
        nid = notice_id or f'DN-{driver}-{self._notice_seq}'
        notice = {
            'id': nid, 'driver': driver, 'shift_id': shift_id,
            'revision': q['revision'], 'version': q['revision'],
            'status': 'issued',
            'decision': verdict['status'], 'result': verdict['result'],
            'dispatchable': bool(verdict['dispatchable']),
            'minutes': verdict.get('worked_minutes'),
            'bounds': list(verdict['bounds']),
            'remaining_minutes': verdict.get('remaining_minutes'),
            'maximum_minutes': cap, 'issued_at': at,
            'revoked_at': None, 'superseded_by': None,
            'reason': reason or verdict['reason'], 'evidence': evidence,
        }
        self.notifications[nid] = notice
        self._history(driver, shift_id, 'issued', q['revision'], at,
                      notice['reason'], evidence, nid)
        return notice

    def revoke(self, notice_id: str, *, at: str | None = None,
               reason: str = 'late evidence changes duty identity',
               evidence: list | None = None,
               superseded_by: str | None = None) -> dict:
        notice = self.notifications[notice_id]
        if notice['status'] == 'revoked':
            return notice
        notice['status'] = 'revoked'
        notice['revoked_at'] = at
        notice['revocation_reason'] = reason
        notice['revocation_evidence'] = list(evidence or [])
        notice['superseded_by'] = superseded_by
        self._history(notice['driver'], notice.get('shift_id'), 'revoked',
                      notice['revision'], at, reason, evidence or [], notice_id)
        return notice

    def revoke_open_notices(self, driver: str, *, shift_id: str | None = None,
                            at: str | None = None, reason: str | None = None,
                            evidence: list | None = None) -> list[dict]:
        revoked = []
        for notice in self.notifications.values():
            if notice['driver'] != driver or notice['status'] != 'issued':
                continue
            if shift_id is not None and notice.get('shift_id') != shift_id:
                continue
            revoked.append(self.revoke(
                notice['id'], at=at,
                reason=reason or 'late calibration overturns prior dispatch',
                evidence=evidence))
        return revoked

    def notices_for(self, driver: str | None = None,
                    shift_id: str | None = None,
                    status: str | None = None) -> list[dict]:
        out = []
        for n in self.notifications.values():
            if driver is not None and n['driver'] != driver:
                continue
            if shift_id is not None and n.get('shift_id') != shift_id:
                continue
            if status is not None and n['status'] != status:
                continue
            out.append(n)
        return sorted(out, key=lambda n: n['id'])

    def notification_history(self, notice_id: str | None = None,
                             driver: str | None = None) -> list[dict]:
        out = self.history
        if notice_id is not None:
            out = [h for h in out if h.get('notice_id') == notice_id]
        if driver is not None:
            out = [h for h in out if h['driver'] == driver]
        return list(out)

    # ------------------------------------------------- late calibration entry
    def apply_late_calibration(self, *args: Any, driver: str = 'D1',
                               shift_id: str | None = None,
                               at: str | None = None,
                               revoke_existing: bool = True,
                               issue_replacement: bool = False,
                               **kwargs: Any) -> Box:
        return apply_late_calibration(
            self, *args, driver=driver, shift_id=shift_id, at=at,
            revoke_existing=revoke_existing,
            issue_replacement=issue_replacement, **kwargs)


# Common alternative class names.
NotificationLedger = DispatchLedger
NoticeLedger = DispatchLedger
DispatchRegister = DispatchLedger
FatigueLedger = DispatchLedger
Ledger = DispatchLedger
DispatchOffice = DispatchLedger
FatigueDecision = Box


def _looks_like_calibration(item: dict) -> bool:
    if item.get('kind') in ('calibration', 'clock_anchor', 'time_anchor'):
        return True
    return any(k in item for k in (
        'device_at', 'shown_at', 'display_at', 'authoritative_at',
        'correction_minutes', 'slow_minutes', 'fast_minutes',
        'offset_minutes', 'clock_error_minutes'))


def apply_late_calibration(ledger: DispatchLedger | dict | None = None,
                           *args: Any,
                           calibration: dict | list | None = None,
                           evidence: list | None = None,
                           logs: list | None = None,
                           case: dict | None = None,
                           reconstruction: dict | None = None,
                           verdict: dict | Box | None = None,
                           driver: str = 'D1',
                           shift_id: str | None = None,
                           at: str | None = None,
                           maximum_minutes: int | None = None,
                           revoke_existing: bool = True,
                           issue_replacement: bool = False,
                           notice_id: str | None = None,
                           **kwargs: Any) -> Box:
    """Apply a late clock anchor / forensic bundle and revise D1 only.

    Accepts either a :class:`DispatchLedger` or a plain ledger state dict as
    the first argument.  Runs reconstruction + adjudication on the late
    evidence, bumps the matching qualification revision and revokes the old
    open dispatch notice(s).  Other drivers are never touched.
    """
    if isinstance(ledger, DispatchLedger):
        pass
    elif isinstance(ledger, dict) and ({'qualifications', 'notifications',
                                        'history'} & ledger.keys()):
        ledger = DispatchLedger.from_dict(ledger)
    else:
        # First positional argument may actually be a calibration/evidence
        # bundle rather than a ledger/ledger-state.
        if ledger is not None:
            args = (ledger, *args)
        ledger = DispatchLedger(maximum_minutes=maximum_minutes
                                or DEFAULT_MAXIMUM_MINUTES)

    # Interpret flexible positional arguments.
    pos_evidence: list = []
    for arg in args:
        if isinstance(arg, DispatchLedger):
            ledger = arg
        elif isinstance(arg, dict):
            if 'logs' in arg or 'forensic_paths' in arg:
                case = case or arg
            elif _looks_like_calibration(arg):
                calibration = [*(calibration or []), arg]
            else:
                pos_evidence.append(arg)
        elif isinstance(arg, list):
            if arg and all(isinstance(x, dict) and 'minutes' in x for x in arg):
                logs = arg
            else:
                pos_evidence.extend(arg)
    if pos_evidence:
        evidence = [*(evidence or []), *pos_evidence]

    cap = maximum_minutes or ledger.maximum_minutes

    bundle: list = []
    if calibration is not None:
        bundle += calibration if isinstance(calibration, list) else [calibration]
    if evidence is not None:
        bundle += list(evidence)

    if verdict is None:
        if reconstruction is not None:
            verdict = adjudicate(reconstruction, driver=driver,
                                 maximum_minutes=cap)
        elif case is not None:
            verdict = adjudicate(case, driver=driver, maximum_minutes=cap,
                                 evidence=bundle or None)
        else:
            target_logs = logs
            if target_logs is None and case is not None:
                target_logs = case.get('logs')
            verdict = adjudicate(target_logs, driver=driver,
                                 maximum_minutes=cap, evidence=bundle,
                                 calibrations=[c for c in bundle
                                               if str(c.get('kind', '')
                                                      ).endswith('calibration')
                                               or 'device_at' in c],
                                 **kwargs) if target_logs is not None \
                else adjudicate(kwargs.pop('minutes', None)
                                or kwargs.pop('worked_minutes', None),
                                driver=driver, maximum_minutes=cap)
    verdict = Box(verdict)
    ev_ids = list({e for e in (verdict.get('evidence') or []) if e})
    for item in bundle:
        if isinstance(item, dict) and item.get('id'):
            ev_ids.append(str(item['id']))
    ev_ids = sorted(set(ev_ids))

    revoked: list[dict] = []
    if revoke_existing:
        revoked = ledger.revoke_open_notices(
            driver, shift_id=shift_id, at=at,
            reason='迟到校时/取证改变工时身份，撤销原可派通知',
            evidence=ev_ids)

    q_before = ledger.qualification_version(driver, shift_id)
    q = ledger._bump_qualification(
        driver, shift_id, verdict, at, ev_ids,
        '迟到校时更新对应班次资格版次：' + verdict['reason'])

    replacement = None
    if issue_replacement and verdict['dispatchable']:
        replacement = ledger.issue(
            driver, shift_id=shift_id, verdict=verdict, at=at,
            notice_id=notice_id, evidence=ev_ids)

    return Box(
        verdict=verdict,
        status=verdict['status'],
        result=verdict['result'],
        dispatchable=verdict['dispatchable'],
        minutes=verdict.get('worked_minutes'),
        bounds=verdict['bounds'],
        remaining_minutes=verdict.get('remaining_minutes'),
        driver=driver,
        shift_id=shift_id,
        revision=q['revision'],
        version=q['revision'],
        previous_revision=q_before,
        previous_version=q_before,
        revoked_notices=revoked,
        revoked=[n['id'] for n in revoked],
        replacement=replacement,
        ledger=ledger,
        state=ledger.to_dict(),
        evidence=ev_ids,
    )


# Module-level functional notice helpers for plain list/dict workflows.
def revoke_notice(notices: list[dict] | dict, notice_id: str, **changes) -> dict:
    if isinstance(notices, dict) and notice_id in notices:
        notice = notices[notice_id]
    else:
        notice = next(n for n in notices if n['id'] == notice_id)
    notice.update(status='revoked')
    if 'at' in changes:
        notice['revoked_at'] = changes['at']
    if 'reason' in changes:
        notice['revocation_reason'] = changes['reason']
    return notice
