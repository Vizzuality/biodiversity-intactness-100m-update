"""Stage SDPT v2.1 planted trees -> binary planted-mask COG (forestManagement provider).

Swappable alternative to Lesiv FML; both feed ``forestManagement``. Every SDPT polygon is a
planted stand, so staging burns a binary mask (FML instead passes raw class codes through).

Source is a ``.gdb.zip`` with one MultiPolygon layer per country (``<id>_plant_v21``), one Batch
job each. ~12% of layers are EPSG:3857/UTM, so :func:`_localized` reprojects to a local EPSG:4326
copy (``gdal_rasterize`` never reprojects); extent is the layer's own bounds read back from it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager

import pyogrio

from .. import config
from .. import cog

ASSET = "forestManagement"
PROVIDER = "sdpt"

URL = (
    "https://gfw-files.s3.amazonaws.com/plantations/SDPT_v2.1/"
    "sdpt_v21_v09152024_public.gdb.zip"
)
LAYER_SUFFIX = "_plant_v21"
# Region ids in the v2.1 GDB (one layer each).
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


# European ISO3s folded into the consolidated "eu" layer.
EU_ISO3 = {
    "ALB", "AND", "AUT", "BEL", "BGR", "BIH", "BLR", "CHE", "CZE", "DEU", "DNK", "ESP", "EST",
    "FIN", "FRA", "GBR", "GRC", "HRV", "HUN", "IRL", "ISL", "ITA", "LIE", "LTU", "LUX", "LVA",
    "MCO", "MDA", "MKD", "MLT", "MNE", "NLD", "NOR", "POL", "PRT", "ROU", "SMR", "SRB", "SVK",
    "SVN", "SWE", "UKR", "VAT", "XKX",
}


def regions_for(isos) -> set[str]:
    """sdpt regions for the given ISO3s (European countries fold into "eu")."""
    out = set()
    for iso in isos:
        if iso.lower() in REGIONS:
            out.add(iso.lower())
        elif iso in EU_ISO3:
            out.add("eu")
    return out


def list_units(regions: list[str] | None = None) -> list[dict]:
    regions = regions or REGIONS
    return [{"id": r, "region": r, "layer": _layer(r), "dst": _dst(r)} for r in regions]


@contextmanager
def _localized(src: str, layer: str):
    """``ogr2ogr`` the GDB ``layer`` to a local EPSG:4326 GeoPackage layer ``feat``; yield its path.

    ``/vsizip//vsicurl`` range-reads just this layer out of the ~7 GB monolithic GDB and ogr2ogr
    streams+reprojects in one pass, avoiding a full 7 GB download per Batch job.
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
    info = pyogrio.read_info(path, layer="feat")
    tb = info.get("total_bounds")
    return tuple(tb) if info.get("features") and tb is not None else None


def stage_unit(unit: dict) -> bool:
    layer = unit.get("layer") or _layer(unit["region"])
    dst = _dst(unit["region"])
    with _localized(_source_path(), layer) as local:
        extent = _layer_bounds(local)
        if extent is None:
            return False
        cog.rasterize_to_cog(local, dst, extent, layer="feat")
    return True
