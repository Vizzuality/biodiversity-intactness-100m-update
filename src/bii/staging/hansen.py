"""Stage Hansen Global Forest Change ``lossyear`` -> forestLoss COGs (one job per 10deg tile).

``lossyear`` encodes year of loss (1-24 = 2001-2024, 0 = no loss): categorical, so overviews use
``nearest`` and 0 is real data not nodata. Ocean tiles 404.
"""

from __future__ import annotations

import requests

from .. import config
from .. import cog

ASSET = "forestLoss"
LAYER = "lossyear"

BASE = "https://storage.googleapis.com/earthenginepartners-hansen/GFC-2024-v1.12"
VERSION = "GFC-2024-v1.12"
# 10deg tile grid, labeled by upper-left corner per Hansen's naming convention.
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


def stage_unit(unit: dict) -> bool:
    """Stage one Hansen 10deg tile. A 404 is a valid ocean tile -> skip (``False``)."""
    dst = _dst(unit["lat"], unit["lon"])
    try:
        cog.translate_to_cog(unit["url"], dst, resampling="nearest")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return False
        raise
    return True
