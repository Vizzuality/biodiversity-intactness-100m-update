"""Stage Hansen Global Forest Change ``lossyear`` -> forestLoss COGs (one job per 10deg tile).

Source tiles are 10x10 deg, 30 m, uint8 on GCS. ``lossyear`` encodes the year of loss
(1-24 = 2001-2024, 0 = no loss), so it is *categorical* — overviews use ``nearest`` and 0 is
real data, not nodata. Each tile is downloaded then re-COG'd; ocean tiles 404 (no such file).
"""

from __future__ import annotations

import requests

from .. import config, tile_index
from . import cog

ASSET = "forestLoss"
LAYER = "lossyear"

# Hansen Global Forest Change v1.12 (forestLoss / lossyear). 10x10 deg, 30 m tiles on GCS.
BASE = "https://storage.googleapis.com/earthenginepartners-hansen/GFC-2024-v1.12"
VERSION = "GFC-2024-v1.12"
# 10-degree tile origin grid (upper-left corner labels), per Hansen's naming convention.
LATS = [f"{d:02d}N" for d in range(10, 90, 10)] + ["00N"] + [
    f"{d:02d}S" for d in range(10, 60, 10)
]
LONS = [f"{d:03d}W" for d in range(10, 190, 10)] + [
    f"{d:03d}E" for d in range(0, 180, 10)
]


def _tile_url(lat: str, lon: str) -> str:
    return f"{BASE}/Hansen_{VERSION}_{LAYER}_{lat}_{lon}.tif"


def _dst(lat: str, lon: str) -> str:
    return config.staged_uri(ASSET, f"{LAYER}_{lat}_{lon}.tif")


def list_units(lats: list[str] | None = None, lons: list[str] | None = None) -> list[dict]:
    lats = lats or LATS
    lons = lons or LONS
    return [
        {"id": f"{lat}_{lon}", "lat": lat, "lon": lon, "url": _tile_url(lat, lon),
         "dst": _dst(lat, lon)}
        for lat in lats
        for lon in lons
    ]


def stage_unit(
    unit: dict,
    register_index: bool = True,
    missing_ok: bool = True,
) -> dict | None:
    """Stage one Hansen 10deg tile (whole tile). Returns None for ocean tiles (no such file)."""
    dst = _dst(unit["lat"], unit["lon"])
    try:
        footprint = cog.translate_to_cog(unit["url"], dst, resampling="nearest")
    except requests.HTTPError:
        if missing_ok:
            return None
        raise
    return tile_index.finalize(ASSET, dst, footprint, None, register_index)
