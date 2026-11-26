"""Source ingestion, conflation, segmentation, reconciliation, and PostGIS pipeline.

Note: `pipeline` is imported lazily via ``__getattr__`` so running
``python -m trailforge.ingestion.pipeline`` never emits a ``RuntimeWarning``.
"""

from __future__ import annotations

from typing import Any

from trailforge.ingestion.conflate import ConflatedWay, ConflationResult, conflate_tracks
from trailforge.ingestion.reconcile import (
    ExistingSegmentSnapshot,
    ReconciliationPlan,
    reconcile_segments,
)
from trailforge.ingestion.segmenter import (
    CanonicalSegmentCandidate,
    CanonicalTrackGroup,
    segment_conflated_ways,
)

__all__ = [
    "CanonicalSegmentCandidate",
    "CanonicalTrackGroup",
    "ConflatedWay",
    "ConflationResult",
    "ExistingSegmentSnapshot",
    "PipelineRunSummary",
    "ReconciliationPlan",
    "conflate_tracks",
    "ingest_corridor_dataset",
    "reconcile_segments",
    "segment_conflated_ways",
]


def __getattr__(name: str) -> Any:
    if name in ("PipelineRunSummary", "ingest_corridor_dataset"):
        from trailforge.ingestion import pipeline

        return getattr(pipeline, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
