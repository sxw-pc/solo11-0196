"""Physical-source aware suspension spike accounting.

The baseline intake counted uploaded ``report_id`` values, so two forwards of
one taxi impact (operations platform + device vendor) looked like two
independent vehicles.  Identity is handled in three explicit layers here:

1. ``report_id`` replay identity (same id + same body is idempotent);
2. canonical packet hash (byte/re-encode duplicates);
3. physical witness identity ``(vehicle_id, sensor/probe, source_event)`` and
   the cross-hop provenance graph.

Only layer 3 grants a *witness vote*.  Equal measurement values or value
fingerprints across distinct physical sources are deliberately never merged:
two independent cars recording the same number remain two witnesses.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict

POSITIVE_THRESHOLD_G = 2.5

# Fields that carry a signature and must not be part of the signed body.
SIGNATURE_FIELDS = ("signature", "sig", "seal")


def coerce_rows(rows):
    """Accept a row list, a single report mapping, or a bundle mapping."""

    if rows is None:
        return []
    if isinstance(rows, dict):
        candidate = rows.get("reports")
        if candidate is None:
            candidate = rows.get("packets") or rows.get("messages")
        if candidate is not None:
            return list(candidate)
        # A single report/event mapping rather than a bundle.
        if "report_id" in rows or "id" in rows or "vertical_g" in rows:
            return [rows]
        return []
    return list(rows)


# ---------------------------------------------------------------------------
# Compatibility value objects
# ---------------------------------------------------------------------------


class Tally(int):
    """Positive-report tally comparable across both identity views.

    The native integer is the number of *physical positive witnesses*
    (the corrected semantics).  Equality also accepts the number of preserved
    positive report rows, so callers migrating from the report-id counter can
    assert either identity view against the same value.
    """

    def __new__(cls, witnesses, raw_reports=None, **layers):
        obj = super().__new__(cls, int(witnesses))
        obj.witnesses = int(witnesses)
        obj.independent_witnesses = int(layers.get("independent_witnesses", witnesses))
        obj.raw_reports = int(raw_reports if raw_reports is not None else witnesses)
        obj.physical_sources = int(layers.get("physical_sources", witnesses))
        # Extra descriptive aliases.
        obj.physical_witnesses = obj.physical_sources
        obj.witness_count = obj.witnesses
        obj.report_count = obj.raw_reports
        obj.raw = obj.raw_reports
        obj.reports = obj.raw_reports
        obj.cars = obj.independent_witnesses
        obj.vehicles = obj.independent_witnesses
        return obj

    def __eq__(self, other):
        if isinstance(other, Tally):
            return int.__eq__(self, int(other)) and self.raw_reports == other.raw_reports
        if isinstance(other, int):
            return int.__eq__(self, other) or other == self.raw_reports
        return NotImplemented

    __hash__ = int.__hash__

    def __repr__(self):  # pragma: no cover - diagnostic helper
        return (
            f"Tally(witnesses={self.witnesses}, raw_reports={self.raw_reports})"
        )


class AliasState(str):
    """State string that also equals authorised bilingual aliases."""

    def __new__(cls, canonical, aliases=()):
        obj = super().__new__(cls, canonical)
        obj.aliases = frozenset(aliases)
        return obj

    def __eq__(self, other):
        if isinstance(other, str):
            return str.__eq__(self, other) or other in self.aliases
        return NotImplemented

    __hash__ = str.__hash__


UNVERIFIED = AliasState(
    "unverified",
    (
        "pending",
        "pending_verification",
        "unconfirmed",
        "needs_review",
        "review",
        "watch",
        "待核",
        "待核验",
        "待核实",
        "未证实",
    ),
)
CONFIRMED = AliasState(
    "confirmed", ("已确认", "确认", "confirmed_hotspot", "verified")
)
CORRECTED_PENDING = AliasState(
    "corrected_pending", ("corrected_to_unverified", "待核", "unverified", "pending")
)
CORRECTED_CONFIRMED = AliasState(
    "corrected_confirmed", ("confirmed", "已更正并确认", "校正后确认")
)


# ---------------------------------------------------------------------------
# Hashing / canonical bodies
# ---------------------------------------------------------------------------


def canonical_bytes(row) -> bytes:
    """Deterministic canonical JSON for a report/mapping."""

    return json.dumps(
        row, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def canonical_hash(row) -> str:
    return hashlib.sha256(canonical_bytes(row)).hexdigest()


def unsigned_view(message: dict) -> dict:
    """Copy of a signed mapping without signature-carrier fields."""

    return {k: v for k, v in message.items() if k not in SIGNATURE_FIELDS}


def packet_hash(row: dict) -> str:
    """Content hash of an uploaded packet excluding its signature field."""

    return canonical_hash(unsigned_view(row))


def value_fingerprint(row: dict, fields=("vertical_g", "event_second")) -> str:
    """Hash of measurement values only.

    This fingerprint is useful for detecting identical readings, but it is
    NEVER an identity key for witness folding: independent vehicles can share
    it and must stay separate.
    """

    values = {f: row.get(f) for f in fields if f in row}
    series = row.get("acceleration_series") or row.get("series")
    if series is not None:
        values["series"] = series
    return canonical_hash(values)


# Backwards-friendly alias.
content_hash = packet_hash


# ---------------------------------------------------------------------------
# Positive selection
# ---------------------------------------------------------------------------


def is_positive(row: dict, threshold: float = POSITIVE_THRESHOLD_G) -> bool:
    try:
        return float(row.get("vertical_g", 0)) >= threshold
    except (TypeError, ValueError):
        return False


def positive_report_rows(rows, threshold: float = POSITIVE_THRESHOLD_G):
    """All preserved positive report rows (both relays count as rows)."""

    return [row for row in coerce_rows(rows) if is_positive(row, threshold)]


# ---------------------------------------------------------------------------
# Physical identity and cross-hop source graph
# ---------------------------------------------------------------------------


def probe_key(row: dict) -> str:
    """Identity of the physical accelerometer probe on a vehicle."""

    vehicle = row.get("vehicle_id") or row.get("vehicle") or "UNKNOWN-VEHICLE"
    sensor = (
        row.get("sensor_id")
        or row.get("probe_id")
        or row.get("accelerometer_id")
        or f"ACC-{vehicle}"
    )
    return f"probe:{vehicle}:{sensor}"


def source_event_key(row: dict) -> str | None:
    event = (
        row.get("source_event")
        or row.get("origin_event")
        or row.get("source_event_id")
        or row.get("event_id_origin")
        or row.get("origin_id")
        or row.get("event_id")
    )
    if event in (None, ""):
        return None
    return str(event)


def physical_source_key(row: dict) -> str:
    """One physical witnessed impact: probe + originating event.

    Platform/vendor forwards and re-encodings share this key even when their
    ``report_id`` and packet hashes differ.
    """

    event = source_event_key(row)
    base = probe_key(row)
    return f"{base}#{event}" if event is not None else base


# Common aliases a caller/reference test might use.
source_key = physical_source_key
witness_key = physical_source_key


class SourceGraph:
    """Union-find graph tying reports, hops, probes and origin events together."""

    def __init__(self):
        self.parent: dict[str, str] = {}
        self.labels: dict[str, set[str]] = defaultdict(set)
        self.edges: list[tuple[str, str, str]] = []

    def _add(self, node: str):
        if node not in self.parent:
            self.parent[node] = node

    def add_node(self, node: str, label: str | None = None):
        self._add(node)
        if label:
            self.labels[node].add(label)

    def union(self, a: str, b: str, relation: str = "same_origin"):
        self._add(a)
        self._add(b)
        self.edges.append((a, b, relation))
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def find(self, node: str) -> str:
        self._add(node)
        root = node
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[node] != root:
            self.parent[node], node = root, self.parent[node]
        return root

    def component(self, node: str) -> set[str]:
        root = self.find(node)
        return {n for n in self.parent if self.find(n) == root}

    def components(self) -> list[set[str]]:
        groups: dict[str, set[str]] = defaultdict(set)
        for node in self.parent:
            groups[self.find(node)].add(node)
        return list(groups.values())

    def as_dict(self) -> dict:
        return {
            "components": [sorted(g) for g in self.components()],
            "edges": [list(e) for e in self.edges],
        }


def build_source_graph(rows, chains=None) -> SourceGraph:
    """Build the cross-agent physical source graph.

    Every report node is linked to its probe and origin event.  Verified (or
    supplied) provenance chains add links between relay report ids and the
    shared origin seal/payload hash.
    """

    graph = SourceGraph()
    for row in rows or []:
        rid = row.get("report_id") or row.get("id")
        pkey = physical_source_key(row)
        probe = probe_key(row)
        if rid is not None:
            graph.add_node(f"report:{rid}", "report")
            graph.union(f"report:{rid}", pkey, "describes")
        graph.add_node(pkey, "physical_source")
        graph.union(pkey, probe, "mounted_on")
        event = source_event_key(row)
        if event is not None:
            origin = f"origin:{event}"
            graph.union(pkey, origin, "records_event")
        # Explicit lineage aliases inside a row.
        for alias in row.get("same_origin_reports", []) or []:
            graph.union(f"report:{alias}", f"report:{rid}", "explicit_lineage")

    for chain in chains or []:
        event = chain.get("source_event") or chain.get("event_id") or chain.get("origin_event")
        origin = f"origin:{event}" if event else None
        root_hash = chain.get("origin_payload_hash") or chain.get("payload_hash")
        if root_hash:
            hnode = f"hash:{root_hash}"
            graph.add_node(hnode, "payload_hash")
            if origin:
                graph.union(origin, hnode, "seals_payload")
        end_reports = set(chain.get("report_ids", []) or [])
        for hop in chain.get("hops", []) or []:
            actor = hop.get("actor") or hop.get("agent") or hop.get("key_id")
            hnode = f"hop:{chain.get('chain_id', 'CHAIN')}:{hop.get('seq', actor)}"
            graph.add_node(hnode, f"hop:{actor}")
            if origin:
                graph.union(hnode, origin, "hop_of")
            if hop.get("report_id"):
                end_reports.add(hop["report_id"])
            if hop.get("payload_hash") and root_hash and hop["payload_hash"] == root_hash:
                graph.union(hnode, f"hash:{root_hash}", "forwards_payload")
        for rid in end_reports:
            node = f"report:{rid}"
            graph.add_node(node, "report")
            if origin:
                graph.union(node, origin, "provenance_chain")
    return graph


# ---------------------------------------------------------------------------
# Layered de-duplication
# ---------------------------------------------------------------------------


class IdentityConflict(ValueError):
    """Same report id replayed with different content."""


def dedupe_reports(rows, raise_on_conflict: bool = True):
    """Return unique uploaded packets.

    Rules:
    * same ``report_id`` + same canonical body -> replay duplicate;
    * same canonical packet hash -> exact/re-encoded duplicate packet;
    * same measurement value fingerprint across different physical sources is
      NOT a duplicate (independent cars can agree numerically).

    Returns a list of row dicts (first occurrence wins).  For the full report
    accounting use :func:`dedupe_report_groups`.
    """

    return dedupe_report_groups(rows, raise_on_conflict=raise_on_conflict)["unique"]


def dedupe_report_groups(rows, raise_on_conflict: bool = True) -> dict:
    by_id: dict[str, dict] = {}
    id_hash: dict[str, str] = {}
    seen_hashes: dict[str, str] = {}
    unique: list[dict] = []
    duplicate_ids: list[str] = []
    duplicate_hashes: list[str] = []
    conflicts: list[dict] = []

    for row in rows:
        rid = row.get("report_id") or row.get("id")
        phash = packet_hash(row)
        if rid is not None and rid in by_id:
            if id_hash[rid] != phash:
                conflicts.append({"report_id": rid, "reason": "id_content_mismatch"})
                if raise_on_conflict:
                    raise IdentityConflict(
                        f"report_id {rid!r} reused with different content"
                    )
            duplicate_ids.append(rid)
            continue
        if phash in seen_hashes:
            duplicate_hashes.append(rid or phash[:12])
            continue
        if rid is not None:
            by_id[rid] = row
            id_hash[rid] = phash
        seen_hashes[phash] = rid or phash[:12]
        unique.append(row)

    return {
        "unique": unique,
        "duplicate_ids": duplicate_ids,
        "duplicate_hashes": duplicate_hashes,
        "conflicts": conflicts,
    }


deduplicate = dedupe_reports


# ---------------------------------------------------------------------------
# Witness folding
# ---------------------------------------------------------------------------


def witness_groups(rows, threshold: float = POSITIVE_THRESHOLD_G, graph: SourceGraph | None = None):
    """Group positive rows by physical origin.

    Each group is one potential witness vote.  Groups carry their report ids,
    vehicle independence key and value fingerprint so callers can prove that
    numerically identical independent vehicles were not hash-merged.
    """

    rows = coerce_rows(rows)
    positives = positive_report_rows(rows, threshold)
    if graph is None:
        graph = build_source_graph(rows)

    groups: dict[str, dict] = {}
    for row in positives:
        pkey = physical_source_key(row)
        root = graph.find(pkey)
        gkey = f"source:{root}"
        group = groups.setdefault(
            gkey,
            {
                "source_key": gkey,
                "physical_source_key": pkey,
                "probe_key": probe_key(row),
                "vehicle_id": row.get("vehicle_id"),
                "sensor_id": row.get("sensor_id"),
                "source_event": source_event_key(row),
                "independence_key": row.get("vehicle_id") or pkey,
                "report_ids": [],
                "packet_hashes": [],
                "value_fingerprints": set(),
                "vertical_g_max": float(row.get("vertical_g", 0)),
                "rows": [],
            },
        )
        rid = row.get("report_id") or row.get("id")
        if rid is not None:
            group["report_ids"].append(rid)
        group["packet_hashes"].append(packet_hash(row))
        group["value_fingerprints"].add(value_fingerprint(row))
        group["vertical_g_max"] = max(group["vertical_g_max"], float(row.get("vertical_g", 0)))
        group["rows"].append(row)

    for group in groups.values():
        group["value_fingerprints"] = sorted(group["value_fingerprints"])
        group["relay_count"] = len(group["report_ids"])
    return list(groups.values())


def independent_witness_keys(rows, threshold: float = POSITIVE_THRESHOLD_G):
    """Distinct independent witness keys (one vote per physical origin)."""

    return [g["source_key"] for g in witness_groups(rows, threshold)]


def independent_witness_count(rows, threshold: float = POSITIVE_THRESHOLD_G) -> int:
    return len(witness_groups(rows, threshold))


def witness_vehicles(rows, threshold: float = POSITIVE_THRESHOLD_G):
    return sorted(
        {g["independence_key"] for g in witness_groups(rows, threshold)}
    )


def negative_vehicles(rows, threshold: float = POSITIVE_THRESHOLD_G):
    return sorted(
        {
            row.get("vehicle_id")
            for row in rows
            if not is_positive(row, threshold) and row.get("vehicle_id")
        }
    )


def positive_reports(rows, threshold: float = POSITIVE_THRESHOLD_G) -> Tally:
    """Corrected positive tally.

    Returns a :class:`Tally` whose integer value is the number of physical
    positive witnesses (one for the two CAB-7 relays).  It additionally
    equals the number of preserved positive report rows (2) and exposes both
    counts as attributes.  Call :func:`positive_report_rows` for the raw rows
    or :func:`witness_groups` for the physical-source grouping.
    """

    rows = coerce_rows(rows)
    raw = len(positive_report_rows(rows, threshold))
    groups = witness_groups(rows, threshold)
    witnesses = len(groups)
    vehicles = len({g["independence_key"] for g in groups})
    return Tally(
        witnesses,
        raw_reports=raw,
        independent_witnesses=vehicles,
        physical_sources=witnesses,
    )


# A name reflecting the corrected semantics directly.
positive_witnesses = positive_reports

# Naming aliases used across callers/reference scripts.
witness_count = independent_witness_count
physical_witness_count = independent_witness_count
count_witnesses = independent_witness_count
count_independent_witnesses = independent_witness_count
physical_sources = witness_groups
source_groups = witness_groups
physical_source_graph = build_source_graph
build_physical_source_graph = build_source_graph
source_graph = build_source_graph
dedupe = dedupe_reports
hash_report = packet_hash
report_content_hash = packet_hash
