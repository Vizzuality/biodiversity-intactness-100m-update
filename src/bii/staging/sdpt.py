"""Stage SDPT v2.1 planted trees -> normalized planted-mask COG (forestManagement provider).

SDPT is the swappable alternative to Lesiv FML (:mod:`bii.staging.forest_management`): both
feed the same ``forestManagement`` asset so the model is provider-agnostic (``sources.py``
picks which one). Every SDPT polygon is a planted stand, so staging burns a binary planted
mask (``1`` wherever a polygon falls); FML instead passes its raw class codes through, and the
consumer normalizes whichever provider it reads.

The source is a file geodatabase (``.gdb.zip``) with one MultiPolygon layer per country/region
(``<id>_plant_v21``), reached via ``/vsizip//vsicurl`` (the S3 host supports range requests).
Each region layer is one staging unit (one Batch job per country, like WorldPop). Because the GDB
is remote and ~12% of the country layers are EPSG:3857/UTM rather than 4326, :func:`_localized`
reprojects the layer to a local EPSG:4326 copy with ``ogr2ogr`` first (``gdal_rasterize`` burns
onto the degree grid as-is and never reprojects); :func:`bii.staging.cog.rasterize_to_cog` then
burns that copy — polygons are never read into Python/geopandas. The per-country COG extent is the
layer's own bounds, read back from the localized copy, so it needs no externally supplied extent.
A region with no polygons is skipped, keeping the index lean.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager

import pyogrio

from .. import config, tile_index
from . import cog

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
    return [{"id": r, "region": r, "layer": _layer(r), "dst": _dst(r)} for r in regions]


@contextmanager
def _localized(src: str, layer: str):
    """``ogr2ogr`` the GDB ``layer`` to a local EPSG:4326 GeoPackage layer ``feat`` and yield its
    path; the temp dir is removed on exit.

    ``/vsizip//vsicurl`` range-reads just this country's layer out of the ~7 GB monolithic GDB,
    where downloading to reproject in geopandas would fetch all 7 GB per Batch job and load the
    layer into RAM. ogr2ogr streams (geometries never enter Python) and reprojects in the same
    pass — needed since ~12% of layers are EPSG:3857/UTM and ``gdal_rasterize`` never reprojects.
    """
    flags = [f for k, v in cog.GDAL_READ_ENV.items() for f in ("--config", k, str(v))]
    cmd = ["ogr2ogr", *flags, "-t_srs", "EPSG:4326", "-f", "GPKG", "-nln", "feat"]
    tmpdir = tempfile.mkdtemp(prefix="bii_sdpt_")
    try:
        out = os.path.join(tmpdir, "src.gpkg")
        cmd += [out, src]
        if layer:
            cmd.append(layer)
        subprocess.run(cmd, check=True)
        yield out
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _layer_bounds(path: str) -> tuple[float, float, float, float] | None:
    """The localized layer's EPSG:4326 extent (its features' total bounds), or ``None`` if empty."""
    info = pyogrio.read_info(path, layer="feat")
    tb = info.get("total_bounds")
    return tuple(tb) if info.get("features") and tb is not None else None


def stage_unit(
    unit: dict,
    register_index: bool = True,
) -> dict | None:
    layer = unit.get("layer") or _layer(unit["region"])
    dst = _dst(unit["region"])
    # Reproject the remote GDB layer to a local EPSG:4326 copy, then burn it onto the grid over the
    # layer's own extent (read back from that copy). A layer with no polygons is skipped (None).
    with _localized(_source_path(), layer) as local:
        extent = _layer_bounds(local)
        if extent is None:
            return None
        footprint = cog.rasterize_to_cog(local, dst, extent, layer="feat")
    return tile_index.finalize(ASSET, dst, footprint, None, register_index)
