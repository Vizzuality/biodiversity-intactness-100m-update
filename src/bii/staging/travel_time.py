"""Stage Oxford MAP travel-time-to-cities (2015 accessibility surface) -> single global COG.

Single-epoch: one snapshot reused across all years. Continuous minutes, so overviews use
``average``. The Malaria Atlas DirectDownload URL serves the GeoTIFF; override
:data:`URL` if a different epoch/friction-derived surface is used.
"""

from __future__ import annotations

from .. import config, tile_index
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


def stage_unit(
    unit: dict | None = None,
    register_index: bool = True,
) -> dict | None:
    unit = unit or {"url": URL}
    dst = _dst()
    footprint = cog.translate_to_cog(unit["url"], dst, resampling="average")
    return tile_index.finalize(ASSET, dst, footprint, None, register_index)
