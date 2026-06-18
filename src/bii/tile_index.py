"""Footprint index: build + query. Replaces the original private STAC API.

Each staged asset has a GeoParquet index of ``{geometry (EPSG:4326), uri}`` rows — one row
per staged COG tile (or, for ``landcover``, one row per in-place IO STAC item href). Every
asset is queried through one backend: a spatial query of the cached GeoParquet via geopandas
``.sindex``. :func:`lookup` answers "which tiles overlap this chunk?" for the processing
worker.

Staging never writes the index. Workers only write COGs (atomically, via
:func:`bii.s3io.staged_local_path`), so the index is rebuilt after a run from the COGs that
actually landed: :func:`index_cogs` enumerates an asset's staged COGs and reads each one's
header footprint. This makes the index a pure function of the bucket — rebuildable any time,
and immune to a worker dying between writing a COG and recording it. :func:`build_index` is the
explicit ``(uri, geometry)`` path used by :mod:`bii.staging.iolulc`, which pre-walks the IO STAC
so landcover joins the staged backend instead of a live per-chunk search.
"""

from __future__ import annotations

import os

import geopandas as gpd
import pandas as pd
from shapely.geometry import box, shape

from . import config, s3io

INDEX_CRS = "EPSG:4326"


# --------------------------------------------------------------------------------------
# Index locations
# --------------------------------------------------------------------------------------
def index_uri(asset: str, year: int | None = None) -> str:
    if year is None:
        return config.staged_uri(asset, f"{asset}_index.parquet")
    return config.staged_uri(asset, str(year), f"{asset}_{year}_index.parquet")


# --------------------------------------------------------------------------------------
# GeoParquet I/O (gpd reads s3:// directly; writes stage through s3io)
# --------------------------------------------------------------------------------------
def _write_parquet(gdf: gpd.GeoDataFrame, uri: str) -> None:
    with s3io.staged_local_path(uri) as path:
        gdf.to_parquet(path)


def _to_gdf(footprints) -> gpd.GeoDataFrame:
    """Build a GeoDataFrame from ``(uri, geom_or_bounds)`` pairs."""
    geoms, uris = [], []
    for uri, geom in footprints:
        if isinstance(geom, (tuple, list)):  # (west, south, east, north)
            geoms.append(box(*geom))
        elif hasattr(geom, "geom_type"):  # already a shapely geometry
            geoms.append(geom)
        else:  # geojson-like mapping
            geoms.append(shape(geom))
        uris.append(uri)
    return gpd.GeoDataFrame({"uri": uris}, geometry=geoms, crs=INDEX_CRS)


# --------------------------------------------------------------------------------------
# Build / rebuild-from-COGs
# --------------------------------------------------------------------------------------
def build_index(asset: str, footprints, year: int | None = None, append: bool = False) -> str:
    """Write ``{geometry, uri}`` GeoParquet for ``asset``. ``footprints`` is an iterable of
    ``(uri, geometry | (west, south, east, north))``. Dedupes by uri."""
    gdf = _to_gdf(footprints)
    uri = index_uri(asset, year)
    if append and s3io.exists(uri):
        gdf = gpd.GeoDataFrame(
            pd.concat([gpd.read_parquet(uri), gdf], ignore_index=True),
            crs=INDEX_CRS,
        )
    gdf = gdf.drop_duplicates(subset="uri", keep="last").reset_index(drop=True)
    _write_parquet(gdf, uri)
    return uri


def cog_footprint(uri: str) -> tuple[float, float, float, float]:
    """Read a staged COG's EPSG:4326 footprint ``(west, south, east, north)`` from its header."""
    import rasterio as rio
    from rasterio.warp import transform_bounds

    from .staging.cog import GDAL_READ_ENV
    with rio.Env(**GDAL_READ_ENV), rio.open(uri) as s:
        return tuple(transform_bounds(s.crs, INDEX_CRS, *s.bounds))


def _asset_cogs(asset: str, year: int | None) -> list[str]:
    """Staged COG URIs for ``asset``: every ``.tif`` under its prefix (recursive), filtered to the
    year — annual assets embed the year in the COG path, single-epoch assets take all."""
    prefix = config.staged_uri(asset)
    if s3io.is_s3(prefix):
        uris = s3io.list_uris(prefix + "/")  # list_objects_v2 is recursive
    else:
        uris = [os.path.join(r, f) for r, _, fs in os.walk(prefix) for f in fs] \
            if os.path.isdir(prefix) else []
    return [u for u in uris if u.endswith(".tif") and (year is None or str(year) in u)]


def index_cogs(asset: str, year: int | None = None) -> str:
    """Rebuild ``asset``'s index from its staged COGs, reading each one's header footprint."""
    uris = _asset_cogs(asset, year)
    if not uris:
        raise FileNotFoundError(f"no staged COGs for {asset} {year or ''}".strip())
    return build_index(asset, [(u, cog_footprint(u)) for u in uris], year=year)


# --------------------------------------------------------------------------------------
# Lookup
# --------------------------------------------------------------------------------------
def lookup(asset: str, bounds: tuple[float, float, float, float], year: int | None = None) -> list[str]:
    """Return the source URIs whose footprints intersect ``bounds`` (EPSG:4326).

    All assets — including resolve from their GeoParquet index."""
    uri = index_uri(asset, year)
    if not s3io.exists(uri):
        return []
    gdf = gpd.read_parquet(uri)
    query = box(*bounds)
    idx = list(gdf.sindex.query(query, predicate="intersects"))
    return gdf.iloc[idx]["uri"].tolist()


def read_index(asset: str, year: int | None = None) -> gpd.GeoDataFrame | None:
    """Return ``asset``'s footprint index as a GeoDataFrame, or ``None`` if it doesn't exist.

    Unlike :func:`lookup` (one spatial query per call), this hands back the whole frame so a
    caller — the orchestrator's ocean-drop coverage — can build one ``.sindex`` and reuse it
    against thousands of chunks without re-reading the parquet each time.
    """
    uri = index_uri(asset, year)
    if not s3io.exists(uri):
        return None
    return gpd.read_parquet(uri)
