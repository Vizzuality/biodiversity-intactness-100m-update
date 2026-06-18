"""Stage OSM roads -> per-region 100 m burn-1 highway mask COG + footprint index (single-epoch).

Roads feed ``ln(distRoads + 1)``; one recent snapshot is reused across all years. The fan-out
unit is a Geofabrik leaf region (vendored manifest ``geofabrik-index-v1-child.geojson``), one
Batch job per region. Per region we download the ``.osm.pbf`` to ephemeral disk (a vector
source can't be pure-streamed), filter to vehicular ``highway`` ways, and rasterize to a 100 m
burn-1 mask on the BII grid.

Highway filtering is done by **osmctools** (``osmconvert`` | ``osmfilter``), the only backend —
it shrinks a region extract to just the highways we keep before GDAL reads it, and applies the
sub-type/tunnel drops (footpaths, cycleways, tracks, ...) that the GDAL OSM driver's bare
``highway IS NOT NULL`` could not. Both tools must be on ``PATH`` (see ``Dockerfile.roads``);
:func:`stage_unit` raises if they're missing. The filter set (:data:`OSM_HIGHWAY_DROP_VALUES`,
:data:`OSM_DROP_TUNNELS`) mirrors the rasterize-osm notebook's osmfilter pass. The filtered
``.osm.pbf`` is handed straight to
``gdal_rasterize`` (its ``lines`` layer), so road geometries are never read into Python/geopandas
— see :func:`bii.staging.cog.rasterize_to_cog`.

Per-region COGs have variable extent; cog_worker mosaics overlapping regions on the fly at
processing time and the footprint index handles overlap, so no global mosaic is built.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import geopandas as gpd

from .. import config
from . import cog

ASSET = "roads"

# Geofabrik leaf-region manifest (vendored FeatureCollection of {id, parent, name, pbf} +
# geometry) — the work-list, so list_units needs no network.
GEOFABRIK_INDEX = os.path.join(
    os.path.dirname(__file__), "data", "geofabrik-index-v1-child.geojson"
)
# Geofabrik leaf regions whose extents duplicate the union of their children. The vendored index
# already applies Geofabrik's parent-exclusion dedup (e.g. `california` is replaced by
# `norcal`/`socal`), but these macro-extents still overlap their children fully — rasterizing
# them would be redundant multi-GB downloads — so they're excluded from the work-list. This is the
# rasterize-osm notebook's manual `drop_ids` supplement. Children give full coverage:
# whole-US/US-census-regions -> `us/<state>`, GB unions -> English counties + scotland/wales,
# `dach` -> German states + austria/switzerland, `alps` -> member countries, etc.
GEOFABRIK_DROP_IDS = frozenset({
    "us", "us-south", "us-midwest", "us-northeast", "us-pacific", "us-west",
    "great-britain", "britain-and-ireland", "united-kingdom",
    "alps", "dach", "baden-wuerttemberg", "south-africa-and-lesotho", "indonesia",
})

# OSM highway filter, applied by osmfilter. Highways are kept, then tunnels and non-vehicular
# sub-types are dropped — footpaths, cycleways, tracks, etc. aren't "roads" for distRoads. Mirrors
# the rasterize-osm notebook's osmfilter pass exactly.
OSM_HIGHWAY_KEY = "highway"
OSM_DROP_TUNNELS = True
OSM_HIGHWAY_DROP_VALUES = (
    "cycleway", "footway", "path", "pedestrian", "steps", "track", "corridor",
    "elevator", "escalator", "proposed", "bridleway", "abandoned", "platform",
)

# The GDAL OSM driver exposes highways as the "lines" layer; gdal_rasterize burns it directly.
_OSM_LINES_LAYER = "lines"

_manifest_cache: gpd.GeoDataFrame | None = None


# --------------------------------------------------------------------------------------
# Geofabrik manifest (vendored, no network) -> staging units
# --------------------------------------------------------------------------------------
def _manifest() -> gpd.GeoDataFrame:
    global _manifest_cache
    if _manifest_cache is None:
        gdf = gpd.read_file(GEOFABRIK_INDEX)
        gdf = gdf[~gdf["id"].isin(GEOFABRIK_DROP_IDS)].reset_index(drop=True)
        _manifest_cache = gdf
    return _manifest_cache


def _dst(region_id: str) -> str:
    return config.staged_uri(ASSET, f"{region_id}.tif")


def list_units(regions: list[str] | None = None) -> list[dict]:
    gdf = _manifest()
    if regions is not None:
        gdf = gdf[gdf["id"].isin(regions)]
    units = []
    for _, row in gdf.iterrows():
        units.append(
            {
                "id": row["id"],
                "url": row["pbf"],
                "name": row.get("name"),
                "bounds": tuple(row.geometry.bounds),  # (w, s, e, n) EPSG:4326
                "dst": _dst(row["id"]),
            }
        )
    return units


# --------------------------------------------------------------------------------------
# Highway extraction (osmctools)
# --------------------------------------------------------------------------------------
def _require_osmctools() -> None:
    missing = [t for t in ("osmconvert", "osmfilter") if not shutil.which(t)]
    if missing:
        raise RuntimeError(
            f"roads staging requires osmctools but {', '.join(missing)} not on PATH; "
            "install osmctools (see Dockerfile.roads)."
        )


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _osmfilter_args() -> list[str]:
    """osmfilter tag-filter args: keep ``highway=*``, drop tunnels and non-vehicular sub-types."""
    key = OSM_HIGHWAY_KEY
    args = [f"--keep={key}="]
    if OSM_DROP_TUNNELS:
        args.append("--drop=tunnel=yes")
    if OSM_HIGHWAY_DROP_VALUES:
        # osmfilter reuses the last key for subsequent ` =value` terms, so this is one --drop:
        # `highway=cycleway =footway =path ...`.
        joined = " =".join(OSM_HIGHWAY_DROP_VALUES)
        args.append(f"--drop={key}={joined}")
    return args


def _filter_highways(source: str, tmpdir: str) -> str:
    """Filter a local ``.osm.pbf`` to vehicular highways via osmctools and return the path to a
    filtered ``.osm.pbf`` (under ``tmpdir``, which the caller owns and cleans up).

    ``osmfilter`` reads o5m/osm (not pbf) and re-reads its input twice, so it needs a seekable
    file rather than a pipe. The pipeline is therefore three steps via temp files:
    ``osmconvert -> o5m`` (relations dropped), ``osmfilter (keep/drop) -> o5m``,
    ``osmconvert -> pbf``. The pbf is then rasterized in place by ``gdal_rasterize`` (its ``lines``
    layer) — geometries never enter Python.
    """
    _require_osmctools()
    o5m = os.path.join(tmpdir, "in.o5m")
    roads_o5m = os.path.join(tmpdir, "roads.o5m")
    pbf = os.path.join(tmpdir, "roads.osm.pbf")
    _run(["osmconvert", source, "--drop-relations", "--out-o5m", f"-o={o5m}"])
    _run(["osmfilter", o5m, *_osmfilter_args(), "--out-o5m", f"-o={roads_o5m}"])
    _run(["osmconvert", roads_o5m, "--out-pbf", f"-o={pbf}"])
    return pbf


# --------------------------------------------------------------------------------------
# Stage one region
# --------------------------------------------------------------------------------------
def stage_unit(unit: dict) -> bool:
    dst = _dst(unit["id"])
    # A local pbf (tests / pre-staged extract) is read in place; a URL is fetched to disk first
    # (the OSM driver and osmctools both need random access a /vsicurl stream can't serve well).
    src = unit["url"]
    fetched = None
    if src.startswith(("http://", "https://")):
        fetched = cog.fetch(src, suffix=".osm.pbf")
        src = fetched

    # Filter to highways, then gdal_rasterize burns the filtered pbf's "lines" layer straight to
    # the BII grid over the region's extent (``unit["bounds"]``, from the Geofabrik manifest) —
    # road geometries never enter Python. A region with no highways burns to an all-zero mask.
    tmpdir = tempfile.mkdtemp(prefix="osmroads_")
    try:
        pbf = _filter_highways(src, tmpdir)
        cog.rasterize_to_cog(pbf, dst, unit["bounds"], layer=_OSM_LINES_LAYER)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        if fetched:
            os.path.exists(fetched) and os.remove(fetched)

    return True
