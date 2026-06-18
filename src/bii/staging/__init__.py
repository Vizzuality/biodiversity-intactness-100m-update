"""Staging: stream-convert source datasets into COGs in S3 + footprint indexes.

Every module exposes the same pair so the CLI / orchestrator can fan out one Batch array
job per unit, dispatching by name via the :data:`MODULES` registry:

    list_units(...) -> list[dict]   # enumerate work; each unit has a stable "id" + output "dst"
    stage_unit(unit, ...) -> bool   # stage one unit; falsy == produced nothing (e.g. ocean tile)

Every unit carries its output URI as ``dst`` so the orchestrator (:mod:`bii.stage`) can do the
skip-if-exists check without dataset-specific knowledge; per-year modules' ``list_units`` takes a
``years`` filter.

``stage_unit`` only reports whether it produced output — the worker builds the completion record
from the manifest line, and the asset index is rebuilt from the staged COGs after the run
(:func:`bii.tile_index.index_cogs`). (``iolulc`` is the exception: index-only, it returns a richer
dict.)

Raster streaming: hansen, worldpop, nightlights, travel_time, fml.
Vector rasterization (ephemeral disk): sdpt (a forestManagement provider alongside fml),
roads (highway mask; requires osmctools, e.g. in Dockerfile.roads).
Index-only (no pixels moved): iolulc (landcover) records IO STAC hrefs in place.
"""

from __future__ import annotations

from . import (
    forest_management,
    hansen,
    iolulc,
    nightlights,
    roads,
    sdpt,
    travel_time,
    worldpop,
)

MODULES = {
    "hansen": hansen,
    "worldpop": worldpop,
    "nightlights": nightlights,
    "travel_time": travel_time,
    "fml": forest_management,
    "sdpt": sdpt,
    "roads": roads,
    "iolulc": iolulc,
}

__all__ = [
    "MODULES",
    "forest_management",
    "hansen",
    "iolulc",
    "nightlights",
    "roads",
    "sdpt",
    "travel_time",
    "worldpop",
]
