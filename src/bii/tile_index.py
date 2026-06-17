"""Footprint index: build + query. Replaces the original private STAC API.

Each staged asset has a GeoParquet index of ``{geometry (EPSG:4326), uri}`` rows — one row
per staged COG tile (or, for ``landcover``, one row per in-place IO STAC item href). Every
asset is queried through one backend: a spatial query of the cached GeoParquet via geopandas
``.sindex``. :func:`lookup` answers "which tiles overlap this chunk?" for the processing
worker.

Staging writes the index: each unit registers a one-row *part* (so parallel Batch jobs don't
race on a single file); :func:`consolidate` merges parts into the asset index. :func:`build_index`
is the all-at-once path used locally and by the orchestrator (and by
:mod:`bii.staging.iolulc`, which pre-walks the IO STAC so landcover joins the staged backend
instead of a live per-chunk search).
"""

from __future__ import annotations

import hashlib

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


def _parts_prefix(asset: str, year: int | None = None) -> str:
    base = index_uri(asset, year)
    return base[: -len(".parquet")] + "_parts/"


def _part_uri(asset: str, uri: str, year: int | None = None) -> str:
    digest = hashlib.sha1(uri.encode()).hexdigest()[:16]
    return _parts_prefix(asset, year) + f"{digest}.parquet"


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
# Build / register / consolidate
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


def register(asset: str, uri: str, footprint, year: int | None = None) -> str:
    """Register one staged COG as a single-row *part* (race-free for parallel Batch jobs)."""
    gdf = _to_gdf([(uri, footprint)])
    part = _part_uri(asset, uri, year)
    _write_parquet(gdf, part)
    return part


def finalize(asset: str, dst: str, footprint, year: int | None, register_index: bool) -> dict:
    """Register the staged COG's footprint (as an index part) and return the staging result.

    The result dict ``{asset, uri, footprint, year, index_part}`` is the shape every
    ``stage_unit`` returns (see :mod:`bii.staging`)."""
    part = register(asset, dst, footprint, year) if register_index else None
    return {
        "asset": asset,
        "uri": dst,
        "footprint": list(footprint),
        "year": year,
        "index_part": part,
    }


def consolidate(asset: str, year: int | None = None) -> str:
    """Merge all registered parts into the asset index GeoParquet."""
    parts = [p for p in s3io.list_uris(_parts_prefix(asset, year)) if p.endswith(".parquet")]
    frames = [gpd.read_parquet(p) for p in parts]
    if not frames:
        raise FileNotFoundError(f"no index parts found for {asset} {year or ''}".strip())
    gdf = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=INDEX_CRS)
    gdf = gdf.drop_duplicates(subset="uri", keep="last").reset_index(drop=True)
    uri = index_uri(asset, year)
    _write_parquet(gdf, uri)
    return uri


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
