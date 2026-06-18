"""Stage Oxford MAP travel-time-to-cities (2015 accessibility surface) -> single global COG.

Single-epoch: one snapshot reused across all years. Continuous minutes, so overviews use
``average``. The Malaria Atlas DirectDownload URL serves a .zip (GeoTIFF + sidecars), read
via ``/vsizip``; override :data:`URL` if a different epoch/friction-derived surface is used.
"""

from __future__ import annotations

import os
import zipfile

from .. import config
from . import cog

ASSET = "accessibility"
# Oxford MAP travel time to cities (2015 accessibility surface). Single epoch.
URL = (
    "https://data.malariaatlas.org/geoserver/ows"
    "?service=CSW&version=2.0.1&request=DirectDownload"
    "&ResourceId=Explorer:2015_accessibility_to_cities_v1.0"
)


def _dst() -> str:
    return config.staged_uri(ASSET, f"{ASSET}.tif")


def list_units() -> list[dict]:
    return [{"id": "global", "url": URL, "dst": _dst()}]


def stage_unit(unit: dict | None = None) -> bool:
    unit = unit or {"url": URL}
    dst = _dst()
    # The DirectDownload URL serves a .zip (the GeoTIFF plus sidecars); read the tif via /vsizip.
    zip_path = cog.fetch(unit["url"], suffix=".zip")
    try:
        with zipfile.ZipFile(zip_path) as z:
            tif = next(n for n in z.namelist() if n.endswith(".tif"))
        cog.translate_to_cog(f"/vsizip/{zip_path}/{tif}", dst, resampling="average")
    finally:
        os.path.exists(zip_path) and os.remove(zip_path)
    return True
