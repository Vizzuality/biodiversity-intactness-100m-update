"""Staging: convert source datasets into COGs in S3 + footprint indexes.

Every module exposes the same functions, dispatched by name via the :data:`MODULES` registry:

    list_units(...) -> list[dict]   # enumerate work; each unit has a stable "id" + output "dst"
    stage_unit(unit, ...) -> bool   # stage one unit; falsy == produced nothing (e.g. ocean tile)

roads requires osmctools (e.g. in Dockerfile). iolulc records IO STAC hrefs in place (no pixels
moved).
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
