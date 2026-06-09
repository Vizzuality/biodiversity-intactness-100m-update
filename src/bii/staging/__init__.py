"""Staging: stream-convert source datasets into COGs in S3 + footprint indexes.

Every module exposes the same pair so the CLI / orchestrator can fan out one Batch array
job per unit, dispatching by name via the :data:`MODULES` registry:

    list_units(...) -> list[dict]      # enumerate work; each unit has a stable "id"
    stage_unit(unit, ...) -> dict|None # stage one unit; None == skipped (e.g. ocean tile)

``stage_unit`` returns its result via :func:`bii.tile_index.finalize`, which registers the
COG's footprint and packs ``{asset, uri, footprint, year, index_part}``.

Raster streaming: hansen, worldpop, nightlights, travel_time, fml.
Vector rasterization (ephemeral disk): sdpt (a forestManagement provider alongside fml),
roads (highway mask; requires osmctools, e.g. in Dockerfile.roads).
"""

from __future__ import annotations

from . import (
    forest_management,
    hansen,
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
}

__all__ = [
    "MODULES",
    "forest_management",
    "hansen",
    "nightlights",
    "roads",
    "sdpt",
    "travel_time",
    "worldpop",
]
