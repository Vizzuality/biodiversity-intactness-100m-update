"""Stage OSM roads -> per-region 100 m burn-1 highway mask COG (single-epoch).

One snapshot reused across all years. Fan-out unit is a Geofabrik leaf region, 
one Batch job each. Highway filtering uses osmctools (osmconvert | osmfilter), which
applies sub-type/tunnel drops.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import geopandas as gpd

from .. import config
from .. import cog

ASSET = "roads"

# Vendored {id, parent, name, pbf} + geometry so list_units needs no network.
GEOFABRIK_INDEX = os.path.join(
    os.path.dirname(__file__), "data", "geofabrik-index-v1.geojson"
)
# Leaf macro-regions dropped: fully covered by finer leaves also in the
# work-list (us -> us/<state>, GB unions -> counties, dach/alps -> member countries). `enfield` has
# no public PBF so keep parent `greater-london` instead.
GEOFABRIK_DROP_IDS = frozenset({
    "us", "us-south", "us-midwest", "us-northeast", "us-pacific", "us-west",
    "great-britain", "britain-and-ireland", "united-kingdom",
    "alps", "dach", "baden-wuerttemberg", "south-africa-and-lesotho", "indonesia",
    "enfield",
})
GEOFABRIK_KEEP_PARENTS = frozenset({"greater-london"})

# Drop non-vehicular sub-types and tunnels
OSM_HIGHWAY_KEY = "highway"
OSM_DROP_TUNNELS = True
OSM_HIGHWAY_DROP_VALUES = (
    "cycleway", "footway", "path", "pedestrian", "steps", "track", "corridor",
    "elevator", "escalator", "proposed", "bridleway", "abandoned", "platform",
)

# GDAL OSM driver exposes highways as the "lines" layer.
_OSM_LINES_LAYER = "lines"

_manifest_cache: gpd.GeoDataFrame | None = None


def _manifest() -> gpd.GeoDataFrame:
    global _manifest_cache
    if _manifest_cache is None:
        gdf = gpd.read_file(GEOFABRIK_INDEX)
        parents = set(gdf["parent"].dropna())
        leaf = ~gdf["id"].isin(parents) | gdf["id"].isin(GEOFABRIK_KEEP_PARENTS)
        gdf = gdf[leaf & ~gdf["id"].isin(GEOFABRIK_DROP_IDS)].reset_index(drop=True)
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


def _require_osmctools() -> None:
    missing = [t for t in ("osmconvert", "osmfilter") if not shutil.which(t)]
    if missing:
        raise RuntimeError(
            f"roads staging requires osmctools but {', '.join(missing)} not on PATH; "
            "install osmctools (see Dockerfile)."
        )


def _osmfilter_args() -> list[str]:
    key = OSM_HIGHWAY_KEY
    args = [f"--keep={key}="]
    if OSM_DROP_TUNNELS:
        args.append("--drop=tunnel=yes")
    if OSM_HIGHWAY_DROP_VALUES:
        # osmfilter reuses the last key for ` =value` terms: `highway=cycleway =footway ...`.
        joined = " =".join(OSM_HIGHWAY_DROP_VALUES)
        args.append(f"--drop={key}={joined}")
    return args


def _filter_highways(source: str, tmpdir: str) -> str:
    """Filter a local ``.osm.pbf`` to vehicular highways, returning a filtered ``.osm.pbf``.

    osmfilter reads o5m/osm (not pbf) and re-reads its input twice, so it needs a seekable file, not
    a pipe — hence three steps via temp files: osmconvert -> o5m, osmfilter -> o5m, osmconvert -> pbf.
    """
    _require_osmctools()
    o5m = os.path.join(tmpdir, "in.o5m")
    roads_o5m = os.path.join(tmpdir, "roads.o5m")
    pbf = os.path.join(tmpdir, "roads.osm.pbf")
    subprocess.run(["osmconvert", source, "--drop-relations", "--out-o5m", f"-o={o5m}"], check=True)
    subprocess.run(["osmfilter", o5m, *_osmfilter_args(), "--out-o5m", f"-o={roads_o5m}"], check=True)
    subprocess.run(["osmconvert", roads_o5m, "--out-pbf", f"-o={pbf}"], check=True)
    return pbf


def stage_unit(unit: dict) -> bool:
    dst = _dst(unit["id"])
    src = unit["url"]
    fetched = None
    if src.startswith(("http://", "https://")):
        fetched = cog.fetch(src, suffix=".osm.pbf")
        src = fetched

    tmpdir = tempfile.mkdtemp(prefix="osmroads_")
    try:
        pbf = _filter_highways(src, tmpdir)
        cog.rasterize_to_cog(pbf, dst, unit["bounds"], layer=_OSM_LINES_LAYER)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        if fetched:
            os.path.exists(fetched) and os.remove(fetched)

    return True
