"""Stage Lesiv Forest Management Layer v3.2 -> forestManagement COG.

Categorical management-class codes (int8, nodata -128) passed through unchanged; any
managed-forest decode is the consumer's job (the swap point with :mod:`bii.staging.sdpt`).
Categorical -> overviews use ``nearest``.
"""

from __future__ import annotations

from .. import config
from .. import cog

ASSET = "forestManagement"
PROVIDER = "fml"
# Managed-forest codes (31 replanted, 32 woody plantation,  40 oil palm, 53 agroforestry).
URL = "https://zenodo.org/records/4541513/files/FML_v3.2.tif"


def _dst() -> str:
    return config.staged_uri(ASSET, f"{PROVIDER}.tif")


def list_units() -> list[dict]:
    return [{"id": PROVIDER, "url": URL, "dst": _dst()}]


def stage_unit(unit: dict | None = None) -> bool:
    unit = unit or {"url": URL}
    dst = _dst()
    cog.translate_to_cog(unit["url"], dst, resampling="nearest")
    return True
