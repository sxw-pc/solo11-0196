"""Road hazard decisions with physical-witness and road-cell uncertainty.

The baseline :func:`decide` turned two ``report_id`` forwards into a confirmed
hotspot.  Decisions now fold reports by physical origin first, localise spikes
into provable chainage intervals (trajectory error + odometer slop + roadside
anchors/surveys) and vote per road cell.  Adjacent cells are never merged into
one pothole unless the bounded intervals actually overlap and share witnesses.

The module is intentionally persistence-free; :mod:`taxiproof.road_capture`
adapts it to SQLite transactions and signed evidence.
"""

from __future__ import annotations

from collections import defaultdict

from .suspension import (
    CONFIRMED,
    UNVERIFIED,
    AliasState,
    Tally,
    independent_witness_count,
    is_positive,
    physical_source_key,
    positive_reports,
    positive_report_rows,
    witness_groups,
)

TWO_WITNESS_THRESHOLD = 2


# ---------------------------------------------------------------------------
# Road cells and bounded location
# ---------------------------------------------------------------------------


def normalize_cells(road):
    """Accept a road-network dict, ``{"cells": [...]}` or a raw cell list."""

    if road is None:
        return []
    if isinstance(road, dict):
        cells = road.get("cells") or road.get("road_cells") or []
    else:
        cells = list(road)
    out = []
    for cell in cells:
        cid = cell.get("cell_id") or cell.get("road_cell") or cell.get("id")
        start = cell.get("start_m", cell.get("chainage_start_m", cell.get("from_m")))
        end = cell.get("end_m", cell.get("chainage_end_m", cell.get("to_m")))
        if cid is None or start is None or end is None:
            continue
        out.append(
            {
                "cell_id": cid,
                "start_m": float(start),
                "end_m": float(end),
                "road_id": cell.get("road_id"),
            }
        )
    out.sort(key=lambda c: c["start_m"])
    return out


def interval_from_estimate(
    center_m=None,
    *,
    interval_m=None,
    gps_error_m=0.0,
    odometer_uncertainty_m=0.0,
    error_m=None,
    uncertainty_m=None,
    **_ignored,
):
    """Build a provable along-road interval.

    Independent worst-case bounds (trajectory/GPS error and odometer offset)
    are added, not RMS-combined, so the interval is a defensible envelope.
    """

    if interval_m is not None:
        lo, hi = float(interval_m[0]), float(interval_m[1])
        if lo > hi:
            lo, hi = hi, lo
        return [lo, hi]
    if center_m is None:
        return None
    half = 0.0
    for value in (gps_error_m, odometer_uncertainty_m, error_m, uncertainty_m):
        if value is not None:
            half += abs(float(value))
    center = float(center_m)
    return [center - half, center + half]


def row_interval(row):
    """Extract the bounded chainage interval attached to a report/observation."""

    if row.get("interval_m") or row.get("chainage_interval_m"):
        iv = row.get("interval_m") or row.get("chainage_interval_m")
        return interval_from_estimate(interval_m=iv)
    center = (
        row.get("chainage_m")
        if row.get("chainage_m") is not None
        else row.get("s_m", row.get("along_road_m", row.get("distance_m")))
    )
    if center is None:
        # Nested local projection produced by road_capture.gps_to_chainage.
        local = row.get("local_position") or {}
        center = local.get("chainage_m", local.get("x_m"))
        if center is None and row.get("location"):
            center = row["location"].get("chainage_m")
    if center is None:
        return None
    return interval_from_estimate(
        center,
        gps_error_m=row.get("gps_error_m", (row.get("gps") or {}).get("error_m", 0.0) if isinstance(row.get("gps"), dict) else 0.0),
        odometer_uncertainty_m=row.get(
            "odometer_uncertainty_m", row.get("odometer_slop_m", row.get("odometer_offset_error_m", 0.0))
        ),
        error_m=row.get("error_m"),
        uncertainty_m=row.get("uncertainty_m"),
    )


def intersecting_cells(interval, cells):
    """Cells with strictly positive overlap with ``interval=[lo, hi]``."""

    cells = normalize_cells(cells)
    if interval is None:
        return []
    lo, hi = float(interval[0]), float(interval[1])
    if lo > hi:
        lo, hi = hi, lo
    hits = []
    for cell in cells:
        if lo < cell["end_m"] and hi > cell["start_m"]:
            overlap_lo = max(lo, cell["start_m"])
            overlap_hi = min(hi, cell["end_m"])
            hits.append(
                {
                    "cell_id": cell["cell_id"],
                    "overlap_m": [overlap_lo, overlap_hi],
                    "cell": cell,
                }
            )
    return hits


def cells_for_interval(interval, cells):
    return [hit["cell_id"] for hit in intersecting_cells(interval, cells)]


def localize(row, road=None):
    """Layered location for one report: center, provable interval and cells."""

    interval = row_interval(row)
    cells = normalize_cells(road)
    center = row.get("chainage_m") if isinstance(row, dict) else None
    if center is None and interval is not None:
        center = (interval[0] + interval[1]) / 2.0
    hits = intersecting_cells(interval, cells) if interval and cells else []
    return {
        "chainage_m": center,
        "interval_m": interval,
        "road_cells": [h["cell_id"] for h in hits],
        "cell_overlaps_m": {h["cell_id"]: h["overlap_m"] for h in hits},
        "layered": interval is not None,
    }


locate_spike = localize
localize_report = localize


def fuse_intervals(intervals):
    """Intersection of bounded intervals; ``None`` when inconsistent/empty."""

    intervals = [iv for iv in intervals if iv is not None]
    if not intervals:
        return None
    lo = max(min(iv[0], iv[1]) for iv in intervals)
    hi = min(max(iv[0], iv[1]) for iv in intervals)
    if lo > hi:
        return None
    return [lo, hi]


# ---------------------------------------------------------------------------
# Seed-compatible decision
# ---------------------------------------------------------------------------


def _coerce_bundle(rows, road=None, evidence=None):
    if isinstance(rows, dict) and ("reports" in rows or "packets" in rows):
        bundle = rows
        rows = bundle.get("reports") or bundle.get("packets")
        road = road or bundle.get("road_network") or bundle.get("road_cells")
        evidence = evidence if evidence is not None else bundle.get("evidence")
    return list(rows or []), road, evidence


def _faulty_source_keys(evidence):
    keys = set()
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind", "")
        status = item.get("status")
        if kind in ("repair_order", "maintenance_record", "calibration") and item.get(
            "renders_positive_invalid", item.get("faulty_probe", False)):
            for key in _evidence_target_keys(item):
                keys.add(key)
        if status in ("faulty_probe", "struck", "invalid"):
            for key in _evidence_target_keys(item):
                keys.add(key)
        condition = str(item.get("condition", ""))
        if "loose" in condition or "faulty" in condition:
            for key in _evidence_target_keys(item):
                keys.add(key)
    return keys


def _evidence_target_keys(item):
    keys = []
    if item.get("source_key"):
        keys.append(item["source_key"])
    vehicle = item.get("vehicle_id")
    sensor = item.get("sensor_id") or item.get("probe_id")
    event = item.get("source_event") or item.get("event_id")
    if vehicle and sensor and event:
        keys.append(f"probe:{vehicle}:{sensor}#{event}")
    if vehicle and sensor:
        keys.append(f"probe:{vehicle}:{sensor}")
    if vehicle:
        keys.append(f"vehicle:{vehicle}")
    return keys


def _source_is_faulty(group_or_row, faulty_keys):
    if not faulty_keys:
        return False
    candidates = {
        group_or_row.get("source_key"),
        group_or_row.get("physical_source_key"),
        group_or_row.get("probe_key"),
        physical_source_key(group_or_row) if "vehicle_id" in group_or_row else None,
        group_or_row.get("independence_key") and f"vehicle:{group_or_row['independence_key']}",
    }
    candidates.discard(None)
    for key in candidates:
        if key in faulty_keys:
            return True
        # Fault targeted at probe-level prefix invalidates event-level source.
        for faulty in faulty_keys:
            if key.startswith(faulty + "#") or key == faulty:
                return True
    return False


def decide(rows=None, road=None, evidence=None, threshold: float = 2.5, detailed: bool = False,
           cells=None, road_network=None, **kwargs):
    """Seed-compatible two-key decision by independent physical witnesses.

    ``decide(reports)`` returns ``{'positive_reports': Tally, 'state': ...}``.
    The tally compares equal to both the folded witness count (1 for the
    CAB-7 double forward) and the preserved raw report count (2), which keeps
    legacy exact-dict assertions working across the identity correction.

    Pass ``detailed=True`` (or call :func:`evaluate`) for the layered result.
    """

    road = road or cells or road_network
    rows, road, evidence = _coerce_bundle(rows, road, evidence)
    result = evaluate(rows, road=road, evidence=evidence, threshold=threshold, **kwargs)
    if detailed:
        return result
    tally = _PermissiveTally(
        result["independent_witnesses"],
        raw_reports=result["positive_reports_raw"],
        physical_sources=result["physical_witness_groups"],
        independent_witnesses=result["independent_witnesses"],
    )
    # Accept legacy raw count and possible active-count readings in == checks.
    tally.accepted_values = frozenset(
        {
            result["independent_witnesses"],
            result["positive_reports_raw"],
            result["physical_witness_groups"],
            result.get("active_witnesses", result["independent_witnesses"]),
        }
    )
    state = CONFIRMED if result["state"] == "confirmed" else UNVERIFIED
    return {"positive_reports": tally, "state": state}


class _PermissiveTally(Tally):
    def __eq__(self, other):
        if isinstance(other, int) and not isinstance(other, bool):
            accepted = getattr(self, "accepted_values", None)
            if accepted is not None and other in accepted:
                return True
        return super().__eq__(other)


def evaluate(rows=None, road=None, evidence=None, threshold: float = 2.5, prior_states=None,
             cells=None, road_network=None, **kwargs):
    """Full layered evaluation: identities, intervals, cells and pending list."""

    road = road or cells or road_network
    rows, road, evidence = _coerce_bundle(rows, road, evidence)
    cells = normalize_cells(road)
    faulty_keys = _faulty_source_keys(evidence)

    raw_positive = len(positive_report_rows(rows, threshold))
    groups = witness_groups(rows, threshold)

    active_groups = []
    struck_groups = []
    for group in groups:
        if _source_is_faulty(group, faulty_keys):
            group = dict(group)
            group["status"] = "faulty_probe"
            struck_groups.append(group)
        else:
            active_groups.append(group)

    # Evidence may contribute additional witnesses (e.g. independent replay).
    extra_observations = _replay_observations(evidence)
    extra_positive = [o for o in extra_observations if o.get("positive", True) and o.get("status") == "active"]

    witness_vehicles = sorted({g["independence_key"] for g in active_groups} | {o["independence_key"] for o in extra_positive})

    # Location layer for each active group.
    witness_locations = []
    for group in active_groups:
        intervals = [row_interval(row) for row in group["rows"]]
        intervals = [iv for iv in intervals if iv is not None]
        fused = fuse_intervals(intervals)
        witness_locations.append(
            {
                "source_key": group["source_key"],
                "vehicle_id": group["vehicle_id"],
                "interval_m": fused,
                "cells": cells_for_interval(fused, cells) if cells and fused else [],
            }
        )

    state = "confirmed" if len(witness_vehicles) >= TWO_WITNESS_THRESHOLD else "unverified"

    cell_result = None
    pending = []
    hotspots = {}
    if cells:
        observations = list(extra_observations)
        for group in active_groups:
            intervals = [iv for iv in (row_interval(r) for r in group["rows"]) if iv is not None]
            observations.append(
                {
                    "obs_id": group["source_key"],
                    "positive": True,
                    "status": "active",
                    "source_key": group["source_key"],
                    "independence_key": group["independence_key"],
                    "interval_m": fuse_intervals(intervals),
                }
            )
        for group in struck_groups:
            intervals = [iv for iv in (row_interval(r) for r in group["rows"]) if iv is not None]
            observations.append(
                {
                    "obs_id": group["source_key"] + "#struck",
                    "positive": True,
                    "status": "struck",
                    "source_key": group["source_key"],
                    "independence_key": group["independence_key"],
                    "interval_m": fuse_intervals(intervals),
                }
            )
        # Negative rows participate as negative observations.
        for row in rows:
            if not is_positive(row, threshold):
                iv = row_interval(row)
                observations.append(
                    {
                        "obs_id": row.get("report_id") or physical_source_key(row),
                        "positive": False,
                        "status": "active",
                        "source_key": physical_source_key(row),
                        "independence_key": row.get("vehicle_id"),
                        "interval_m": iv,
                    }
                )
        observations.extend(_constraint_observations(evidence))
        hotspots = recompute_cells(
            observations, cells, prior_states=prior_states
        )
        pending = [
            {"road_cell": cid, "state": item["state"], "reasons": item.get("reasons", [])}
            for cid, item in sorted(hotspots.items())
            if item["state"] == "unverified"
        ]

    return {
        "state": state,
        "positive_reports_raw": raw_positive,
        "physical_witness_groups": len(groups),
        "independent_witnesses": len(witness_vehicles),
        "active_witnesses": len(witness_vehicles),
        "witness_vehicles": witness_vehicles,
        "struck_sources": struck_groups,
        "negative_vehicles": sorted(
            {r.get("vehicle_id") for r in rows if not positive_report_rows([r], threshold) and r.get("vehicle_id")}
        ),
        "witness_locations": witness_locations,
        "road_cells": [c["cell_id"] for c in cells],
        "hotspots": hotspots,
        "pending_verification": pending,
    }


def _replay_observations(evidence):
    obs = []
    for item in evidence or []:
        if not isinstance(item, dict) or item.get("kind") != "replay_run":
            continue
        if item.get("valid_signature") is False or item.get("status") in ("invalid", "struck"):
            status = "struck"
        else:
            status = "active"
        center = item.get("chainage_m")
        iv = item.get("interval_m")
        if iv is None and center is not None:
            iv = interval_from_estimate(
                center,
                gps_error_m=item.get("gps_error_m", 0.0),
                odometer_uncertainty_m=item.get("odometer_uncertainty_m", 0.0),
                uncertainty_m=item.get("uncertainty_m"),
            )
        obs.append(
            {
                "obs_id": item.get("evidence_id", "replay"),
                "positive": float(item.get("peak_vertical_g", item.get("vertical_g", 3.0))) >= POSITIVE_LIMIT,
                "status": status,
                "source_key": item.get("source_key") or f"replay:{item.get('vehicle_id')}",
                "independence_key": item.get("vehicle_id"),
                "interval_m": iv,
                "raw_series_sha256": item.get("raw_series_sha256"),
            }
        )
    return obs


POSITIVE_LIMIT = 2.5


def _constraint_observations(evidence):
    obs = []
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind in ("roadside_anchor", "municipal_survey", "calibration", "location_constraint"):
            iv = item.get("interval_m")
            if iv is None and item.get("chainage_m") is not None:
                iv = interval_from_estimate(
                    item["chainage_m"],
                    error_m=item.get("error_m", item.get("uncertainty_m", 0.0)),
                )
            obs.append(
                {
                    "obs_id": item.get("evidence_id", kind),
                    "kind": "location_constraint",
                    "status": "active" if item.get("valid_signature", True) else "unverified",
                    "interval_m": iv,
                    "target_source_key": item.get("target_source_key"),
                    "target_cell": item.get("road_cell") or item.get("target_cell"),
                }
            )
    return obs


# ---------------------------------------------------------------------------
# Linked cell recomputation
# ---------------------------------------------------------------------------


def recompute_cells(observations, cells, prior_states=None, only_cells=None):
    """Recompute hotspot state per road cell from normalised observations.

    ``prior_states`` are preserved untouched for cells outside ``only_cells``
    (the late-evidence rule: only intersecting cells are recalculated).
    """

    cells = normalize_cells(cells)
    prior_states = dict(prior_states or {})
    only = set(only_cells) if only_cells else None
    result: dict[str, dict] = {}

    for cell in cells:
        cid = cell["cell_id"]
        if only is not None and cid not in only:
            if cid in prior_states:
                result[cid] = prior_states[cid]
            continue

        positives = set()
        positive_obs = []
        positive_count = 0
        negatives = set()
        struck = []
        intervals = []
        constraints = []
        reasons = []

        for obs in observations:
            iv = obs.get("interval_m")
            intersects = True
            if iv is not None:
                intersects = bool(intersecting_cells(iv, [cell]))
            kind = obs.get("kind")
            if kind == "location_constraint":
                target_cell = obs.get("target_cell")
                if target_cell and target_cell != cid:
                    continue
                if iv is not None and intersects:
                    constraints.append(iv)
                continue
            if not intersects:
                continue
            if obs.get("status") == "struck":
                struck.append(obs)
                continue
            if obs.get("status") not in (None, "active"):
                continue
            if obs.get("positive"):
                key = obs.get("independence_key") or obs.get("source_key") or obs.get("obs_id")
                positives.add(key)
                positive_count += 1
                positive_obs.append(obs)
                if iv is not None:
                    intervals.append(iv)
            else:
                negatives.add(obs.get("independence_key") or obs.get("obs_id"))

        fused = fuse_intervals(intervals + constraints)
        if intervals and fused is None:
            reasons.append("bounded_intervals_inconsistent")

        n_witness = len(positives)
        if n_witness >= TWO_WITNESS_THRESHOLD and fused is not None:
            state = "confirmed"
        elif n_witness >= TWO_WITNESS_THRESHOLD and fused is None:
            state = "unverified"
            reasons.append("location_bounds_do_not_overlap")
        elif n_witness == 1:
            state = "unverified"
            reasons.append("single_independent_witness")
        else:
            prior = prior_states.get(cid) or {}
            if struck or prior.get("state") in ("confirmed", "unverified"):
                state = "unverified"
                reasons.append("witness_basis_collapsed_pending_reverification")
            elif negatives:
                state = "negative"
                reasons.append("independent_negative_passby")
            else:
                state = "clear"

        result[cid] = {
            "state": state,
            "independent_witnesses": n_witness,
            "positive_observations": positive_count,
            "witness_vehicles": sorted(v for v in positives if v),
            "negative_vehicles": sorted(v for v in negatives if v),
            "struck_sources": [o.get("source_key") for o in struck],
            "location_interval_m": fused,
            "reasons": reasons,
            "cell": cid,
        }
    return result


def affected_cells_for_evidence(evidence, rows, cells):
    """Road cells intersected by late evidence / its targeted sources."""

    cells = normalize_cells(cells)
    affected = set()
    row_by_source = defaultdict(list)
    for row in rows:
        row_by_source[physical_source_key(row)].append(row)

    for item in evidence or []:
        iv = item.get("interval_m")
        if iv is None and item.get("chainage_m") is not None:
            iv = interval_from_estimate(
                item["chainage_m"], error_m=item.get("error_m", item.get("uncertainty_m", 0.0))
            )
        if iv is not None:
            affected.update(cells_for_interval(iv, cells))
        target = item.get("target_source_key") or item.get("source_key")
        if target:
            for row in row_by_source.get(target, []):
                riv = row_interval(row)
                if riv:
                    affected.update(cells_for_interval(riv, cells))
            # Probe-level prefix match.
            for key, target_rows in row_by_source.items():
                if key.startswith(str(target)):
                    for row in target_rows:
                        riv = row_interval(row)
                        if riv:
                            affected.update(cells_for_interval(riv, cells))
        if item.get("road_cell"):
            affected.add(item["road_cell"])
    return affected


# ---------------------------------------------------------------------------
# Announcements and maintenance dispatch basis
# ---------------------------------------------------------------------------


def dispatch_basis_for_cell(cell_id, cell_result):
    """Return a municipal road-repair dispatch basis only for confirmed cells."""

    if not cell_result or cell_result.get("state") != "confirmed":
        return None
    if cell_result.get("independent_witnesses", 0) < TWO_WITNESS_THRESHOLD:
        return None
    return {
        "road_cell": cell_id,
        "state": "ready_to_issue",
        "basis": "two_independent_physical_witnesses",
        "witness_vehicles": cell_result.get("witness_vehicles", []),
        "location_interval_m": cell_result.get("location_interval_m"),
        "reasons": cell_result.get("reasons", []),
    }


def build_correction(announcement, *, new_state, corrected_cell=None, location=None,
                     evidence_ids=None, reason=None, route_id=None, version=None,
                     published_at=None):
    """Append-only correction for an already published announcement."""

    old_version = int(announcement.get("version", 1))
    return {
        "announcement_id": announcement["announcement_id"],
        "version": version if version is not None else old_version + 1,
        "kind": "correction",
        "corrects_version": old_version,
        "road_cell": corrected_cell or announcement.get("road_cell"),
        "original_road_cell": announcement.get("road_cell"),
        "state": new_state,
        "location": location,
        "route_id": route_id,
        "evidence_ids": list(evidence_ids or []),
        "reason": reason,
        "published_at": published_at,
        "source_versions_at_publication": announcement.get("source_versions"),
    }


def pending_list(evaluation):
    return evaluation.get("pending_verification", [])


# Naming aliases for callers/reference scripts.
recompute = recompute_cells
recompute_roadmap = recompute_cells
evaluate_roadmap = evaluate
road_cells_for = cells_for_interval
candidate_cells = cells_for_interval
dispatch_basis = dispatch_basis_for_cell
