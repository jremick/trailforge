"""Offline unit tests for conflation, 50-100 m segmentation, and 4-pass ID reconciliation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shapely.geometry import LineString

from trailforge.ingestion.conflate import conflate_tracks, to_albers
from trailforge.ingestion.reconcile import ExistingSegmentSnapshot, reconcile_segments
from trailforge.ingestion.segmenter import (
    compute_chunk_count,
    compute_geom_fingerprint_and_id,
    segment_conflated_ways,
)


@dataclass
class _FixtureTrack:
    source: str
    source_layer: str
    source_id: str
    name: str | None
    trail_class: str
    is_vehicular: bool
    surface_class: str
    tracktype: str | None
    smoothness: str | None
    is_4wd_only: bool
    is_4wd_recommended: bool
    access_status: str
    has_gate_barrier: bool
    is_bridged: bool
    is_ford: bool
    geometry: dict[str, Any]


def _load_fixture_tracks(
    fixtures_dir: Path, osm_file: str = "osm_tracks.geojson"
) -> list[_FixtureTrack]:
    out: list[_FixtureTrack] = []

    osm_fc = json.loads((fixtures_dir / osm_file).read_text(encoding="utf-8"))
    for feat in osm_fc["features"]:
        p = feat["properties"]
        hw = p.get("highway", "track")
        mv = p.get("motor_vehicle")
        is_veh = not (hw == "path" and mv == "no")
        trail_class = (
            "walking_track"
            if not is_veh
            else ("unsealed_road" if hw == "unclassified" else "4wd_track")
        )
        surf_map = {
            "asphalt": "sealed",
            "gravel": "gravel_compacted",
            "dirt": "dirt_unsealed",
            "rock": "rough_rock_mud_sand",
        }
        out.append(
            _FixtureTrack(
                source="osm",
                source_layer="osm",
                source_id=f"way/{p['osm_way_id']}",
                name=p.get("name"),
                trail_class=trail_class,
                is_vehicular=is_veh,
                surface_class=surf_map.get(p.get("surface") or "", "unknown"),
                tracktype=p.get("tracktype"),
                smoothness=p.get("smoothness"),
                is_4wd_only=(p.get("4wd_only") == "yes"),
                is_4wd_recommended=(p.get("4wd_only") == "recommended"),
                access_status=p.get("access") or "public",
                has_gate_barrier=bool(p.get("has_gate_barrier")),
                is_bridged=(p.get("bridge") == "yes"),
                is_ford=(p.get("ford") == "yes"),
                geometry=feat["geometry"],
            )
        )

    vic_fc = json.loads((fixtures_dir / "vic_tracks.geojson").read_text(encoding="utf-8"))
    for feat in vic_fc["features"]:
        p = feat["properties"]
        seal_map = {"1": "sealed", "2": "gravel_compacted", "3": "dirt_unsealed"}
        out.append(
            _FixtureTrack(
                source="vic",
                source_layer=p["source_layer"],
                source_id=p["pfi"],
                name=p.get("ezi_road_name_label"),
                trail_class=p["trail_class"],
                is_vehicular=bool(p["is_vehicular"]),
                surface_class=seal_map.get(str(p.get("road_seal") or ""), "unknown"),
                tracktype=None,
                smoothness=None,
                is_4wd_only=False,
                is_4wd_recommended=False,
                access_status="public",
                has_gate_barrier=False,
                is_bridged=False,
                is_ford=False,
                geometry=feat["geometry"],
            )
        )

    nsw_fc = json.loads((fixtures_dir / "nsw_tracks.geojson").read_text(encoding="utf-8"))
    for feat in nsw_fc["features"]:
        p = feat["properties"]
        surf_map = {"1": "sealed", "2": "gravel_compacted", "3": "dirt_unsealed"}
        full_name = " ".join(
            part for part in (p.get("roadnamebase"), p.get("roadnametype")) if part
        )
        out.append(
            _FixtureTrack(
                source="nsw",
                source_layer=p["source_layer"],
                source_id=p["brimsid"],
                name=full_name or None,
                trail_class=p["trail_class"],
                is_vehicular=bool(p["is_vehicular"]),
                surface_class=surf_map.get(str(p.get("surface") or ""), "unknown"),
                tracktype=None,
                smoothness=None,
                is_4wd_only=False,
                is_4wd_recommended=False,
                access_status="public",
                has_gate_barrier=False,
                is_bridged=False,
                is_ford=False,
                geometry=feat["geometry"],
            )
        )

    return out


def test_chunk_count_invariant_strictly_within_50_to_100m() -> None:
    assert compute_chunk_count(35.0) == 1
    assert compute_chunk_count(100.0) == 1
    for length_m in [100.1, 120.0, 149.9, 150.0, 150.1, 200.0, 225.0, 499.0, 1234.5, 9876.0]:
        n = compute_chunk_count(length_m)
        chunk = length_m / n
        assert 50.0 <= chunk <= 100.0, f"L={length_m}, N={n}, chunk={chunk}"


def test_direction_normalization_and_fingerprint_invariance() -> None:
    fwd = LineString([(148.1200, -36.5900), (148.1205, -36.5904), (148.1210, -36.5908)])
    rev = LineString(list(reversed(fwd.coords)))

    _, _, fp_fwd, id_fwd, sk_fwd, ek_fwd = compute_geom_fingerprint_and_id(fwd)
    _, _, fp_rev, id_rev, sk_rev, ek_rev = compute_geom_fingerprint_and_id(rev)

    assert fp_fwd == fp_rev
    assert id_fwd == id_rev
    assert id_fwd.startswith("tfseg_") and len(id_fwd) == 22
    assert sk_fwd == sk_rev and ek_fwd == ek_rev


def test_corridor_conflation_priority_dedupe_and_metadata_enrichment(fixtures_dir: Path) -> None:
    raw = _load_fixture_tracks(fixtures_dir, "osm_tracks.geojson")
    res = conflate_tracks(raw)

    # 2 walking tracks (way/1007 Main Range Footpad + VIC-RW-3001 Alpine Walking Track) filtered
    assert res.dropped_non_vehicular_count == 2
    # 2 overlapping state tracks deduplicated: VIC-TR-2001 (Davies Plain) & NSW FT-401 (Poplars)
    assert res.deduplicated_count == 2
    # Surviving vehicular ways: 6 OSM + 1 VIC-only (Bucky Ridge) + 1 NSW-only (Pinch River) = 8
    assert len(res.ways) == 8

    by_name = {w.name: w for w in res.ways}
    assert "BUCKY RIDGE FIRE TRAIL" in by_name
    assert by_name["BUCKY RIDGE FIRE TRAIL"].primary_source == "vic"
    assert by_name["BUCKY RIDGE FIRE TRAIL"].has_osm_source is False

    assert "PINCH RIVER FIRE TRAIL" in by_name
    assert by_name["PINCH RIVER FIRE TRAIL"].primary_source == "nsw"
    assert by_name["PINCH RIVER FIRE TRAIL"].has_osm_source is False

    # Davies Plain Track Lower/Ridge enriched with VIC-TR-2001 provenance & alt_name
    davies_lower = by_name["Davies Plain Track Lower"]
    assert any(ref["source_id"] == "VIC-TR-2001" for ref in davies_lower.source_refs)
    assert "DAVIES PLAIN TRACK" in davies_lower.alt_names

    # Poplars Fire Trail enriched with NSW FT-401 provenance
    poplars = by_name["Poplars Fire Trail"]
    assert any(ref["source_id"] == "FT-401" for ref in poplars.source_refs)


def test_corridor_segmentation_and_v2_jitter_reconciliation(fixtures_dir: Path) -> None:
    raw_v1 = _load_fixture_tracks(fixtures_dir, "osm_tracks.geojson")
    conflated_v1 = conflate_tracks(raw_v1)
    tracks_v1, segs_v1 = segment_conflated_ways(conflated_v1.ways)

    assert len(tracks_v1) == 8
    assert len(segs_v1) > 100
    # Every segment from ways >100 m must strictly lie in [50, 100] m (tiny tolerance for round)
    for seg in segs_v1:
        assert 49.5 <= seg.length_m <= 100.5, f"{seg.name}: length={seg.length_m}"
        assert seg.segment_id.startswith("tfseg_")
        assert len(seg.geom_fingerprint) == 64

    # Snapshot v1 active segments as if loaded from PostGIS
    existing_v1 = [
        ExistingSegmentSnapshot(
            segment_id=s.segment_id,
            geom_fingerprint=s.geom_fingerprint,
            geom_3577=s.geom_3577,
            length_m=s.length_m,
            bearing_deg=s.bearing_deg,
        )
        for s in segs_v1
    ]

    # Re-ingest v2 jittered OSM fixture:
    # - way/1001: tag edit only -> exact_match (Pass 1)
    # - way/1002: vertex-reversed + 3-6 m jitter -> spatial_preserved (Pass 2)
    # - way/1004: split mid-way into 100401+100402 -> degree-2 merge -> exact_match (Pass 1)
    raw_v2 = _load_fixture_tracks(fixtures_dir, "osm_tracks_v2_jittered.geojson")
    conflated_v2 = conflate_tracks(raw_v2)
    _tracks_v2, segs_v2 = segment_conflated_ways(conflated_v2.ways)

    plan = reconcile_segments(existing_v1, segs_v2)

    assert len(plan.retired_segments) == 0, (
        f"Expected zero retired segments under v2 tag-edit/split/jitter, "
        f"got {len(plan.retired_segments)}"
    )
    assert plan.created_count == 0
    assert plan.exact_match_count > 0
    assert plan.spatial_preserved_count > 0
    assert plan.exact_match_count + plan.spatial_preserved_count == len(segs_v1)

    # Confirm exact set of segment_ids is 100% preserved across v1 -> v2!
    v1_ids = {s.segment_id for s in segs_v1}
    v2_ids = {r.final_segment_id for r in plan.reconciled_segments}
    assert v2_ids == v1_ids

    # Specifically confirm way/1002 (Davies Plain Track Lower) was preserved via Pass 2
    davies_v2_actions = {
        r.action for r in plan.reconciled_segments if r.candidate.name == "Davies Plain Track Lower"
    }
    assert "spatial_preserved" in davies_v2_actions

    # Confirm way/1001 (tag edit) and way/1004 (mid-way split merged back) were exact_match!
    tom_actions = {
        r.action for r in plan.reconciled_segments if r.candidate.name == "Tom Groggin Access Rd"
    }
    poplars_actions = {
        r.action for r in plan.reconciled_segments if r.candidate.name == "Poplars Fire Trail"
    }
    assert tom_actions == {"exact_match"}
    assert poplars_actions == {"exact_match"}


def test_reconcile_split_and_soft_retire_lineage() -> None:
    # Single 90 m existing segment split into two 45 m candidates -> split + soft-retire (no delete)
    base_line = LineString([(148.1000, -36.6000), (148.1000, -36.60081)])
    _, b3577, fp, seg_id, _, _ = compute_geom_fingerprint_and_id(base_line)
    existing = [
        ExistingSegmentSnapshot(
            segment_id=seg_id,
            geom_fingerprint=fp,
            geom_3577=b3577,
            length_m=round(b3577.length, 2),
            bearing_deg=180.0,
        )
    ]

    part_a = LineString([(148.1000, -36.6000), (148.1000, -36.60041)])
    part_b = LineString([(148.1000, -36.60041), (148.1000, -36.60081)])

    from trailforge.ingestion.segmenter import CanonicalSegmentCandidate

    cands = []
    for part in (part_a, part_b):
        nw, n3577, cfp, cid, sk, ek = compute_geom_fingerprint_and_id(part)
        cands.append(
            CanonicalSegmentCandidate(
                segment_id=cid,
                geom_fingerprint=cfp,
                geom_wgs84=nw,
                geom_3577=n3577,
                start_node_key=sk,
                end_node_key=ek,
                length_m=round(n3577.length, 2),
                bearing_deg=180.0,
                state="VIC",
                name="Split Test Track",
                alt_names=[],
                trail_class="4wd_track",
                is_vehicular=True,
                surface_class="dirt_unsealed",
                tracktype="grade3",
                smoothness=None,
                is_4wd_only=False,
                is_4wd_recommended=False,
                access_status="public",
                has_gate_barrier=False,
                is_bridged=False,
                is_ford=False,
                primary_source="osm",
                has_osm_source=True,
                source_refs=[],
            )
        )

    plan = reconcile_segments(existing, cands)
    assert len(plan.retired_segments) == 1
    assert plan.retired_segments[0].segment_id == seg_id
    assert plan.retired_segments[0].superseded_by_segment_id in {c.segment_id for c in cands}
    assert all(e.reconcile_action == "split" for e in plan.log_entries)
    assert len(plan.log_entries) == 2

    # Ensure to_albers returns metric length ~90 m
    assert 85.0 <= to_albers(base_line).length <= 95.0


def test_short_spur_preserved_and_dangling_sliver_dropped() -> None:
    # A 35 m connected spur stays intact as 1 segment (<50 m, >=15 m), while an 8 m sliver drops
    main_track = _FixtureTrack(
        source="osm",
        source_layer="osm",
        source_id="way/9001",
        name="Main Track",
        trail_class="4wd_track",
        is_vehicular=True,
        surface_class="dirt_unsealed",
        tracktype="grade3",
        smoothness=None,
        is_4wd_only=False,
        is_4wd_recommended=False,
        access_status="public",
        has_gate_barrier=False,
        is_bridged=False,
        is_ford=False,
        geometry={
            "type": "LineString",
            "coordinates": [[148.1000, -36.6000], [148.1000, -36.6018]],
        },
    )
    short_spur = _FixtureTrack(
        source="osm",
        source_layer="osm",
        source_id="way/9002",
        name="Camp Spur",
        trail_class="4wd_track",
        is_vehicular=True,
        surface_class="dirt_unsealed",
        tracktype="grade3",
        smoothness=None,
        is_4wd_only=False,
        is_4wd_recommended=False,
        access_status="public",
        has_gate_barrier=False,
        is_bridged=False,
        is_ford=False,
        geometry={
            "type": "LineString",
            "coordinates": [[148.1000, -36.6009], [148.1004, -36.6009]],  # ~36 m
        },
    )
    tiny_sliver = _FixtureTrack(
        source="osm",
        source_layer="osm",
        source_id="way/9003",
        name="Tiny Sliver",
        trail_class="4wd_track",
        is_vehicular=True,
        surface_class="dirt_unsealed",
        tracktype="grade3",
        smoothness=None,
        is_4wd_only=False,
        is_4wd_recommended=False,
        access_status="public",
        has_gate_barrier=False,
        is_bridged=False,
        is_ford=False,
        geometry={
            "type": "LineString",
            "coordinates": [[148.1000, -36.6015], [148.10009, -36.6015]],  # ~8 m
        },
    )

    conflated = conflate_tracks([main_track, short_spur, tiny_sliver])
    tracks, segs = segment_conflated_ways(conflated.ways)
    names = {t.name for t in tracks}
    assert "Main Track" in names
    assert "Camp Spur" in names
    assert "Tiny Sliver" not in names
    spur_segs = [s for s in segs if s.name == "Camp Spur"]
    assert len(spur_segs) == 1
    assert 20.0 <= spur_segs[0].length_m <= 50.0


def test_partial_overlap_trims_and_preserves_distinct_tail() -> None:
    # OSM covers northern 200 m; VIC covers same 200 m + continues 300 m further south
    osm_way = _FixtureTrack(
        source="osm",
        source_layer="osm",
        source_id="way/8001",
        name="North Leg",
        trail_class="4wd_track",
        is_vehicular=True,
        surface_class="dirt_unsealed",
        tracktype="grade3",
        smoothness=None,
        is_4wd_only=False,
        is_4wd_recommended=False,
        access_status="public",
        has_gate_barrier=False,
        is_bridged=False,
        is_ford=False,
        geometry={
            "type": "LineString",
            "coordinates": [[148.1100, -36.6000], [148.1100, -36.6018]],
        },
    )
    vic_way = _FixtureTrack(
        source="vic",
        source_layer="tr_road",
        source_id="VIC-PARTIAL-1",
        name="Full State Track",
        trail_class="fire_trail",
        is_vehicular=True,
        surface_class="gravel_compacted",
        tracktype=None,
        smoothness=None,
        is_4wd_only=False,
        is_4wd_recommended=False,
        access_status="public",
        has_gate_barrier=False,
        is_bridged=False,
        is_ford=False,
        geometry={
            "type": "MultiLineString",
            "coordinates": [
                [[148.11005, -36.6000], [148.11005, -36.6018], [148.11005, -36.6045]]
            ],
        },
    )

    res = conflate_tracks([osm_way, vic_way])
    assert res.partial_trimmed_count == 1
    assert len(res.ways) == 2
    vic_tail = [w for w in res.ways if w.primary_source == "vic"][0]
    assert 250.0 <= vic_tail.geom_3577.length <= 350.0


def test_hairpin_switchback_track_topological_segment_seq() -> None:
    """Consecutive segment_seq segments on a U-shaped hairpin track must share endpoints."""
    # Hairpin climbs east, turns sharply south, then returns west (longitude reverses!)
    hairpin = _FixtureTrack(
        source="osm",
        source_layer="osm",
        source_id="way/7777",
        name="Switchback Ridge Track",
        trail_class="4wd_track",
        is_vehicular=True,
        surface_class="rough_rock_mud_sand",
        tracktype="grade4",
        smoothness=None,
        is_4wd_only=True,
        is_4wd_recommended=False,
        access_status="public",
        has_gate_barrier=False,
        is_bridged=False,
        is_ford=False,
        geometry={
            "type": "LineString",
            "coordinates": [
                [148.1000, -36.6000],
                [148.1030, -36.6000],  # east leg (~270 m)
                [148.1040, -36.6010],  # hairpin bend (~140 m)
                [148.1005, -36.6015],  # west return leg (~320 m)
            ],
        },
    )

    conflated = conflate_tracks([hairpin])
    tracks, segs = segment_conflated_ways(conflated.ways)
    assert len(tracks) == 1
    ordered = sorted(segs, key=lambda s: s.segment_seq or 0)
    assert len(ordered) >= 8

    for prev_seg, next_seg in zip(ordered, ordered[1:], strict=False):
        shared_nodes = {prev_seg.start_node_key, prev_seg.end_node_key} & {
            next_seg.start_node_key,
            next_seg.end_node_key,
        }
        assert shared_nodes, (
            f"Discontinuous segment_seq {prev_seg.segment_seq}->{next_seg.segment_seq}: "
            f"{prev_seg.start_node_key}..{prev_seg.end_node_key} vs "
            f"{next_seg.start_node_key}..{next_seg.end_node_key}"
        )
        assert prev_seg.geom_3577.distance(next_seg.geom_3577) < 0.5


