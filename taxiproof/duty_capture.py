"""Offline capture for duty evidence.

The capture layer only does the mechanical work that cannot by itself decide
whether two messages describe one relayed shift or two shifts from different
clock epochs:

* byte / canonical-JSON SHA-256 hashing and hash de-duplication,
* tolerant parsing of local wall-clock values and UTC conversion,
* transactional SQLite persistence (same id + different body rolls back),
* a small key/value pocket for serialised dispatch/notification state.

Everything here accepts plain dictionaries so callers can pass the seeded
``fixtures/duty-relay.json`` dossier or evidence assembled inline.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date as date_cls
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path
from typing import Any, Iterable

CANONICAL_DATE = '2026-11-24'
DEFAULT_TIMEZONE = '+08:00'

# Keys that carry an absolute or wall-clock timestamp in evidence records.
TIME_KEYS = frozenset({
    'start', 'end', 'at', 'time', 'timestamp', 'observed_at', 'seen_at',
    'recorded_at', 'device_at', 'shown_at', 'display_at', 'anchor_at',
    'authoritative_at', 'true_at', 'utc_at', 'login_at', 'logout_at',
    'signed_at', 'issued_at', 'revoked_at', 'superseded_at', 'effective_at',
    'pickup_at', 'dropoff_at', 'verified_at', 'arrived_at',
})

KIND_ALIASES = {
    'log': 'log', 'logs': 'log', 'duty_log': 'log', 'duty_logs': 'log',
    'report': 'log', 'reports': 'log', 'duty_report': 'log',
    'duty_reports': 'log', 'message': 'log', 'messages': 'log',
    'terminal_report': 'log', '驾驶报文': 'log', '报文': 'log',
    'calibration': 'calibration', 'calibrations': 'calibration',
    'clock_anchor': 'calibration', 'clock_anchors': 'calibration',
    'time_anchor': 'calibration', 'time_anchors': 'calibration',
    'anchor': 'calibration', 'anchors': 'calibration',
    'time_sync': 'calibration', 'timing': 'calibration',
    'timing_anchor': 'calibration', '授时': 'calibration', '校时': 'calibration',
    'provenance': 'provenance', 'source_chain': 'provenance',
    'origin_chain': 'provenance', 'lineage': 'provenance',
    'relay': 'provenance', 'relay_link': 'provenance',
    'forward': 'provenance', 'forwarded': 'provenance',
    'forwarding': 'provenance', 'source_link': 'provenance',
    'source_links': 'provenance', '回源链': 'provenance', '转发': 'provenance',
    'pulse': 'pulse', 'pulses': 'pulse', 'vehicle_pulse': 'pulse',
    'vehicle_pulses': 'pulse', 'telemetry': 'pulse', 'gps': 'pulse',
    'gps_pulse': 'pulse', 'can_pulse': 'pulse', '脉冲': 'pulse',
    'order': 'order', 'orders': 'order', 'dispatch_order': 'order',
    'dispatch_orders': 'order', 'order_original': 'order', '订单': 'order',
    'login': 'login', 'logins': 'login', 'session': 'login',
    'sessions': 'login', 'login_logout': 'login', 'terminal_login': 'login',
    '登录': 'login',
    'handover': 'handover', 'handovers': 'handover',
    'shift_handover': 'handover', '交班': 'handover', '交接': 'handover',
    'signature': 'signature', 'signatures': 'signature',
    'ack': 'signature', 'acks': 'signature',
    'acknowledgment': 'signature', 'acknowledgments': 'signature',
    'acknowledgement': 'signature', 'acknowledgements': 'signature',
    'signoff': 'signature', 'signed_segment': 'signature',
    'signed_segments': 'signature', 'signed_shift': 'signature',
    'signed_shifts': 'signature', 'driver_signature': 'signature',
    'driver_signatures': 'signature', '签认': 'signature',
    'shift': 'shift', 'shifts': 'shift', 'assignment': 'shift',
    'assignments': 'shift', 'roster': 'shift', 'shift_record': 'shift',
    'notification': 'notification', 'notifications': 'notification',
    'notice': 'notification', 'notices': 'notification',
    'dispatch_notice': 'notification', 'dispatch_notices': 'notification',
}

# Dossier keys whose list value is evidence of the key's kind.
SECTION_KINDS = {
    'logs': 'log',
    'records': None,  # items carry their own kind
    'items': None,
    'evidence': None,
    'evidence_items': None,
    'calibrations': 'calibration',
    'anchors': 'calibration',
    'clock_anchors': 'calibration',
    'time_anchors': 'calibration',
    'provenance': 'provenance',
    'links': 'provenance',
    'source_links': 'provenance',
    'pulses': 'pulse',
    'vehicle_pulses': 'pulse',
    'orders': 'order',
    'logins': 'login',
    'sessions': 'login',
    'handovers': 'handover',
    'signatures': 'signature',
    'acknowledgments': 'signature',
    'acknowledgements': 'signature',
    'acks': 'signature',
    'shifts': 'shift',
    'assignments': 'shift',
    'notifications': 'notification',
    'notices': 'notification',
}


def norm_kind(value: Any) -> str:
    """Map heterogeneous kind/type labels onto the canonical evidence kinds."""
    if value is None:
        return 'evidence'
    key = str(value).strip().lower().replace('-', '_').replace(' ', '_')
    return KIND_ALIASES.get(key, KIND_ALIASES.get(str(value).strip().lower(), key))


def content_hash(raw: bytes | str) -> str:
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


# Friendly aliases used by callers/tests.
sha256 = content_hash
hash_bytes = content_hash


def canonical_bytes(doc: Any) -> bytes:
    return json.dumps(doc, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def parse_timezone(tz: Any = DEFAULT_TIMEZONE) -> dt_timezone:
    if isinstance(tz, dt_timezone):
        return tz
    if isinstance(tz, timedelta):
        return dt_timezone(tz)
    if isinstance(tz, (int, float)):
        return dt_timezone(timedelta(hours=float(tz)))
    text = str(tz).strip()
    if text.upper() in ('Z', 'UTC', 'GMT'):
        return dt_timezone.utc
    match = re.fullmatch(r'([+-])(\d{1,2}):?(\d{2})', text)
    if not match:
        # Last resort: let datetime try an ISO offset fragment.
        try:
            return datetime.fromisoformat('2026-01-01T00:00:00' + text).tzinfo  # type: ignore[return-value]
        except ValueError as exc:
            raise ValueError(f'unsupported timezone offset: {tz!r}') from exc
    sign, hours, minutes = match.groups()
    delta = timedelta(hours=int(hours), minutes=int(minutes))
    if sign == '-':
        delta = -delta
    return dt_timezone(delta)


def reference_date(value: Any = CANONICAL_DATE) -> date_cls:
    if isinstance(value, date_cls) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date_cls.fromisoformat(str(value)[:10])


_HHMM = re.compile(r'(\d{1,2}):(\d{2})(?::(\d{2}))?')


def to_utc(value: Any, *, date: Any = CANONICAL_DATE,
           timezone: Any = DEFAULT_TIMEZONE) -> datetime:
    """Parse ISO / ``HH:MM`` evidence time and return an aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        # Minutes since local midnight is a common compact encoding.
        minutes = float(value)
        day = reference_date(date)
        dt = datetime(day.year, day.month, day.day) + timedelta(minutes=minutes)
    else:
        text = str(value).strip()
        match = _HHMM.fullmatch(text)
        if match:
            hour, minute, second = match.groups()
            day = reference_date(date)
            dt = datetime(day.year, day.month, day.day,
                          int(hour), int(minute), int(second or 0))
        else:
            text = text[:-1] + '+00:00' if text.endswith('Z') else text
            dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=parse_timezone(timezone))
    return dt.astimezone(dt_timezone.utc)


def utc_iso(value: Any, **kwargs: Any) -> str:
    return to_utc(value, **kwargs).isoformat().replace('+00:00', 'Z')


def parse_wall(value: Any, date: Any = CANONICAL_DATE) -> datetime:
    """Parse a device wall-clock reading as *naive* local wall time.

    Anchors pair a device display (which must not be UTC-normalised) with an
    authoritative timestamp, so the device side intentionally drops tzinfo.
    """
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, (int, float)):
        day = reference_date(date)
        return datetime(day.year, day.month, day.day) + timedelta(minutes=float(value))
    text = str(value).strip()
    match = _HHMM.fullmatch(text)
    if match:
        hour, minute, second = match.groups()
        day = reference_date(date)
        return datetime(day.year, day.month, day.day,
                        int(hour), int(minute), int(second or 0))
    text = text[:-1] if text.endswith('Z') else text
    return datetime.fromisoformat(text).replace(tzinfo=None)


def case_clock_args(case: dict | None) -> dict:
    if not isinstance(case, dict):
        return {}
    out = {}
    for key in ('date', 'day', 'service_date'):
        if case.get(key):
            out['date'] = case[key]
            break
    for key in ('timezone', 'tz', 'utc_offset', 'zone'):
        if case.get(key):
            out['timezone'] = case[key]
            break
    return out


def utc_fields(item: dict, **clock: Any) -> dict:
    """Best-effort UTC view of the known timestamp fields of one record."""
    out = {}
    for key in TIME_KEYS:
        if key in item and item[key] not in (None, ''):
            try:
                out[key + '_utc'] = utc_iso(item[key], **clock)
            except (ValueError, TypeError):
                continue
    return out


def iter_evidence_items(doc: Any, *, nested_scenarios: bool = True
                        ) -> Iterable[tuple[str, dict]]:
    """Yield ``(canonical_kind, item)`` pairs from a dossier/payload.

    Accepts a single evidence dict, a list of evidence dicts, or a case
    dictionary containing the conventional plural sections.

    ``nested_scenarios=False`` skips the alternative forensic-path bundles
    and separate shared-login / late-calibration mini-cases so reconstruction
    never accidentally fuses mutually exclusive authorised paths.
    """
    if isinstance(doc, list):
        for item in doc:
            if isinstance(item, dict):
                yield norm_kind(item.get('kind') or item.get('type')), item
        return
    if not isinstance(doc, dict):
        return

    # A single explicitly typed evidence record.
    explicit = doc.get('kind') or doc.get('type')
    if explicit and explicit not in ('case', 'dossier', 'duty_case') and (
            'logs' not in doc and 'evidence' not in doc):
        yield norm_kind(explicit), doc

    for key, forced_kind in SECTION_KINDS.items():
        value = doc.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            kind = forced_kind or norm_kind(item.get('kind') or item.get('type') or key[:-1])
            yield kind, item

    if not nested_scenarios:
        return

    # Forensic path bundles are named evidence containers, not evidence itself.
    paths = doc.get('forensic_paths')
    if isinstance(paths, dict):
        for bundle in paths.values():
            if isinstance(bundle, dict):
                ev = bundle.get('evidence')
                if isinstance(ev, list):
                    for item in ev:
                        if isinstance(item, dict):
                            yield norm_kind(item.get('kind') or item.get('type')), item
    elif isinstance(paths, list):
        for bundle in paths:
            if isinstance(bundle, dict) and isinstance(bundle.get('evidence'), list):
                for item in bundle['evidence']:
                    if isinstance(item, dict):
                        yield norm_kind(item.get('kind') or item.get('type')), item

    shared = doc.get('shared_login')
    if isinstance(shared, dict):
        for pair in iter_evidence_items(shared):
            yield pair
    late = doc.get('late_calibration')
    if isinstance(late, dict):
        for pair in iter_evidence_items(late):
            yield pair


def load_case(path: str | Path) -> dict:
    """Read a duty dossier JSON file."""
    return json.loads(Path(path).read_text(encoding='utf-8'))


SCHEMA = """
CREATE TABLE IF NOT EXISTS duty_source (
    sha TEXT PRIMARY KEY,
    kind TEXT,
    raw BLOB NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS duty_evidence (
    evidence_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    sha TEXT NOT NULL,
    seq INTEGER NOT NULL,
    utc_start TEXT,
    utc_end TEXT
);
CREATE TABLE IF NOT EXISTS duty_kv (
    key TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def connect(path: str | Path = ':memory:') -> sqlite3.Connection:
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(SCHEMA)
    db.commit()
    return db


class DutyCapture:
    """Transactional SQLite pocket for duty evidence and ledger state."""

    def __init__(self, path: str | Path | sqlite3.Connection = ':memory:',
                 db: sqlite3.Connection | None = None):
        # Tolerate both ``DutyCapture('x.sqlite')`` and ``DutyCapture(db=conn)``
        # / the historical ``DutyCapture(path=...)`` keyword style.
        if isinstance(path, sqlite3.Connection):
            db = path
            path = ':memory:'
        self.db = db if db is not None else connect(path)
        self._owns_db = db is None

    @classmethod
    def open(cls, path: str | Path = ':memory:') -> 'DutyCapture':
        return cls(path)

    def close(self) -> None:
        if self._owns_db:
            self.db.close()

    def __enter__(self) -> 'DutyCapture':
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ ingest
    def ingest(self, doc: Any, *, kind: str | None = None,
               raw: bytes | str | None = None) -> dict:
        if raw is None:
            raw_bytes = canonical_bytes(doc)
        elif isinstance(raw, str):
            raw_bytes = raw.encode('utf-8')
        else:
            raw_bytes = raw
        sha = content_hash(raw_bytes)
        if self.db.execute('SELECT 1 FROM duty_source WHERE sha=?', (sha,)).fetchone():
            return {'sha': sha, 'inserted': 0, 'idempotent': True}

        clock = case_clock_args(doc if isinstance(doc, dict) else {})
        self.db.execute('SAVEPOINT duty_ingest')
        try:
            seq = self.db.execute(
                'SELECT COALESCE(MAX(seq),0) FROM duty_evidence').fetchone()[0]
            inserted = 0
            items = list(iter_evidence_items(doc))
            if not items and isinstance(doc, dict):
                items = [(norm_kind(kind or doc.get('kind') or 'case'), doc)]
            for item_kind, item in items:
                evidence_id = str(item.get('id') or item.get('evidence_id')
                                  or (sha + f':{seq + 1}'))
                body = json.dumps(item, ensure_ascii=False, sort_keys=True,
                                  separators=(',', ':'))
                old = self.db.execute(
                    'SELECT body FROM duty_evidence WHERE evidence_id=?',
                    (evidence_id,)).fetchone()
                if old is not None:
                    if old[0] != body:
                        raise ValueError(
                            f'same evidence id {evidence_id!r} with different content')
                    continue
                seq += 1
                inserted += 1
                utc_start = utc_end = None
                start_value = next((item[k] for k in (
                    'start', 'at', 'time', 'timestamp', 'login_at',
                    'observed_at', 'device_at') if item.get(k) not in (None, '')), None)
                end_value = next((item[k] for k in (
                    'end', 'logout_at', 'until') if item.get(k) not in (None, '')), None)
                try:
                    if start_value is not None:
                        utc_start = utc_iso(start_value, **clock)
                    if end_value is not None:
                        utc_end = utc_iso(end_value, **clock)
                except (ValueError, TypeError):
                    utc_start = utc_end = None
                self.db.execute(
                    'INSERT INTO duty_evidence VALUES (?,?,?,?,?,?,?)',
                    (evidence_id, item_kind, body, sha, seq, utc_start, utc_end))
            self.db.execute('INSERT INTO duty_source(sha,kind,raw) VALUES (?,?,?)',
                            (sha, kind or 'duty_case', raw_bytes))
            self.db.execute('RELEASE SAVEPOINT duty_ingest')
            self.db.commit()
        except Exception:
            self.db.execute('ROLLBACK TO SAVEPOINT duty_ingest')
            self.db.execute('RELEASE SAVEPOINT duty_ingest')
            self.db.rollback()
            raise
        return {'sha': sha, 'inserted': inserted, 'idempotent': False}

    def ingest_file(self, path: str | Path, *, kind: str | None = None) -> dict:
        p = Path(path)
        raw = p.read_bytes()
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            doc = None
        receipt = self.ingest(doc if doc is not None else raw, kind=kind, raw=raw)
        receipt['file'] = str(p)
        return receipt

    # ------------------------------------------------------------------- reads
    def evidence(self, kind: str | None = None) -> list[dict]:
        if kind is None:
            rows = self.db.execute('SELECT body FROM duty_evidence ORDER BY seq')
        else:
            rows = self.db.execute(
                'SELECT body FROM duty_evidence WHERE kind=? ORDER BY seq',
                (norm_kind(kind),))
        return [json.loads(r[0]) for r in rows]

    def sources(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            'SELECT sha, kind, imported_at FROM duty_source ORDER BY rowid')]

    # ------------------------------------------------------- serialised state
    def put_state(self, key: str, state: dict) -> None:
        body = json.dumps(state, ensure_ascii=False, sort_keys=True)
        with self.db:
            self.db.execute(
                'INSERT INTO duty_kv(key,body,updated_at) VALUES (?,?,CURRENT_TIMESTAMP) '
                'ON CONFLICT(key) DO UPDATE SET body=excluded.body, '
                'updated_at=CURRENT_TIMESTAMP', (key, body))

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.db.execute('SELECT body FROM duty_kv WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    # ------------------------------------------- fatigue ledger convenience IO
    def save_ledger(self, ledger: Any, key: str = 'dispatch-ledger') -> None:
        state = ledger.to_dict() if hasattr(ledger, 'to_dict') else dict(ledger)
        self.put_state(key, state)

    def load_ledger(self, key: str = 'dispatch-ledger'):
        from .fatigue import DispatchLedger
        state = self.get_state(key)
        return DispatchLedger.from_dict(state) if state is not None else DispatchLedger()

    def apply_late_calibration(self, *args: Any, key: str = 'dispatch-ledger',
                               **kwargs: Any):
        from .fatigue import apply_late_calibration
        ledger = self.load_ledger(key)
        result = apply_late_calibration(ledger, *args, **kwargs)
        self.save_ledger(ledger, key)
        return result


# Module level functional mirrors, stylistically aligned with taxiproof.store.
def ingest_file(db: sqlite3.Connection | DutyCapture, file: str | Path,
                kind: str | None = None) -> dict:
    capture = db if isinstance(db, DutyCapture) else DutyCapture(db)
    return capture.ingest_file(file, kind=kind)


def capture(path_or_doc: str | Path | dict | list | None = None,
            db_path: str | Path = ':memory:', **kwargs: Any) -> DutyCapture:
    """Open a capture book and optionally ingest a path/document immediately."""
    book = DutyCapture(path=db_path)
    if path_or_doc is not None:
        if isinstance(path_or_doc, (str, Path)) and Path(path_or_doc).exists():
            book.ingest_file(path_or_doc, **kwargs)
        else:
            book.ingest(path_or_doc, **kwargs)
    return book


# Alternative class / function names.
DutyBook = DutyCapture
CaptureBook = DutyCapture
EvidenceBook = DutyCapture
EvidenceCapture = DutyCapture
Capture = DutyCapture
open_book = DutyCapture.open


def hash_file(path: str | Path) -> str:
    return content_hash(Path(path).read_bytes())


def sha256_file(path: str | Path) -> str:
    return hash_file(path)


# Method-name aliases.
DutyCapture.ingest_source = DutyCapture.ingest_file
DutyCapture.add_file = DutyCapture.ingest_file
DutyCapture.save = DutyCapture.put_state


def capture_evidence(path_or_doc: Any = None, db_path: str | Path = ':memory:',
                     **kwargs: Any) -> DutyCapture:
    return capture(path_or_doc, db_path=db_path, **kwargs)


def ingest_path(path: str | Path, db_path: str | Path = ':memory:',
                **kwargs: Any) -> dict:
    """One-shot file ingest into a fresh capture database."""
    book = DutyCapture(path=db_path)
    return book.ingest_file(path, **kwargs)
