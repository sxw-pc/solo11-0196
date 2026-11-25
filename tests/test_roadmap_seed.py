"""Road spike replay: physical witnesses, layered cells and corrections.

These tests replace the baseline characterisation that counted two
``report_id`` forwards as two independent cars.  They verify:

* relay/re-encode votes from one physical probe fold to one witness
  (重票);
* independent vehicles reporting identical values stay two witnesses
  (独立同值不按哈希误并);
* GPS -> chainage conversion and provable road-cell intervals
  (路格定位 / 相邻路格不合并);
* packet/evidence signature and multi-hop chain verification;
* report_id/hash de-duplication and transactional SQLite rollback;
* two authorised verification routes from the same three packets and the
  same published R-17 announcement, with divergent hotspot/dispatch outcomes;
* append-only old-announcement preservation and intersecting-cell-only
  recomputation for late calibration.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from taxiproof.road_capture import (
    RoadCapture,
    SignatureError,
    apply_late_evidence,
    connect,
    gps_to_chainage,
    load_fixture,
    put_cell_state,
    verify_provenance_chain,
    verify_signature,
    wgs84_to_xy,
)
from taxiproof.roadmap import (
    decide,
    evaluate,
    intersecting_cells,
    localize,
    recompute_cells,
)
from taxiproof.suspension import (
    IdentityConflict,
    dedupe_report_groups,
    dedupe_reports,
    independent_witness_count,
    physical_source_key,
    positive_reports,
    positive_report_rows,
    value_fingerprint,
    witness_groups,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "road-spike-replay.json"


@pytest.fixture(scope="module")
def bundle():
    return load_fixture(FIXTURE)


@pytest.fixture()
def store(bundle):
    cap = RoadCapture(":memory:", keyring=bundle["keyring"])
    result = cap.ingest(copy.deepcopy(bundle))
    assert result["inserted"] == 3
    yield cap
    cap.close()


# ---------------------------------------------------------------------------
# Starting point: the same three packets the baseline miscounted
# ---------------------------------------------------------------------------


def test_fixture_starts_from_the_same_three_packets(bundle):
    rows = bundle["reports"]
    assert [r["report_id"] for r in rows] == ["FLEET-41", "VENDOR-882", "CAB-9-NEG"]
    # Both positive packets are preserved as raw rows ...
    assert positive_reports(rows).raw_reports == 2
    assert len(positive_report_rows(rows)) == 2
    assert {r["sensor_id"] for r in rows if r["vertical_g"] >= 2.5} == {"ACC-7"}


# ---------------------------------------------------------------------------
# Hard constraint 1a: relay / re-encode of one physical probe = one witness
# ---------------------------------------------------------------------------


def test_relayed_vehicle_spike_counts_one_physical_witness(bundle):
    rows = bundle["reports"]

    # Two preserved positive packets ...
    assert len(positive_report_rows(rows)) == 2

    # ... but exactly one physical origin (CAB-7 / ACC-7 / E-901).
    fleet, vendor, _negative = rows
    assert physical_source_key(fleet) == physical_source_key(vendor)
    assert fleet["source_event"] == vendor["source_event"] == "E-901"

    tally = positive_reports(rows)
    assert int(tally) == 1
    assert tally.witnesses == 1
    assert tally.raw_reports == 2
    assert independent_witness_count(rows) == 1

    groups = witness_groups(rows)
    assert len(groups) == 1
    assert groups[0]["vehicle_id"] == "CAB-7"
    assert groups[0]["report_ids"] == ["FLEET-41", "VENDOR-882"]

    # The seed-compatible decision is no longer "confirmed" on two report ids.
    assert decide(rows) == {"positive_reports": 1, "state": "unverified"}


def test_decide_accepts_the_whole_bundle(bundle):
    assert decide(bundle)["state"] == "unverified"
    detailed = decide(bundle, detailed=True)
    assert detailed["independent_witnesses"] == 1
    assert detailed["negative_vehicles"] == ["CAB-9"]


def test_cross_agent_source_graph_unions_the_two_forwards(bundle):
    from taxiproof.suspension import build_source_graph

    graph = build_source_graph(bundle["reports"], bundle["provenance_chains"])

    def component_of(node):
        return graph.find(node)

    # Both agent report nodes and the E-901 origin live in one component.
    assert component_of("report:FLEET-41") == component_of("report:VENDOR-882")
    assert component_of("report:FLEET-41") == component_of("origin:E-901")
    # CAB-9 is a separate physical origin.
    assert component_of("report:CAB-9-NEG") != component_of("report:FLEET-41")
    assert component_of("report:CAB-9-NEG") == component_of("origin:E-110")


# ---------------------------------------------------------------------------
# Hard constraint 1b: independent cars with identical values are not merged
# ---------------------------------------------------------------------------


def test_independent_identical_values_remain_two_witnesses():
    a = {
        "report_id": "CAR-A-1",
        "vehicle_id": "CAB-A",
        "sensor_id": "ACC-A",
        "source_event": "EA-1",
        "vertical_g": 2.8,
        "event_second": 500,
    }
    b = copy.deepcopy(a)
    b.update(
        report_id="CAR-B-1",
        vehicle_id="CAB-B",
        sensor_id="ACC-B",
        source_event="EB-1",
    )

    # The measurement-value hash is identical ...
    assert value_fingerprint(a) == value_fingerprint(b)

    # ... but packet identity and physical origin differ, so no hash merge.
    assert physical_source_key(a) != physical_source_key(b)
    assert len(dedupe_reports([a, b])) == 2
    groups = witness_groups([a, b])
    assert len(groups) == 2
    assert independent_witness_count([a, b]) == 2
    assert decide([a, b])["state"] == "confirmed"


# ---------------------------------------------------------------------------
# report_id / hash de-duplication layers
# ---------------------------------------------------------------------------


def test_report_id_replay_and_conflict():
    rows = [
        {"report_id": "D1", "vehicle_id": "C1", "vertical_g": 2.8},
        {"report_id": "D2", "vehicle_id": "C2", "vertical_g": 0.3},
    ]
    replayed = rows + [copy.deepcopy(rows[0])]
    result_groups = dedupe_report_groups(replayed)
    assert len(result_groups["unique"]) == 2
    assert result_groups["duplicate_ids"] == ["D1"]

    changed = copy.deepcopy(rows)
    changed[0]["vertical_g"] = 9.9
    with pytest.raises(IdentityConflict):
        dedupe_reports([rows[0], changed[0]])


def test_exact_hash_duplicate_without_report_id_collapses_but_distinct_cars_do_not():
    packet = {"vehicle_id": "C1", "sensor_id": "A1", "vertical_g": 3.1}
    idless_replay = [copy.deepcopy(packet), copy.deepcopy(packet)]
    assert len(dedupe_reports(idless_replay)) == 1

    other_car = copy.deepcopy(packet)
    other_car["vehicle_id"] = "C2"
    assert len(dedupe_reports([packet, other_car])) == 2


# ---------------------------------------------------------------------------
# Signature + provenance chain verification
# ---------------------------------------------------------------------------


def test_all_fixture_packets_and_chains_verify(bundle):
    keyring = bundle["keyring"]
    for report in bundle["reports"]:
        assert verify_signature(report, keyring), report["report_id"]
    for evidence in bundle["evidence"]:
        if evidence.get("signature"):
            assert verify_signature(evidence, keyring), evidence["evidence_id"]
    for chain in bundle["provenance_chains"]:
        verdict = verify_provenance_chain(chain, keyring)
        assert verdict["valid"], verdict["errors"]


def test_tampered_packet_and_broken_chain_fail(bundle):
    keyring = bundle["keyring"]
    tampered = copy.deepcopy(bundle["reports"][0])
    tampered["vertical_g"] = 9.6
    assert not verify_signature(tampered, keyring)

    bad_chain = copy.deepcopy(bundle["provenance_chains"][0])
    bad_chain["hops"][-1]["actor"] = "attacker-relay"
    assert not verify_provenance_chain(bad_chain, keyring)["valid"]

    bad_sig_chain = copy.deepcopy(bundle["provenance_chains"][0])
    bad_sig_chain["hops"][0]["signature"] = "0" * 64
    assert not verify_provenance_chain(bad_sig_chain, keyring)["valid"]


# ---------------------------------------------------------------------------
# GPS conversion and layered road-cell uncertainty
# ---------------------------------------------------------------------------


def test_gps_conversion_matches_stated_chainage(bundle):
    road = bundle["road_network"]
    for report in bundle["reports"]:
        projected = gps_to_chainage(report["gps"], road)
        assert projected["chainage_m"] == pytest.approx(report["chainage_m"], abs=0.01)

    # 0.001 degree of latitude is about 111 m on the local tangent plane.
    north = wgs84_to_xy(31.231, 121.47, 31.23, 121.47)[1]
    assert 110.0 < north < 112.5


def test_spike_gets_provable_interval_and_candidate_cells(bundle):
    road = bundle["road_network"]
    fleet = bundle["reports"][0]
    located = localize(fleet, road)
    # 336 +/- (6 m trajectory + 4 m odometer envelope) = [326, 346].
    assert located["interval_m"] == [326.0, 346.0]
    assert located["road_cells"] == ["R-17", "R-18"]

    cab9 = localize(bundle["reports"][2], road)
    assert cab9["road_cells"] == ["R-17"]

    # The old announcement only kept a point; the layered view keeps bounds.
    assert bundle["published_announcement"]["location"]["layered"] is False


def test_gps_trajectory_event_point_bounds_interval(bundle):
    from taxiproof.road_capture import trajectory_interval

    road = bundle["road_network"]
    cab7_track = next(t for t in bundle["gps_trajectories"] if t["vehicle_id"] == "CAB-7")
    assert trajectory_interval(cab7_track, 500, road=road, odometer_uncertainty_m=4.0) == [326.0, 346.0]


def test_adjacent_cell_spikes_are_not_merged_into_one_pothole(bundle):
    cells = bundle["road_network"]["cells"]
    observations = [
        {"positive": True, "status": "active", "independence_key": "CAB-A",
         "source_key": "sA", "interval_m": [329.0, 331.0]},   # tight in R-17
        {"positive": True, "status": "active", "independence_key": "CAB-B",
         "source_key": "sB", "interval_m": [349.0, 351.0]},   # tight in R-18
    ]
    states = recompute_cells(observations, cells)
    assert states["R-17"]["state"] == "unverified"
    assert states["R-17"]["independent_witnesses"] == 1
    assert states["R-18"]["state"] == "unverified"
    assert states["R-18"]["independent_witnesses"] == 1
    assert all(cell["state"] != "confirmed" for cell in states.values())

    # A broad envelope may legitimately intersect two cells, but only as a
    # candidate range - it must not be pinned to one point.
    hits = intersecting_cells([326.0, 346.0], cells)
    assert [h["cell_id"] for h in hits] == ["R-17", "R-18"]


# ---------------------------------------------------------------------------
# SQLite transactions: idempotent ingest and atomic rollback
# ---------------------------------------------------------------------------


def test_sqlite_ingest_is_idempotent_and_preserves_raw_fixture(tmp_path, bundle):
    db_path = tmp_path / "road.sqlite"
    cap = RoadCapture(db_path, keyring=bundle["keyring"])
    first = cap.ingest(copy.deepcopy(bundle))
    assert first["inserted"] == 3
    second = cap.ingest(copy.deepcopy(bundle))
    assert second["idempotent"] is True
    assert cap.db.execute("SELECT COUNT(*) c FROM packet").fetchone()["c"] == 3
    cap.close()

    # Ingesting the fixture path into a fresh store preserves its raw bytes.
    raw = FIXTURE.read_bytes()
    cap2 = RoadCapture(":memory:", keyring=bundle["keyring"])
    cap2.ingest(str(FIXTURE))
    stored = cap2.db.execute("SELECT raw FROM source_file LIMIT 1").fetchone()["raw"]
    assert bytes(stored) == raw
    assert cap2.report()["packet_count"] == 3
    cap2.close()


def test_invalid_signature_rolls_back_entire_ingest(bundle):
    cap = RoadCapture(":memory:", keyring=bundle["keyring"])
    bad = copy.deepcopy(bundle)
    bad["reports"][0]["vertical_g"] = 9.9  # body changed after signing
    with pytest.raises(SignatureError):
        cap.ingest(bad)
    assert cap.db.execute("SELECT COUNT(*) c FROM packet").fetchone()["c"] == 0
    assert cap.db.execute("SELECT COUNT(*) c FROM source_file").fetchone()["c"] == 0
    # connection/transaction remains usable after rollback
    assert cap.db.execute("SELECT 1").fetchone()[0] == 1
    cap.close()


def test_same_report_id_changed_body_rolls_back(store, bundle):
    from taxiproof.road_capture import sign_message

    changed_report = copy.deepcopy(bundle["reports"][0])
    changed_report["vertical_g"] = 4.4
    # Re-sign so signature verification passes and the id-content collision
    # against the already stored packet is what aborts the transaction.
    changed_report = sign_message(changed_report, keyring=bundle["keyring"])
    changed_bundle = {"case_id": "changed-collision-bundle", "reports": [changed_report]}
    with pytest.raises(IdentityConflict):
        store.ingest(changed_bundle)
    row = store.db.execute(
        "SELECT vertical_g FROM packet WHERE report_id='FLEET-41'"
    ).fetchone()
    assert row["vertical_g"] == 2.8


# ---------------------------------------------------------------------------
# Route A: signed origin seal + pre-event repair order -> pending, no dispatch
# ---------------------------------------------------------------------------


def test_route_a_faulty_probe_corrects_r17_to_pending_and_revokes_dispatch(store, bundle):
    result = store.apply_route(
        "A_probe_fault", bundle=bundle, now="2026-11-21T09:00+08:00"
    )

    # Physical source is marked faulty - this drives the roadmap transition.
    source = store.db.execute(
        "SELECT status FROM physical_source WHERE source_key=?",
        ("probe:CAB-7:ACC-7#E-901",),
    ).fetchone()
    assert source["status"] == "faulty_probe"

    r17 = store.cell_states()["R-17"]
    assert r17["state"] == "unverified"
    assert result["target_state"]["state"] == "unverified"

    # Old announcement v1 confirmed is preserved; v2 is the pending correction.
    versions = store.announcements("ANN-R17")
    assert [(v["version"], v["kind"]) for v in versions] == [(1, "publication"), (2, "correction")]
    assert versions[0]["state"] == "confirmed"
    assert versions[0]["source_versions"]["location_version"] == "loc-v1-point"
    correction = versions[-1]
    assert correction["state"] == "unverified"
    assert correction["state_zh"] == "待核"
    assert correction["corrects_version"] == 1

    # The repair task cannot be dispatched on a two-car witness claim.
    orders = {o["order_id"]: o for o in store.dispatch_orders()}
    assert orders["MNT-R17-01"]["state"] == "revoked"
    assert store.dispatch_orders(state=("issued", "ready_to_issue")) == []
    assert result["dispatch"]["dispatchable_on_two_witnesses"] is False

    pending_cells = {p["road_cell"] for p in store.report()["pending_verification"]}
    assert "R-17" in pending_cells

    # Original three packets are still there.
    assert store.report()["packet_count"] == 3


# ---------------------------------------------------------------------------
# Route B: independent CAB-10 re-drive + anchor -> confirmed corrected R-18
# ---------------------------------------------------------------------------


def test_route_b_independent_replay_confirms_corrected_r18_and_dispatches(store, bundle):
    result = store.apply_route(
        "B_independent_replay", bundle=bundle, now="2026-11-21T11:00+08:00"
    )

    states = store.cell_states()
    assert states["R-18"]["state"] == "confirmed"
    assert states["R-18"]["independent_witnesses"] == 2
    assert set(states["R-18"]["basis"]["witness_vehicles"]) == {"CAB-7", "CAB-10"}
    # Tight municipal/anchor bounds publish a provable corrected interval.
    lo, hi = states["R-18"]["location_interval_m"]
    assert 344.0 <= lo and hi <= 345.2

    # R-17 falls back to a single broad witness and stays unverified.
    assert states["R-17"]["state"] == "unverified"
    assert states["R-17"]["independent_witnesses"] == 1

    correction = [a for a in store.announcements("ANN-R17") if a["version"] == 2][0]
    assert correction["road_cell"] == "R-18"
    assert correction["state"] == "corrected_confirmed"

    new_ann = store.announcements("ANN-R-18")[-1]
    assert new_ann["state"] == "confirmed"
    assert new_ann["location"]["location_version"] == "loc-v2"

    orders = {o["order_id"]: o for o in store.dispatch_orders()}
    assert orders["MNT-R17-01"]["state"] == "superseded"
    fresh = orders["MNT-R18-CAB7-CAB10"]
    assert fresh["state"] == "issued"
    assert fresh["road_cell"] == "R-18"
    assert set(fresh["basis"]["witness_vehicles"]) == {"CAB-7", "CAB-10"}
    assert result["dispatch"]["new_orders"] == ["MNT-R18-CAB7-CAB10"]

    # Original packets and old confirmed announcement survive.
    assert store.report()["packet_count"] == 3
    v1 = store.announcements("ANN-R17")[0]
    assert v1["version"] == 1 and v1["state"] == "confirmed"


# ---------------------------------------------------------------------------
# The two authorised routes diverge from the identical starting point
# ---------------------------------------------------------------------------


def test_two_routes_produce_different_current_map_and_dispatch(bundle):
    outcomes = {}
    for route in ("A_probe_fault", "B_independent_replay"):
        cap = RoadCapture(":memory:", keyring=bundle["keyring"])
        cap.ingest(copy.deepcopy(bundle))
        res = cap.apply_route(route, bundle=bundle, now="2026-11-21T12:00+08:00")
        outcomes[route] = (res, cap.report())
        cap.close()

    res_a, rep_a = outcomes["A_probe_fault"]
    res_b, rep_b = outcomes["B_independent_replay"]

    assert rep_a["hotspots"]["R-17"]["state"] == "unverified"
    assert "R-18" not in rep_a["hotspots"] or rep_a["hotspots"]["R-18"]["state"] != "confirmed"
    assert rep_a["dispatch_orders"] and all(o["state"] == "revoked" for o in rep_a["dispatch_orders"])

    assert rep_b["hotspots"]["R-18"]["state"] == "confirmed"
    assert any(o["order_id"] == "MNT-R18-CAB7-CAB10" and o["state"] == "issued"
               for o in rep_b["dispatch_orders"])

    # Both routes retain all three original packets and ANN-R17 v1+v2.
    for _res, rep in outcomes.values():
        assert rep["packet_count"] == 3
        ann17 = [a for a in rep["announcements"] if a["announcement_id"] == "ANN-R17"]
        assert [a["version"] for a in ann17] == [1, 2]
        assert ann17[0]["state"] == "confirmed"


# ---------------------------------------------------------------------------
# Hard constraint 3: late calibration recomputes only intersecting cells
# ---------------------------------------------------------------------------


def test_late_calibration_only_recomputes_intersecting_cells(store, bundle):
    for cid in ("R-16", "R-17", "R-18"):
        put_cell_state(store.db, cid, "unverified", independent_witnesses=1, revision=1)

    before = store.cell_states()
    result = apply_late_evidence(
        store.db, bundle["late_evidence"][0], bundle=bundle,
        now="2026-11-22T10:00+08:00",
    )
    assert set(result["affected_cells"]) == {"R-17", "R-18"}

    after = store.cell_states()
    assert after["R-16"]["revision"] == before["R-16"]["revision"] == 1
    assert after["R-17"]["revision"] == 2
    assert after["R-18"]["revision"] == 2

    # The late calibration narrows R-17's provable interval inside the cell.
    lo, hi = after["R-17"]["location_interval_m"]
    assert [lo, hi] == [335.2, 339.2]

    # No silent replacement/addition of announcements by a calibration record.
    assert len(store.announcements()) == 1
    assert store.announcements("ANN-R17")[0]["state"] == "confirmed"


def test_pure_only_cells_preserves_untouched_prior_state(bundle):
    cells = bundle["road_network"]["cells"]
    prior = {
        "R-16": {"cell": "R-16", "state": "clear", "revision": 7},
        "R-17": {"cell": "R-17", "state": "unverified", "revision": 3},
    }
    observations = [{"positive": True, "status": "active",
                    "independence_key": "CAB-7", "interval_m": [326.0, 346.0]}]
    result = recompute_cells(observations, cells, prior_states=prior, only_cells={"R-17"})
    # Untouched cell is passed through as the exact prior object.
    assert result["R-16"] is prior["R-16"]
    assert result["R-17"]["state"] == "unverified"
    assert result["R-17"]["independent_witnesses"] == 1


# ---------------------------------------------------------------------------
# Module-level connection helper also yields a working transactional store
# ---------------------------------------------------------------------------


def test_connect_helper_roundtrip(bundle, tmp_path):
    db = connect(tmp_path / "capture.sqlite")
    try:
        from taxiproof.road_capture import import_bundle, report as road_report

        assert import_bundle(db, copy.deepcopy(bundle))["inserted"] == 3
        view = road_report(db)
        assert view["packet_count"] == 3
        assert len(view["source_graph"]["components"]) >= 1
    finally:
        db.close()
