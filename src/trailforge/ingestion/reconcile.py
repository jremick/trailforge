"""4-Pass Re-Import Stable Segment-ID Reconciliation Engine.

Implements docs/SPEC_GIS_SCHEMA.md Section 3.3.2:
  - Pass 1: Exact `geom_fingerprint` match -> O(1) ID preservation (`exact_match`)
  - Pass 2: 12 m symmetric corridor overlap >= 0.75 AND Hausdorff <= 15.0 m AND
            acute heading diff <= 25 deg AND length ratio >= 0.75 -> greedy max-weight
            1:1 preservation (`spatial_preserved`) so upstream node jitter never orphans
            attached telemetry_observations, segment_closure_links, or condition_reports
  - Pass 3: Split (1 -> k), merge (m -> 1), and soft-retire lineage (`is_active=FALSE`,
            `superseded_by_segment_id`) — NEVER hard-deletes track_segments
  - Pass 4: Newly minted segments (`created`)
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from shapely import STRtree, hausdorff_distance
from shapely.geometry import LineString
from shapely.ops import unary_union

from trailforge.ingestion.conflate import acute_heading_diff_deg
from trailforge.ingestion.segmenter import CanonicalSegmentCandidate

RECONCILE_CORRIDOR_M = 12.0
RECONCILE_SEARCH_M = 15.0
MIN_OVERLAP_RATIO = 0.75
MAX_HAUSDORFF_M = 15.0
MAX_HEADING_DIFF_DEG = 25.0
MIN_LENGTH_RATIO = 0.75

ReconcileAction = Literal[
    "created",
    "exact_match",
    "spatial_preserved",
    "split",
    "merge",
    "retired",
]


@dataclass(frozen=True)
class ExistingSegmentSnapshot:
    """Active canonical segment loaded from PostGIS before a re-import batch."""

    segment_id: str
    geom_fingerprint: str
    geom_3577: LineString
    length_m: float
    bearing_deg: float


@dataclass(frozen=True)
class ReconciliationLogEntry:
    """Single row destined for `segment_reconciliation_log`."""

    import_run_id: uuid.UUID
    reconcile_action: ReconcileAction
    old_segment_id: str | None
    new_segment_id: str | None
    overlap_ratio: float | None = None
    hausdorff_m: float | None = None
    heading_diff_deg: float | None = None


@dataclass(frozen=True)
class RetiredSegmentDirective:
    """Directive to soft-retire an existing segment without hard DELETE."""

    segment_id: str
    superseded_by_segment_id: str | None = None


@dataclass
class ReconciledSegment:
    """Candidate paired with its final stable `segment_id` and reconciliation action."""

    final_segment_id: str
    candidate: CanonicalSegmentCandidate
    action: ReconcileAction
    is_update: bool


@dataclass
class ReconciliationPlan:
    """Full deterministic reconciliation output ready to apply inside one DB transaction."""

    import_run_id: uuid.UUID
    reconciled_segments: list[ReconciledSegment] = field(default_factory=list)
    retired_segments: list[RetiredSegmentDirective] = field(default_factory=list)
    log_entries: list[ReconciliationLogEntry] = field(default_factory=list)

    @property
    def exact_match_count(self) -> int:
        return sum(1 for r in self.reconciled_segments if r.action == "exact_match")

    @property
    def spatial_preserved_count(self) -> int:
        return sum(1 for r in self.reconciled_segments if r.action == "spatial_preserved")

    @property
    def created_count(self) -> int:
        return sum(1 for r in self.reconciled_segments if r.action == "created")


def _symmetric_corridor_overlap(
    line_a: LineString, line_b: LineString, corridor_m: float = RECONCILE_CORRIDOR_M
) -> float:
    len_a = max(line_a.length, 1e-6)
    len_b = max(line_b.length, 1e-6)
    cov_a = line_a.intersection(line_b.buffer(corridor_m)).length / len_a
    cov_b = line_b.intersection(line_a.buffer(corridor_m)).length / len_b
    return min(cov_a, cov_b)


def reconcile_segments(
    existing_active: Sequence[ExistingSegmentSnapshot],
    candidates: Sequence[CanonicalSegmentCandidate],
    *,
    import_run_id: uuid.UUID | None = None,
    all_known_segment_ids: set[str] | None = None,
) -> ReconciliationPlan:
    """Execute the 4-pass stable-ID reconciliation algorithm per SPEC_GIS_SCHEMA Sec 3.3.2."""
    run_id = import_run_id or uuid.uuid4()
    plan = ReconciliationPlan(import_run_id=run_id)

    unmatched_existing: dict[str, ExistingSegmentSnapshot] = {
        e.segment_id: e for e in existing_active
    }
    unmatched_candidates: dict[int, CanonicalSegmentCandidate] = {
        i: c for i, c in enumerate(candidates)
    }

    used_ids: set[str] = set(all_known_segment_ids or set()) | set(unmatched_existing.keys())

    # -------------------------------------------------------------------------
    # Pass 1 — Exact geom_fingerprint Match (O(1))
    # -------------------------------------------------------------------------
    by_fingerprint: dict[str, list[ExistingSegmentSnapshot]] = {}
    for ex in existing_active:
        by_fingerprint.setdefault(ex.geom_fingerprint, []).append(ex)

    for c_idx in list(unmatched_candidates.keys()):
        cand = unmatched_candidates[c_idx]
        matches = by_fingerprint.get(cand.geom_fingerprint)
        if not matches:
            continue
        # Pick first still-unmatched active existing segment with identical fingerprint
        chosen: ExistingSegmentSnapshot | None = None
        for ex in matches:
            if ex.segment_id in unmatched_existing:
                chosen = ex
                break
        if chosen is None:
            continue

        del unmatched_existing[chosen.segment_id]
        del unmatched_candidates[c_idx]

        plan.reconciled_segments.append(
            ReconciledSegment(
                final_segment_id=chosen.segment_id,
                candidate=cand,
                action="exact_match",
                is_update=True,
            )
        )
        plan.log_entries.append(
            ReconciliationLogEntry(
                import_run_id=run_id,
                reconcile_action="exact_match",
                old_segment_id=chosen.segment_id,
                new_segment_id=chosen.segment_id,
                overlap_ratio=1.0,
                hausdorff_m=0.0,
                heading_diff_deg=0.0,
            )
        )

    # -------------------------------------------------------------------------
    # Pass 2 — Spatial Corridor + Hausdorff + Heading 1:1 Preservation
    # -------------------------------------------------------------------------
    if unmatched_existing and unmatched_candidates:
        ex_list = list(unmatched_existing.values())
        ex_tree = STRtree([e.geom_3577 for e in ex_list])

        scored_pairs: list[tuple[float, int, str, float, float, float]] = []
        for c_idx, cand in unmatched_candidates.items():
            for pos in ex_tree.query(cand.geom_3577.buffer(RECONCILE_SEARCH_M)):
                ex = ex_list[int(pos)]
                if cand.geom_3577.distance(ex.geom_3577) > RECONCILE_SEARCH_M:
                    continue
                len_ratio = min(cand.length_m, ex.length_m) / max(cand.length_m, ex.length_m, 1e-6)
                if len_ratio < MIN_LENGTH_RATIO:
                    continue
                dtheta = acute_heading_diff_deg(cand.bearing_deg, ex.bearing_deg)
                if dtheta > MAX_HEADING_DIFF_DEG:
                    continue
                dh = float(hausdorff_distance(cand.geom_3577, ex.geom_3577))
                if dh > MAX_HAUSDORFF_M:
                    continue
                ov = _symmetric_corridor_overlap(cand.geom_3577, ex.geom_3577)
                if ov < MIN_OVERLAP_RATIO:
                    continue

                score = (
                    0.50 * ov
                    + 0.30 * (1.0 - dh / MAX_HAUSDORFF_M)
                    + 0.20 * (1.0 - dtheta / MAX_HEADING_DIFF_DEG)
                )
                scored_pairs.append((score, c_idx, ex.segment_id, ov, dh, dtheta))

        # Greedy maximum-weight bipartite matching (deterministic tie-break by IDs)
        scored_pairs.sort(key=lambda item: (-item[0], item[2], item[1]))
        for _score, c_idx, ex_id, ov, dh, dtheta in scored_pairs:
            if c_idx not in unmatched_candidates or ex_id not in unmatched_existing:
                continue
            cand = unmatched_candidates.pop(c_idx)
            unmatched_existing.pop(ex_id)
            plan.reconciled_segments.append(
                ReconciledSegment(
                    final_segment_id=ex_id,
                    candidate=cand,
                    action="spatial_preserved",
                    is_update=True,
                )
            )
            plan.log_entries.append(
                ReconciliationLogEntry(
                    import_run_id=run_id,
                    reconcile_action="spatial_preserved",
                    old_segment_id=ex_id,
                    new_segment_id=ex_id,
                    overlap_ratio=round(ov, 4),
                    hausdorff_m=round(dh, 3),
                    heading_diff_deg=round(dtheta, 2),
                )
            )

    # -------------------------------------------------------------------------
    # Pass 3 & Pass 4 — Split (1 -> k), Merge (m -> 1), Soft-Retire, & New Mint
    # -------------------------------------------------------------------------
    # First assign final unique IDs to all remaining unmatched candidates
    rem_cand_ids: dict[int, str] = {}
    for c_idx, cand in unmatched_candidates.items():
        final_id = cand.segment_id
        if final_id in used_ids:
            # Extremely rare collision with a previously retired ID: derive deterministic suffix
            suffix = 2
            while f"{final_id}_{suffix}" in used_ids:
                suffix += 1
            final_id = f"{final_id}_{suffix}"
        used_ids.add(final_id)
        rem_cand_ids[c_idx] = final_id

    handled_cands: set[int] = set()
    handled_existing: set[str] = set()

    # Detect Splits: 1 unmatched existing E covered >= 75% by >= 2 unmatched candidates
    if unmatched_existing and unmatched_candidates:
        cand_indices = list(unmatched_candidates.keys())
        cand_tree = STRtree([unmatched_candidates[i].geom_3577 for i in cand_indices])

        for ex_id, ex in list(unmatched_existing.items()):
            covering: list[tuple[int, float]] = []
            for pos in cand_tree.query(ex.geom_3577.buffer(RECONCILE_CORRIDOR_M)):
                c_idx = cand_indices[int(pos)]
                cand = unmatched_candidates[c_idx]
                dtheta = acute_heading_diff_deg(cand.bearing_deg, ex.bearing_deg)
                if dtheta > MAX_HEADING_DIFF_DEG:
                    continue
                inter_len = ex.geom_3577.intersection(
                    cand.geom_3577.buffer(RECONCILE_CORRIDOR_M)
                ).length
                if inter_len > 1.0:
                    covering.append((c_idx, inter_len))

            if len(covering) >= 2:
                union_cov = unary_union(
                    [
                        unmatched_candidates[i].geom_3577.buffer(RECONCILE_CORRIDOR_M)
                        for i, _ in covering
                    ]
                )
                total_cov_ratio = ex.geom_3577.intersection(union_cov).length / max(
                    ex.length_m, 1e-6
                )
                if total_cov_ratio >= MIN_OVERLAP_RATIO:
                    covering.sort(key=lambda x: -x[1])
                    dominant_id = rem_cand_ids[covering[0][0]]
                    plan.retired_segments.append(
                        RetiredSegmentDirective(
                            segment_id=ex_id, superseded_by_segment_id=dominant_id
                        )
                    )
                    handled_existing.add(ex_id)
                    for c_idx, inter_len in covering:
                        cid = rem_cand_ids[c_idx]
                        if c_idx not in handled_cands:
                            plan.reconciled_segments.append(
                                ReconciledSegment(
                                    final_segment_id=cid,
                                    candidate=unmatched_candidates[c_idx],
                                    action="split",
                                    is_update=False,
                                )
                            )
                            handled_cands.add(c_idx)
                        plan.log_entries.append(
                            ReconciliationLogEntry(
                                import_run_id=run_id,
                                reconcile_action="split",
                                old_segment_id=ex_id,
                                new_segment_id=cid,
                                overlap_ratio=round(inter_len / max(ex.length_m, 1e-6), 4),
                            )
                        )

        # Detect Merges: 1 remaining candidate C covering >= 75% of >= 2 remaining existing
        rem_ex_ids = [eid for eid in unmatched_existing if eid not in handled_existing]
        if rem_ex_ids:
            ex_tree2 = STRtree([unmatched_existing[eid].geom_3577 for eid in rem_ex_ids])
            for c_idx in cand_indices:
                if c_idx in handled_cands:
                    continue
                cand = unmatched_candidates[c_idx]
                merged_ex: list[tuple[str, float]] = []
                for pos in ex_tree2.query(cand.geom_3577.buffer(RECONCILE_CORRIDOR_M)):
                    eid = rem_ex_ids[int(pos)]
                    if eid in handled_existing:
                        continue
                    ex = unmatched_existing[eid]
                    dtheta = acute_heading_diff_deg(cand.bearing_deg, ex.bearing_deg)
                    if dtheta > MAX_HEADING_DIFF_DEG:
                        continue
                    cov = ex.geom_3577.intersection(
                        cand.geom_3577.buffer(RECONCILE_CORRIDOR_M)
                    ).length / max(ex.length_m, 1e-6)
                    if cov >= MIN_OVERLAP_RATIO:
                        merged_ex.append((eid, cov))

                if len(merged_ex) >= 2:
                    cid = rem_cand_ids[c_idx]
                    plan.reconciled_segments.append(
                        ReconciledSegment(
                            final_segment_id=cid,
                            candidate=cand,
                            action="merge",
                            is_update=False,
                        )
                    )
                    handled_cands.add(c_idx)
                    for eid, cov in merged_ex:
                        handled_existing.add(eid)
                        plan.retired_segments.append(
                            RetiredSegmentDirective(
                                segment_id=eid, superseded_by_segment_id=cid
                            )
                        )
                        plan.log_entries.append(
                            ReconciliationLogEntry(
                                import_run_id=run_id,
                                reconcile_action="merge",
                                old_segment_id=eid,
                                new_segment_id=cid,
                                overlap_ratio=round(cov, 4),
                            )
                        )

    # Soft-retire any remaining unmatched existing segments (NEVER hard DELETE)
    for ex_id in unmatched_existing:
        if ex_id in handled_existing:
            continue
        plan.retired_segments.append(
            RetiredSegmentDirective(segment_id=ex_id, superseded_by_segment_id=None)
        )
        plan.log_entries.append(
            ReconciliationLogEntry(
                import_run_id=run_id,
                reconcile_action="retired",
                old_segment_id=ex_id,
                new_segment_id=None,
            )
        )

    # Pass 4: Create all remaining newly minted segments
    for c_idx, cand in unmatched_candidates.items():
        if c_idx in handled_cands:
            continue
        cid = rem_cand_ids[c_idx]
        plan.reconciled_segments.append(
            ReconciledSegment(
                final_segment_id=cid,
                candidate=cand,
                action="created",
                is_update=False,
            )
        )
        plan.log_entries.append(
            ReconciliationLogEntry(
                import_run_id=run_id,
                reconcile_action="created",
                old_segment_id=None,
                new_segment_id=cid,
            )
        )

    return plan
