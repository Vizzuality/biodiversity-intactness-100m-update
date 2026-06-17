"""Stage Lesiv Forest Management Layer v3.2 -> forestManagement COG.

FML is a single global 100 m GeoTIFF of categorical management-class codes (int8, nodata
-128). Staging copies those codes through unchanged (no class selection) via a whole-raster
re-COG — any managed-forest decode is the consumer's job, so this stays the swap point with
:mod:`bii.staging.sdpt`. Codes are categorical -> overviews use ``nearest``.

Single-source like :mod:`bii.staging.nightlights` / :mod:`bii.staging.travel_time`: one
staging unit, one Batch job, streamed in place via ``/vsicurl`` (Zenodo supports ranges).
"""

from __future__ import annotations

from .. import config, tile_index
from . import cog

ASSET = "forestManagement"
PROVIDER = "fml"
# Lesiv Forest Management Layer v3.2 (managed forest), single epoch, global 100 m. Staged as raw
# categorical management-class codes (no class selection); any managed-forest decode (codes >30 &
# <55: 31 replanted, 32 woody plantation, 40 oil palm, 53 agroforestry) is the consumer's job.
URL = "https://zenodo.org/records/4541513/files/FML_v3.2.tif"


def _dst() -> str:
    return config.staged_uri(ASSET, f"{PROVIDER}.tif")


def list_units() -> list[dict]:
    return [{"id": PROVIDER, "url": URL, "dst": _dst()}]


def stage_unit(
    unit: dict | None = None,
    register_index: bool = True,
) -> dict | None:
    unit = unit or {"url": URL}
    dst = _dst()
    footprint = cog.translate_to_cog(unit["url"], dst, resampling="nearest")
    return tile_index.finalize(ASSET, dst, footprint, None, register_index)
