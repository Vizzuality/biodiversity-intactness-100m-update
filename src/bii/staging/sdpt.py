"""Stage SDPT v2.1 planted trees -> normalized planted-mask COG (forestManagement provider).

SDPT is the swappable alternative to Lesiv FML (:mod:`bii.staging.forest_management`): both
feed the same ``forestManagement`` asset so the model is provider-agnostic (``sources.py``
picks which one). Every SDPT polygon is a planted stand, so staging burns a binary planted
mask (``1`` wherever a polygon falls); FML instead passes its raw class codes through, and the
consumer normalizes whichever provider it reads.

The source is a file geodatabase (``.gdb.zip``) with one MultiPolygon layer per country/region
(``<id>_plant_v21``), read in place via ``/vsizip//vsicurl`` (the S3 host supports range
requests). Each region layer is one staging unit (one Batch job per country, like WorldPop).
Rasterization is done by ``gdal_rasterize`` straight from the GDB layer (see
:func:`bii.staging.cog.rasterize_to_cog`) — polygons are never read into Python/geopandas. A
region with no polygons in the requested window is skipped, keeping the index lean.
"""

from __future__ import annotations

from .. import config
from . import _base, cog

ASSET = "forestManagement"
PROVIDER = "sdpt"

# SDPT v2.1 planted trees (alternative forestManagement provider), file geodatabase. The GDB
# holds one MultiPolygon layer per country/region, named ``<id>_plant_v21``; read in place via
# ``/vsizip//vsicurl`` (S3 host supports range requests). Each layer is a staging unit (one Batch
# job per country, like WorldPop). Every polygon is a planted stand, so staging burns a binary
# planted mask (the swap point with FML, which instead passes its raw class codes through).
URL = (
    "https://gfw-files.s3.amazonaws.com/plantations/SDPT_v2.1/"
    "sdpt_v21_v09152024_public.gdb.zip"
)
LAYER_SUFFIX = "_plant_v21"
# Region ids present in the v2.1 GDB (one MultiPolygon layer each). Static so list_units needs no
# network; verify against pyogrio.list_layers if the release changes.
REGIONS = [
    "ago", "arg", "arm", "aus", "aze", "bdi", "ben", "bfa", "bgd", "blz", "bol", "bra",
    "brn", "btn", "caf", "can", "chl", "chn", "civ", "cmr", "cod", "cog", "col", "cpv",
    "cri", "cub", "cyp", "dom", "dza", "ecu", "egy", "eri", "eth", "eu", "fji", "gab",
    "gha", "gin", "glp", "gmb", "gnb", "gnq", "gtm", "guf", "hnd", "hti", "idn", "ind",
    "irn", "irq", "isr", "jam", "jor", "jpn", "kaz", "ken", "kgz", "khm", "kor", "lao",
    "lbn", "lbr", "lby", "lka", "lso", "mar", "mdg", "mex", "mli", "mmr", "mng", "moz",
    "mrt", "mwi", "mys", "ncl", "nga", "nic", "npl", "nzl", "omn", "pak", "pan", "per",
    "phl", "prk", "pry", "rus", "rwa", "sen", "slb", "sle", "slv", "som", "ssd", "stp",
    "sur", "swz", "syr", "tgo", "tha", "tjk", "tto", "tun", "tur", "tza", "uga", "ury",
    "usa", "uzb", "ven", "vnm", "vut", "zaf", "zmb", "zwe",
]


def _source_path() -> str:
    return f"/vsizip//vsicurl/{URL}"


def _layer(region: str) -> str:
    return f"{region}{LAYER_SUFFIX}"


def _dst(region: str) -> str:
    return config.staged_uri(ASSET, f"{PROVIDER}_{region}.tif")


def list_units(regions: list[str] | None = None) -> list[dict]:
    regions = regions or REGIONS
    return [{"id": r, "region": r, "layer": _layer(r)} for r in regions]


def stage_unit(
    unit: dict,
    *,
    bounds: tuple[float, float, float, float] | None = None,
    overwrite: bool = False,
    register_index: bool = True,
    skip_empty: bool = True,
    **_,
) -> dict | None:
    layer = unit.get("layer") or _layer(unit["region"])
    dst = _dst(unit["region"])
    # gdal_rasterize burns the GDB layer (clipped to ``bounds`` when set, else its full extent)
    # straight to the BII grid; polygons never enter Python. ``bounds=None`` -> cog reads the
    # layer extent from metadata. An empty window/layer -> all-fill burn -> skipped (None).
    # rasterize_to_cog skips the burn when ``dst`` already exists (unless ``overwrite``).
    footprint = cog.rasterize_to_cog(
        _source_path(),
        dst,
        bounds,
        layer=layer,
        dtype="uint8",
        burn=1,
        overwrite=overwrite,
        skip_empty=skip_empty,
    )
    if footprint is None:
        return None
    return _base.finalize(ASSET, dst, footprint, None, register_index)
