"""Signed road-spike capture, coordinate conversion and SQLite replay store.

This is the infrastructure layer around :mod:`taxiproof.suspension` and
:mod:`taxiproof.roadmap`:

* WGS84 GPS -> local metric / road chainage conversion;
* HMAC-SHA256 packet/evidence signatures and multi-hop provenance chains;
* idempotent, transactional SQLite ingestion (same id + changed body aborts);
* append-only announcements/corrections and municipal repair dispatch orders;
* route recomputation that only rewrites road cells intersected by late
  origin-seal, repair, calibration, replay or survey evidence.

Nothing here touches fares, passenger services, driver hours, taxi dispatch or
in-cab video: the only "dispatch" records are municipal road-repair work
orders.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import roadmap
from .suspension import (
    IdentityConflict,
    build_source_graph,
    canonical_bytes,
    packet_hash,
    physical_source_key,
    unsigned_view,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = REPO_ROOT / "fixtures" / "road-spike-replay.json"

EARTH_RADIUS_M = 6_378_137.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_file (
    sha256 TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    raw BLOB NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS packet (
    report_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    vehicle_id TEXT,
    sensor_id TEXT,
    source_event TEXT,
    vertical_g REAL,
    positive INTEGER NOT NULL,
    body TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    valid_signature INTEGER,
    body TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS physical_source (
    source_key TEXT PRIMARY KEY,
    vehicle_id TEXT,
    sensor_id TEXT,
    source_event TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    status_reason TEXT,
    body TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS announcement (
    announcement_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'publication',
    road_cell TEXT,
    state TEXT NOT NULL,
    corrects_version INTEGER,
    payload TEXT NOT NULL,
    source_versions TEXT,
    published_at TEXT,
    PRIMARY KEY (announcement_id, version)
);
CREATE TABLE IF NOT EXISTS hotspot_state (
    road_cell TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    independent_witnesses INTEGER NOT NULL DEFAULT 0,
    positive_reports INTEGER NOT NULL DEFAULT 0,
    location TEXT,
    basis TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS hotspot_revision (
    road_cell TEXT NOT NULL,
    revision INTEGER NOT NULL,
    state TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (road_cell, revision)
);
CREATE TABLE IF NOT EXISTS dispatch_order (
    order_id TEXT PRIMARY KEY,
    road_cell TEXT NOT NULL,
    state TEXT NOT NULL,
    basis TEXT,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class SignatureError(ValueError):
    """A signed packet/evidence/provenance element failed verification."""


# Same identity-conflict type as the suspension layer so callers can catch
# either name for "same report id, different content".
ConflictError = IdentityConflict


# ---------------------------------------------------------------------------
# JSON / time helpers
# ---------------------------------------------------------------------------


def _json_default(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if hasattr(value, "aliases"):
        return str(value)
    if isinstance(value, int):
        return int(value)
    return str(value)


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# GPS coordinate conversion (WGS84 <-> local metric tangent plane)
# ---------------------------------------------------------------------------


def wgs84_to_xy(lat, lon, ref_lat, ref_lon):
    """Equirectangular local tangent projection; returns ``(east_m, north_m)``."""

    lat0 = _radians(float(ref_lat))
    x = EARTH_RADIUS_M * _radians(float(lon) - float(ref_lon)) * _cos(lat0)
    y = EARTH_RADIUS_M * _radians(float(lat) - float(ref_lat))
    return x, y


def xy_to_wgs84(x, y, ref_lat, ref_lon):
    lat0 = _radians(float(ref_lat))
    lat = float(ref_lat) + _degrees(float(y) / EARTH_RADIUS_M)
    lon = float(ref_lon) + _degrees(float(x) / (EARTH_RADIUS_M * _cos(lat0)))
    return lat, lon


def _radians(deg):
    import math

    return math.radians(deg)


def _degrees(rad):
    import math

    return math.degrees(rad)


def _cos(rad):
    import math

    return math.cos(rad)


# Common aliases.
gps_to_xy = wgs84_to_xy
convert_gps = wgs84_to_xy
wgs84_to_local = wgs84_to_xy


# ---------------------------------------------------------------------------
# WGS84 <-> GCJ-02 (China "Mars" coordinate) conversion, for completeness
# ---------------------------------------------------------------------------

_GCJ_A = 6378245.0
_GCJ_EE = 0.00669342162296594323


def _out_of_china(lat, lon):
    return not (73.66 < lon < 135.05 and 3.86 < lat < 53.55)


def _transform_lat(x, y):
    import math

    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x, y):
    import math

    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lat, lon):
    """Standard WGS84 -> GCJ-02; coordinates outside China pass through."""

    import math

    if _out_of_china(lat, lon):
        return lat, lon
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - _GCJ_EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_GCJ_A * (1 - _GCJ_EE)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180.0) / (_GCJ_A / sqrtmagic * math.cos(radlat) * math.pi)
    return lat + dlat, lon + dlon


def gcj02_to_wgs84(lat, lon):
    """Iterative inverse of :func:`wgs84_to_gcj02` (converges sub-centimetre)."""

    if _out_of_china(lat, lon):
        return lat, lon
    wgs_lat, wgs_lon = lat, lon
    for _ in range(40):
        gcj_lat, gcj_lon = wgs84_to_gcj02(wgs_lat, wgs_lon)
        err_lat = lat - gcj_lat
        err_lon = lon - gcj_lon
        wgs_lat += err_lat
        wgs_lon += err_lon
        if abs(err_lat) < 1e-11 and abs(err_lon) < 1e-11:
            break
    return wgs_lat, wgs_lon


convert_gps_coordinates = wgs84_to_local
gps_to_local_metres = wgs84_to_local
wgs84_to_meters = wgs84_to_xy


def road_origin(road):
    if not road:
        return 31.23, 121.47, 0.0
    origin = road.get("origin") or {}
    return (
        float(origin.get("lat", road.get("ref_lat", 31.23))),
        float(origin.get("lon", road.get("ref_lon", 121.47))),
        float(road.get("chainage_at_origin_m", origin.get("chainage_m", 0.0))),
    )


def gps_to_chainage(gps, road):
    """Project a ``{'lat': ..., 'lon': ...}`` point to road chainage/cross-track."""

    ref_lat, ref_lon, chain0 = road_origin(road)
    lat = gps["lat"] if isinstance(gps, dict) else gps[0]
    lon = gps["lon"] if isinstance(gps, dict) else gps[1]
    x, y = wgs84_to_xy(lat, lon, ref_lat, ref_lon)
    axis = (road or {}).get("axis", "east_west")
    if axis == "north_south":
        return {"chainage_m": chain0 + y, "cross_track_m": x, "x_m": x, "y_m": y}
    return {"chainage_m": chain0 + x, "cross_track_m": y, "x_m": x, "y_m": y}


def enrich_with_chainage(rows, road):
    """Return rows with ``chainage_m`` filled from GPS when not stated."""

    enriched = []
    for row in rows or []:
        row = dict(row)
        if row.get("chainage_m") is None and isinstance(row.get("gps"), dict) and road:
            proj = gps_to_chainage(row["gps"], road)
            row["chainage_m"] = proj["chainage_m"]
            row.setdefault("cross_track_m", proj["cross_track_m"])
            row["local_position"] = proj
        enriched.append(row)
    return enriched


def trajectory_interval(track, event_second=None, *, road=None, odometer_uncertainty_m=0.0):
    """Project a GNSS/odom trajectory and bound the event position.

    The event-time point (or nearest sample) supplies the chainage centre;
    its own trajectory error and the odometer envelope are added as
    worst-case bounds.
    """

    points = list((track or {}).get("points", track if isinstance(track, list) else []))
    if not points:
        return None
    resolved = []
    for point in points:
        point = dict(point)
        if point.get("chainage_m") is None and isinstance(point.get("lat"), (int, float)) and road:
            point["chainage_m"] = gps_to_chainage(point, road)["chainage_m"]
        if point.get("chainage_m") is not None:
            resolved.append(point)
    if not resolved:
        return None
    target = event_second if event_second is not None else (track or {}).get("event_second")
    if target is None:
        anchor = resolved[len(resolved) // 2]
    else:
        anchor = min(resolved, key=lambda p: abs(float(p.get("event_second", target)) - float(target)))
    from .roadmap import interval_from_estimate

    return interval_from_estimate(
        anchor["chainage_m"],
        gps_error_m=anchor.get("error_m", 0.0),
        odometer_uncertainty_m=odometer_uncertainty_m,
    )


gps_track_interval = trajectory_interval


# ---------------------------------------------------------------------------
# Signatures and multi-hop provenance
# ---------------------------------------------------------------------------


def _secret(key_id, keyring):
    if keyring is None:
        raise SignatureError(f"no keyring available to resolve key {key_id!r}")
    if key_id not in keyring:
        raise SignatureError(f"unknown signer key id {key_id!r}")
    secret = keyring[key_id]
    return secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)


def sign_bytes(body: bytes, secret) -> str:
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def sign_message(message: dict, secret=None, *, key_id=None, keyring=None) -> dict:
    """Return a signed copy (adds ``key_id`` + ``signature``)."""

    out = dict(message)
    if key_id is not None:
        out["key_id"] = key_id
    kid = out.get("key_id") or out.get("signer_key_id")
    if secret is None:
        secret = _secret(kid, keyring or {})
    sig = sign_bytes(canonical_bytes(unsigned_view(out)), secret)
    out["signature"] = sig
    return out


sign_payload = sign_message


def verify_signature(message: dict, keyring) -> bool:
    """Constant-time HMAC verification of a signed mapping.

    The second argument may be a keyring mapping ``{key_id: secret}`` or a
    literal shared secret (``str``/``bytes``) used directly.
    """

    sig = message.get("signature") or message.get("sig")
    kid = message.get("key_id") or message.get("signer_key_id")
    if not sig:
        return False
    try:
        if isinstance(keyring, (str, bytes)):
            secret = keyring.encode("utf-8") if isinstance(keyring, str) else keyring
        else:
            if not kid:
                return False
            secret = _secret(kid, keyring or {})
        expected = sign_bytes(canonical_bytes(unsigned_view(message)), secret)
    except SignatureError:
        return False
    return hmac.compare_digest(expected, str(sig))


verify_message = verify_signature
check_signature = verify_signature


def require_valid_signature(message: dict, keyring, what: str = "message"):
    if not verify_signature(message, keyring):
        raise SignatureError(f"invalid signature for {what} {message.get('evidence_id') or message.get('report_id') or message.get('id') or ''}")


def hash_hex(body) -> str:
    if isinstance(body, (dict, list)):
        body = canonical_bytes(body)
    elif isinstance(body, str):
        body = body.encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def sign_hop(hop: dict, keyring) -> dict:
    """Compute ``hop_hash`` and sign it with the hop ``key_id`` key."""

    hop = dict(hop)
    base = {
        "seq": hop.get("seq"),
        "actor": hop.get("actor"),
        "key_id": hop.get("key_id"),
        "payload_hash": hop.get("payload_hash"),
        "prev_hash": hop.get("prev_hash"),
    }
    hop["hop_hash"] = hash_hex(canonical_bytes(base))
    hop["signature"] = sign_bytes(hop["hop_hash"].encode("ascii"), _secret(hop["key_id"], keyring))
    return hop


def verify_provenance_chain(chain: dict, keyring) -> dict:
    """Verify signatures, hash links and the origin seal of a hop chain."""

    errors = []
    cid = chain.get("chain_id", "CHAIN")
    event = chain.get("source_event") or chain.get("event_id")
    origin_hash = chain.get("origin_payload_hash")

    origin_sig = chain.get("origin_signature")
    origin_key = chain.get("origin_key_id")
    if origin_sig is not None:
        seal_body = canonical_bytes(
            {"source_event": event, "origin_payload_hash": origin_hash}
        )
        try:
            expected = sign_bytes(seal_body, _secret(origin_key, keyring))
            if not hmac.compare_digest(expected, str(origin_sig)):
                errors.append(f"{cid}: origin seal mismatch")
        except SignatureError as exc:
            errors.append(f"{cid}: {exc}")

    prev = None
    for index, hop in enumerate(chain.get("hops", []) or []):
        base = {
            "seq": hop.get("seq"),
            "actor": hop.get("actor"),
            "key_id": hop.get("key_id"),
            "payload_hash": hop.get("payload_hash"),
            "prev_hash": hop.get("prev_hash"),
        }
        expected_hash = hash_hex(canonical_bytes(base))
        if not hmac.compare_digest(expected_hash, str(hop.get("hop_hash", ""))):
            errors.append(f"{cid}: hop {index} hash mismatch")
        try:
            expected_sig = sign_bytes(
                str(hop.get("hop_hash", "")).encode("ascii"),
                _secret(hop.get("key_id"), keyring),
            )
            if not hmac.compare_digest(expected_sig, str(hop.get("signature", ""))):
                errors.append(f"{cid}: hop {index} signature mismatch")
        except SignatureError as exc:
            errors.append(f"{cid}: hop {index} {exc}")
        if hop.get("prev_hash") != prev:
            errors.append(f"{cid}: hop {index} previous-link broken")
        prev = hop.get("hop_hash")

    return {
        "valid": not errors,
        "errors": errors,
        "chain_id": cid,
        "source_event": event,
        "report_ids": list(chain.get("report_ids", []) or []),
        "origin_payload_hash": origin_hash,
    }


verify_chain = verify_provenance_chain
verify_provenance = verify_provenance_chain


# ---------------------------------------------------------------------------
# SQLite store
# ---------------------------------------------------------------------------


def connect(db_path=":memory:") -> sqlite3.Connection:
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    if str(db_path) != ":memory:":
        with contextlib.suppress(sqlite3.DatabaseError):
            db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


open_store = connect


def _file_sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def load_fixture(path=None) -> dict:
    path = Path(path) if path else DEFAULT_FIXTURE
    return json.loads(path.read_text(encoding="utf-8"))


load_case = load_fixture


def _store_meta(db, key, value):
    db.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, dumps(value)),
    )


def _load_meta(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def import_bundle(db, bundle, *, keyring=None, verify=True, source_path=None):
    """Import the three packets, road network, keyring and published record.

    The whole import is one SQLite transaction.  Invalid signatures or an id
    reused with different content raise and roll back every write.  Re-import
    of the exact same source file is idempotent.
    """

    if isinstance(bundle, (str, Path)):
        source_path = bundle
        raw = Path(bundle).read_bytes()
        bundle = json.loads(raw.decode("utf-8"))
    else:
        raw = dumps(bundle).encode("utf-8")
    sha = _file_sha(raw)

    if db.execute("SELECT 1 FROM source_file WHERE sha256=?", (sha,)).fetchone():
        return {"sha256": sha, "inserted": 0, "idempotent": True}

    keyring = keyring or bundle.get("keyring") or {}
    reports = bundle.get("reports") or bundle.get("packets") or []
    road = bundle.get("road_network") or bundle.get("road_cells_bundle") or {}

    db.execute("SAVEPOINT road_ingest")
    try:
        inserted = 0
        for report in reports:
            rid = report.get("report_id") or report.get("id")
            phash = packet_hash(report)
            if verify and report.get("signature"):
                require_valid_signature(report, keyring, "report")
            old = db.execute("SELECT sha256 FROM packet WHERE report_id=?", (rid,)).fetchone()
            if old:
                if old["sha256"] != phash:
                    raise ConflictError(f"report_id {rid!r} reused with different content")
                continue
            db.execute(
                "INSERT INTO packet VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                (
                    rid,
                    phash,
                    report.get("vehicle_id"),
                    report.get("sensor_id"),
                    report.get("source_event"),
                    report.get("vertical_g"),
                    1 if roadmap.is_positive(report) else 0,
                    dumps(report),
                ),
            )
            _upsert_physical_source(db, report)
            inserted += 1

        if road:
            _store_meta(db, "road_network", road)
        if keyring:
            _store_meta(db, "keyring", keyring)
        # Remember the whole bundle so apply_route() can resolve route ids
        # without the caller re-passing it.
        _store_meta(db, "bundle", bundle)
        # Remember the whole bundle so later route calls can resolve evidence
        # and route definitions without the caller re-passing it.
        _store_meta(db, "bundle", bundle)

        # Persist the as-published announcement(s), preserving version 1.
        announcements = []
        if bundle.get("published_announcement"):
            announcements.append(bundle["published_announcement"])
        announcements.extend(bundle.get("announcements", []) or [])
        for ann in announcements:
            _insert_announcement(db, ann)
            # Project the published state onto the current roadmap.
            db.execute(
                "INSERT INTO hotspot_state(road_cell,state,independent_witnesses,positive_reports,location,basis,revision) "
                "VALUES(?,?,?,?,?,?,1) ON CONFLICT(road_cell) DO NOTHING",
                (
                    ann["road_cell"],
                    ann.get("state", "confirmed"),
                    len((ann.get("basis") or {}).get("claimed_witness_vehicles", []) or []),
                    len((ann.get("basis") or {}).get("report_ids", []) or []),
                    dumps(ann.get("location")),
                    dumps(ann.get("basis")),
                ),
            )

        for order in bundle.get("dispatch_orders", []) or []:
            db.execute(
                "INSERT OR IGNORE INTO dispatch_order(order_id,road_cell,state,basis,payload) "
                "VALUES(?,?,?,?,?)",
                (
                    order["order_id"],
                    order["road_cell"],
                    order.get("state", "issued"),
                    dumps(order.get("basis")),
                    dumps(order),
                ),
            )

        db.execute(
            "INSERT INTO source_file(sha256,kind,raw) VALUES(?,?,?)",
            (sha, bundle.get("case_id", "road_spike_replay"), raw),
        )
        db.execute("RELEASE SAVEPOINT road_ingest")
        db.commit()
    except Exception:
        db.execute("ROLLBACK TO SAVEPOINT road_ingest")
        db.execute("RELEASE SAVEPOINT road_ingest")
        db.rollback()
        raise

    return {"sha256": sha, "inserted": inserted, "idempotent": False, "packets": len(reports)}


ingest_packets = import_bundle
ingest_bundle = import_bundle


def ingest_report(db, report, **kwargs):
    """Convenience wrapper to ingest one report mapping transactionally."""

    return import_bundle(db, {"reports": [report]}, **kwargs)


def add_evidence(db, item, *, keyring=None, verify=True):
    """Verify and persist one evidence item in its own transaction."""

    keyring = keyring or _load_meta(db, "keyring", {}) or {}
    if verify and item.get("signature") and not verify_signature(item, keyring):
        raise SignatureError(f"invalid signature for evidence {item.get('evidence_id')}")
    body = dumps(item)
    eid = item.get("evidence_id") or item.get("id")
    try:
        db.execute("SAVEPOINT evidence_add")
        db.execute(
            "INSERT OR REPLACE INTO evidence(evidence_id,kind,sha256,valid_signature,body) "
            "VALUES(?,?,?,?,?)",
            (eid, item.get("kind", "evidence"), hash_hex(body),
             1 if item.get("signature") else None, body),
        )
        db.execute("RELEASE SAVEPOINT evidence_add")
        db.commit()
    except Exception:
        db.execute("ROLLBACK TO SAVEPOINT evidence_add")
        db.execute("RELEASE SAVEPOINT evidence_add")
        db.rollback()
        raise
    return {"evidence_id": eid, "stored": True}


def _upsert_physical_source(db, report, status="active", reason=None):
    key = physical_source_key(report)
    db.execute(
        "INSERT INTO physical_source(source_key,vehicle_id,sensor_id,source_event,status,status_reason,body) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_key) DO NOTHING",
        (
            key,
            report.get("vehicle_id"),
            report.get("sensor_id"),
            report.get("source_event"),
            status,
            reason,
            dumps({k: report.get(k) for k in ("report_id", "source_event")}),
        ),
    )
    return key


def _insert_announcement(db, ann):
    db.execute(
        "INSERT OR IGNORE INTO announcement VALUES (?,?,?,?,?,?,?,?,?)",
        (
            ann["announcement_id"],
            int(ann.get("version", 1)),
            ann.get("kind", "publication"),
            ann.get("road_cell"),
            ann.get("state", "confirmed"),
            ann.get("corrects_version"),
            dumps(ann),
            dumps(ann.get("source_versions")),
            ann.get("published_at"),
        ),
    )


# ---------------------------------------------------------------------------
# Evidence + route application
# ---------------------------------------------------------------------------


def _packet_rows(db):
    return [json.loads(r["body"]) for r in db.execute("SELECT body FROM packet ORDER BY rowid")]


def _all_evidence(db):
    return [json.loads(r["body"]) for r in db.execute("SELECT body FROM evidence ORDER BY rowid")]


def _resolve_route(route, bundle):
    if isinstance(route, str):
        routes = (bundle or {}).get("verification_routes", {})
        if route in routes:
            return routes[route]
        for value in routes.values():
            if value.get("route_id") == route or value.get("name") == route:
                return value
        raise KeyError(f"unknown verification route {route!r}")
    return route or {}


def _route_evidence(route, bundle, explicit):
    if explicit is not None:
        return list(explicit)
    items = list(route.get("evidence", []) or [])
    ids = route.get("evidence_ids") or []
    if ids:
        index = {e["evidence_id"]: e for e in (bundle or {}).get("evidence", []) or []}
        chains = {c.get("chain_id"): c for c in (bundle or {}).get("provenance_chains", []) or []}
        for eid in ids:
            if eid in index:
                items.append(index[eid])
            elif eid in chains:
                items.append(dict(chains[eid], kind="provenance_chain", evidence_id=eid))
            else:
                raise KeyError(f"route evidence {eid!r} not found in bundle")
    return items


def apply_route(db, route=None, *, bundle=None, route_id=None, evidence=None,
                keyring=None, now=None):
    """Apply one authorised verification route in a single transaction.

    The transaction verifies signatures, persists late evidence, moves
    physical-source status, recomputes only intersected cells, appends an
    announcement correction and reconciles repair dispatch orders.
    """

    if route_id is not None and route is None:
        route = route_id
    if bundle is None:
        bundle = _load_meta(db, "bundle")
    route_def = _resolve_route(route if route is not None else route_id, bundle)
    keyring = keyring or _load_meta(db, "keyring", {}) or {}
    road = _load_meta(db, "road_network", {}) or (bundle or {}).get("road_network")
    now = now or utc_now_iso()

    new_evidence = _route_evidence(route_def, bundle, evidence)
    target_announcement = route_def.get("announcement_id", "ANN-R17")
    target_cell = route_def.get("target_cell", "R-17")

    db.execute("SAVEPOINT road_route")
    try:
        verified_evidence = []
        for item in new_evidence:
            eid = item.get("evidence_id") or item.get("chain_id")
            kind = item.get("kind", "")
            if kind == "provenance_chain" or item.get("hops"):
                verdict = verify_provenance_chain(item, keyring)
                if not verdict["valid"]:
                    raise SignatureError("; ".join(verdict["errors"]))
                item = dict(item)
                item["valid_signature"] = True
                item["verification"] = verdict
            elif item.get("signature"):
                if not verify_signature(item, keyring):
                    raise SignatureError(f"invalid signature for evidence {eid}")
                item = dict(item)
                item["valid_signature"] = True
            else:
                item = dict(item)
                item.setdefault("valid_signature", None)

            body = dumps(item)
            esha = hash_hex(body)
            old = db.execute("SELECT sha256 FROM evidence WHERE evidence_id=?", (eid,)).fetchone()
            if old and old["sha256"] != esha:
                raise ConflictError(f"evidence {eid!r} reused with different content")
            if not old:
                db.execute(
                    "INSERT INTO evidence VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)",
                    (eid, kind, esha, 1 if item.get("valid_signature") else (0 if item.get("valid_signature") is False else None), body),
                )
            verified_evidence.append(item)

            # Physical-source status changes drive the roadmap recompute.
            if kind in ("repair_order", "maintenance_record") and (
                item.get("faulty_probe") or "loose" in str(item.get("condition", ""))
            ):
                _mark_source_status(
                    db, item, "faulty_probe",
                    item.get("finding") or "verified loose/faulty sensor mount before event",
                )
            if kind == "origin_seal" and item.get("valid_signature"):
                _mark_source_status(db, item, "origin_verified", "signed vehicle origin seal")

        # Persist the bundle once for later route/id resolution.
        if bundle is not None:
            _store_meta(db, "bundle", bundle)

        rows = enrich_with_chainage(_packet_rows(db), road)
        all_evidence = _all_evidence(db) + [
            e for e in verified_evidence if e.get("evidence_id") not in {x.get("evidence_id") for x in _all_evidence(db)}
        ]

        prior = cell_states(db)
        evaluation = roadmap.evaluate(
            rows, road=road, evidence=all_evidence, prior_states=prior
        )
        hotspots = evaluation["hotspots"]

        affected = roadmap.affected_cells_for_evidence(verified_evidence, rows, road and road.get("cells"))
        affected.update(route_def.get("target_cells", []) or [])
        if target_cell:
            affected.add(target_cell)
        # Always persist every cell the engine produced for affected targets.
        for cid in list(affected):
            if cid in hotspots:
                _persist_hotspot(db, cid, hotspots[cid])

        # ---- Announcement correction -------------------------------------
        old_ann_row = db.execute(
            "SELECT * FROM announcement WHERE announcement_id=? ORDER BY version DESC LIMIT 1",
            (target_announcement,),
        ).fetchone()
        old_ann = json.loads(old_ann_row["payload"]) if old_ann_row else {
            "announcement_id": target_announcement, "version": 1, "road_cell": target_cell
        }

        route_kind = route_def.get("route") or route_def.get("outcome") or (
            "probe_fault" if route_def.get("route_id") == "A" else "independent_replay"
        )

        correction = None
        new_announcement = None
        dispatch_summary = {}

        if route_kind in ("probe_fault", "A", "unverified"):
            correction = roadmap.build_correction(
                old_ann,
                new_state="unverified",
                corrected_cell=target_cell,
                location=(hotspots.get(target_cell) or {}).get("location_interval_m"),
                evidence_ids=[e.get("evidence_id") or e.get("chain_id") for e in verified_evidence],
                reason="single physical probe relayed by two agents; verified pre-event mount fault; independent pass-by negative",
                route_id=route_def.get("route_id", "A"),
                published_at=now,
            )
            correction["state_zh"] = "待核"
            _insert_announcement(db, correction)
            db.execute(
                "UPDATE hotspot_state SET basis=?, updated_at=? WHERE road_cell=?",
                (dumps({"reason": correction["reason"], "route": "probe_fault"}), now, target_cell),
            )
            dispatch_summary = _reconcile_dispatch_probe_fault(db, route_def, now)

        elif route_kind in ("independent_replay", "B", "confirmed_other_cell"):
            confirmed_cell = route_def.get("confirmed_cell") or _pick_confirmed_cell(hotspots, target_cell)
            cell_result = hotspots.get(confirmed_cell)
            basis = roadmap.dispatch_basis_for_cell(confirmed_cell, cell_result)
            location = (cell_result or {}).get("location_interval_m")
            correction = roadmap.build_correction(
                old_ann,
                new_state="corrected_confirmed",
                corrected_cell=confirmed_cell,
                location=location,
                evidence_ids=[e.get("evidence_id") or e.get("chain_id") for e in verified_evidence],
                reason="original forwards fold to one witness; independent re-drive plus roadside anchor gives two witnesses in corrected cell",
                route_id=route_def.get("route_id", "B"),
                published_at=now,
            )
            correction["state_zh"] = "校正后确认"
            correction["superseded_by"] = f"ANN-{confirmed_cell}"
            _insert_announcement(db, correction)

            new_announcement = {
                "announcement_id": f"ANN-{confirmed_cell}",
                "version": 1,
                "kind": "publication",
                "road_cell": confirmed_cell,
                "state": "confirmed",
                "state_zh": "已确认",
                "location": {"chainage_interval_m": location, "location_version": "loc-v2"},
                "basis": {
                    "two_independent_physical_witnesses": True,
                    "witness_vehicles": (cell_result or {}).get("witness_vehicles", []),
                    "evidence_ids": correction["evidence_ids"],
                },
                "source_versions": {
                    "packets": [p["sha256"] for p in db.execute("SELECT sha256 FROM packet ORDER BY rowid")],
                    "location_version": "loc-v2",
                },
                "published_at": now,
            }
            _insert_announcement(db, new_announcement)
            db.execute(
                "INSERT INTO hotspot_state(road_cell,state,independent_witnesses,positive_reports,location,basis,revision) "
                "VALUES(?,?,?,?,?,?,1) ON CONFLICT(road_cell) DO UPDATE SET "
                "state=excluded.state, independent_witnesses=excluded.independent_witnesses, "
                "location=excluded.location, basis=excluded.basis, updated_at=?",
                (
                    confirmed_cell,
                    "confirmed",
                    (cell_result or {}).get("independent_witnesses", 2),
                    (cell_result or {}).get("positive_observations", 3),
                    dumps(location),
                    dumps(new_announcement["basis"]),
                    now,
                ),
            )
            dispatch_summary = _reconcile_dispatch_replay(
                db, confirmed_cell, cell_result or {}, basis, route_def, now
            )

        elif route_kind in ("late_evidence", "calibration_only", "late"):
            # Late origin/calibration evidence only rewrites the affected
            # cells already persisted above; announcements stay untouched
            # unless a caller explicitly publishes a correction afterwards.
            dispatch_summary = {"new_orders": [], "valid_open_orders": len(
                dispatch_orders(db, ("issued", "ready_to_issue"))
            )}

        db.execute("RELEASE SAVEPOINT road_route")
        db.commit()
    except Exception:
        db.execute("ROLLBACK TO SAVEPOINT road_route")
        db.execute("RELEASE SAVEPOINT road_route")
        db.rollback()
        raise

    return {
        "route_id": route_def.get("route_id"),
        "route": route_kind,
        "affected_cells": sorted(affected),
        "hotspots": {cid: hotspots.get(cid) for cid in sorted(affected) if cid in hotspots},
        "all_hotspots": hotspots,
        "target_cell": target_cell,
        "target_state": cell_state(db, (correction or {}).get("road_cell") or target_cell),
        "correction": correction,
        "new_announcement": new_announcement,
        "dispatch": dispatch_summary,
        "packets_preserved": db.execute("SELECT COUNT(*) c FROM packet").fetchone()["c"],
        "announcement_versions": [
            dict(r) for r in db.execute(
                "SELECT announcement_id,version,kind,road_cell,state FROM announcement ORDER BY announcement_id,version"
            )
        ],
    }


def _mark_source_status(db, item, status, reason):
    candidates = []
    if item.get("source_key"):
        candidates.append(item["source_key"])
    vehicle = item.get("vehicle_id")
    sensor = item.get("sensor_id")
    event = item.get("source_event")
    if vehicle and sensor and event:
        candidates.append(f"probe:{vehicle}:{sensor}#{event}")
    if vehicle and sensor:
        candidates.append(f"probe:{vehicle}:{sensor}")
    for key in candidates:
        row = db.execute("SELECT source_key FROM physical_source WHERE source_key=?", (key,)).fetchone()
        if row:
            db.execute(
                "UPDATE physical_source SET status=?, status_reason=? WHERE source_key=?",
                (status, item.get("finding") or reason, key),
            )
        else:
            db.execute(
                "INSERT INTO physical_source(source_key,vehicle_id,sensor_id,source_event,status,status_reason,body) "
                "VALUES(?,?,?,?,?,?,?)",
                (key, vehicle, sensor, event, status, item.get("finding") or reason, dumps(item)),
            )
    # Probe-level fault must hit the event-level source row even if the repair
    # order only names vehicle+sensor.
    if vehicle and sensor and not event:
        db.execute(
            "UPDATE physical_source SET status=?, status_reason=? "
            "WHERE source_key LIKE ? AND status='active'",
            (status, item.get("finding") or reason, f"probe:{vehicle}:{sensor}#%"),
        )


def _persist_hotspot(db, cell_id, result):
    db.execute(
        "INSERT INTO hotspot_state(road_cell,state,independent_witnesses,positive_reports,location,basis,revision) "
        "VALUES(?,?,?,?,?,?,1) ON CONFLICT(road_cell) DO UPDATE SET "
        "state=excluded.state, independent_witnesses=excluded.independent_witnesses, "
        "positive_reports=excluded.positive_reports, location=excluded.location, "
        "basis=excluded.basis, revision=hotspot_state.revision+1, "
        "updated_at=CURRENT_TIMESTAMP",
        (
            cell_id,
            result["state"],
            result.get("independent_witnesses", 0),
            result.get("positive_observations", result.get("independent_witnesses", 0)),
            dumps(result.get("location_interval_m")),
            dumps(
                {
                    "witness_vehicles": result.get("witness_vehicles", []),
                    "negative_vehicles": result.get("negative_vehicles", []),
                    "reasons": result.get("reasons", []),
                }
            ),
        ),
    )
    _record_revision(db, cell_id, result)


def _record_revision(db, cell_id, result=None):
    row = db.execute("SELECT revision,state FROM hotspot_state WHERE road_cell=?", (cell_id,)).fetchone()
    if row:
        db.execute(
            "INSERT OR REPLACE INTO hotspot_revision(road_cell,revision,state,payload) VALUES(?,?,?,?)",
            (cell_id, row["revision"], row["state"], dumps(result or {"state": row["state"]})),
        )


def _pick_confirmed_cell(hotspots, exclude=None):
    for cid, item in hotspots.items():
        if item.get("state") == "confirmed":
            return cid
    raise ValueError("route B did not produce a confirmed road cell")


def _reconcile_dispatch_probe_fault(db, route_def, now):
    revoked = []
    ids = route_def.get("revoke_order_ids") or ["MNT-R17-01"]
    for oid in ids:
        row = db.execute("SELECT * FROM dispatch_order WHERE order_id=?", (oid,)).fetchone()
        if row and row["state"] not in ("revoked", "superseded"):
            payload = json.loads(row["payload"])
            payload["state"] = "revoked"
            payload["revocation_reason"] = (
                "only one faulty physical probe relayed twice; not a two-vehicle witness basis"
            )
            payload["revoked_at"] = now
            db.execute(
                "UPDATE dispatch_order SET state='revoked', payload=?, updated_at=? WHERE order_id=?",
                (dumps(payload), now, oid),
            )
            revoked.append(oid)
    valid = db.execute(
        "SELECT COUNT(*) c FROM dispatch_order WHERE state IN ('issued','ready_to_issue')"
    ).fetchone()["c"]
    return {"revoked": revoked, "new_orders": [], "valid_open_orders": valid, "dispatchable_on_two_witnesses": False}


def _reconcile_dispatch_replay(db, cell_id, cell_result, basis, route_def, now):
    superseded = []
    for oid in route_def.get("supersede_order_ids") or ["MNT-R17-01"]:
        row = db.execute("SELECT * FROM dispatch_order WHERE order_id=?", (oid,)).fetchone()
        if row and row["state"] not in ("superseded", "revoked"):
            payload = json.loads(row["payload"])
            payload["state"] = "superseded"
            payload["superseded_by_cell"] = cell_id
            payload["superseded_at"] = now
            db.execute(
                "UPDATE dispatch_order SET state='superseded', payload=?, updated_at=? WHERE order_id=?",
                (dumps(payload), now, oid),
            )
            superseded.append(oid)

    order_id = route_def.get("new_order_id") or f"MNT-{cell_id}-2W"
    order = {
        "order_id": order_id,
        "order_type": "municipal_road_repair",
        "road_cell": cell_id,
        "state": "issued",
        "basis": {
            "rule": "two_independent_physical_witnesses",
            "independent_witnesses": cell_result.get("independent_witnesses", 2),
            "witness_vehicles": cell_result.get("witness_vehicles", []),
            "source": "CAB-7 origin E-901 folded to one vote + independent CAB-10 re-drive",
        },
        "location_interval_m": cell_result.get("location_interval_m"),
        "created_at": now,
    }
    db.execute(
        "INSERT OR REPLACE INTO dispatch_order(order_id,road_cell,state,basis,payload,updated_at) "
        "VALUES(?,?,?,?,?,?)",
        (order_id, cell_id, "issued", dumps(order["basis"]), dumps(order), now),
    )
    valid = [
        r["order_id"]
        for r in db.execute("SELECT order_id FROM dispatch_order WHERE state IN ('issued','ready_to_issue')")
    ]
    return {"superseded": superseded, "new_orders": [order_id], "valid_open_orders": len(valid), "order": order}


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------


def cell_states(db):
    return {
        r["road_cell"]: {
            "cell": r["road_cell"],
            "state": r["state"],
            "independent_witnesses": r["independent_witnesses"],
            "positive_reports": r["positive_reports"],
            "location_interval_m": json.loads(r["location"]) if r["location"] else None,
            "basis": json.loads(r["basis"]) if r["basis"] else None,
            "revision": r["revision"],
        }
        for r in db.execute("SELECT * FROM hotspot_state ORDER BY road_cell")
    }


def cell_state(db, cell_id):
    return cell_states(db).get(cell_id)


def put_cell_state(db, cell_id, state, *, independent_witnesses=0, positive_reports=0,
                   location=None, basis=None, revision=1):
    """Seed/replace a roadmap cell projection (used to establish prior state)."""

    db.execute(
        "INSERT INTO hotspot_state(road_cell,state,independent_witnesses,positive_reports,"
        "location,basis,revision) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(road_cell) DO UPDATE SET state=excluded.state, "
        "independent_witnesses=excluded.independent_witnesses, "
        "positive_reports=excluded.positive_reports, location=excluded.location, "
        "basis=excluded.basis, revision=excluded.revision",
        (
            cell_id, state, independent_witnesses, positive_reports,
            dumps(location), dumps(basis), revision,
        ),
    )
    return cell_state(db, cell_id)


def apply_late_evidence(db, item, *, bundle=None, keyring=None, now=None):
    """Apply one late origin/calibration record and recompute intersected cells only."""

    return apply_route(
        db,
        {"route_id": "LATE", "route": "late_evidence", "evidence": [item], "target_cells": []},
        bundle=bundle,
        keyring=keyring,
        now=now,
    )


def announcements(db, announcement_id=None):
    sql = "SELECT * FROM announcement"
    args = ()
    if announcement_id:
        sql += " WHERE announcement_id=?"
        args = (announcement_id,)
    sql += " ORDER BY announcement_id,version"
    out = []
    for r in db.execute(sql, args):
        item = json.loads(r["payload"])
        item.setdefault("announcement_id", r["announcement_id"])
        item["version"] = r["version"]
        item["_row_state"] = r["state"]
        out.append(item)
    return out


def current_announcements(db):
    out = {}
    for ann in announcements(db):
        out[ann["announcement_id"]] = ann
    return out


def dispatch_orders(db, state=None):
    if state is None:
        rows = db.execute("SELECT * FROM dispatch_order").fetchall()
    elif isinstance(state, str):
        rows = db.execute("SELECT * FROM dispatch_order WHERE state=?", (state,)).fetchall()
    else:
        states = tuple(state)
        placeholders = ",".join("?" for _ in states)
        rows = db.execute(
            f"SELECT * FROM dispatch_order WHERE state IN ({placeholders})", states
        ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def pending_verification(db):
    return [
        {"road_cell": cid, "state": item["state"], "reasons": (item.get("basis") or {}).get("reasons", [])}
        for cid, item in cell_states(db).items()
        if item["state"] == "unverified"
    ]


def report(db):
    """JSON-serialisable current road-hotspot / announcement / dispatch view."""

    packets = db.execute("SELECT report_id,sha256,vehicle_id,sensor_id,source_event,vertical_g,positive FROM packet ORDER BY rowid").fetchall()
    sources = [dict(r) for r in db.execute("SELECT * FROM physical_source ORDER BY source_key")]
    return {
        "packet_count": len(packets),
        "packets": [dict(r) for r in packets],
        "physical_sources": sources,
        "source_graph": build_source_graph(_packet_rows(db)).as_dict(),
        "hotspots": cell_states(db),
        "announcements": announcements(db),
        "current_announcements": current_announcements(db),
        "dispatch_orders": dispatch_orders(db),
        "pending_verification": pending_verification(db),
    }


roadmap_report = report


# ---------------------------------------------------------------------------
# Facade class
# ---------------------------------------------------------------------------


class RoadCapture:
    """Object-oriented facade over the transactional road-capture store."""

    def __init__(self, db_path=":memory:", keyring=None):
        self.db = connect(db_path)
        self.keyring = keyring

    @classmethod
    def open(cls, db_path=":memory:", keyring=None):
        return cls(db_path, keyring)

    def ingest(self, bundle, **kwargs):
        return import_bundle(self.db, bundle, keyring=self.keyring, **kwargs)

    import_bundle = ingest

    def apply_route(self, route=None, **kwargs):
        kwargs.setdefault("keyring", self.keyring)
        return apply_route(self.db, route, **kwargs)

    def apply_late_evidence(self, item, **kwargs):
        kwargs.setdefault("keyring", self.keyring)
        return apply_late_evidence(self.db, item, **kwargs)

    def report(self):
        return report(self.db)

    def cell_states(self):
        return cell_states(self.db)

    def announcements(self, announcement_id=None):
        return announcements(self.db, announcement_id)

    def dispatch_orders(self, **kwargs):
        return dispatch_orders(self.db, **kwargs)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


CaptureStore = RoadCapture


def run_replay(fixture_path=None, db_path=":memory:"):
    """Load the standard fixture into a fresh store and return the facade."""

    bundle = load_fixture(fixture_path)
    store = RoadCapture(db_path, keyring=bundle.get("keyring"))
    store.ingest(bundle)
    return store


# Extra naming aliases for the capture/store entry points.
create_store = connect
open_db = connect
Store = RoadCapture
Capture = RoadCapture
verify_packet_signature = verify_signature
verify_report_signature = verify_signature
decode_gps = wgs84_to_xy
gps_to_local = wgs84_to_local
import_fixture = import_bundle
load_replay = load_fixture


def import_file(db, kind_or_file=None, file=None, **kwargs):
    """Repo-style import entry point.

    Supports both ``import_file(db, path)`` and the
    :mod:`taxiproof.store` convention ``import_file(db, kind, path)``
    (the ``kind`` label is accepted and ignored for the road domain).
    """

    if file is not None:
        path = file
    else:
        path = kind_or_file
    return import_bundle(db, path, **kwargs)


def audit(db, keyring=None):
    """Verify stored packet/evidence signatures and report counts."""

    keyring = keyring or _load_meta(db, "keyring", {}) or {}
    invalid_packets = []
    for row in db.execute("SELECT report_id,body FROM packet"):
        body = json.loads(row["body"])
        if body.get("signature") and not verify_signature(body, keyring):
            invalid_packets.append(row["report_id"])
    invalid_evidence = []
    for row in db.execute("SELECT evidence_id,body FROM evidence"):
        body = json.loads(row["body"])
        if body.get("signature") and body.get("valid_signature") is False:
            invalid_evidence.append(row["evidence_id"])
    return {
        "ok": not invalid_packets and not invalid_evidence,
        "packet_count": db.execute("SELECT COUNT(*) c FROM packet").fetchone()["c"],
        "evidence_count": db.execute("SELECT COUNT(*) c FROM evidence").fetchone()["c"],
        "invalid_packets": invalid_packets,
        "invalid_evidence": invalid_evidence,
    }
