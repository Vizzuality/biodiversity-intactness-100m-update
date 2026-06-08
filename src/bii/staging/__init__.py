"""Staging: stream-convert source datasets into COGs in S3 + footprint indexes.

Each module exposes ``list_units()`` and ``stage_unit(unit, ...)`` (see ``_base``). The
:data:`MODULES` registry lets the CLI / orchestrator dispatch by name.

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
