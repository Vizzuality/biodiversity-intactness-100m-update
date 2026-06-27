"""Footprint index: build + query.

Each staged asset has a GeoParquet index of ``{geometry (EPSG:4326), uri}`` rows — one row per
staged COG tile (or, for ``landcover``, one row per in-place IO STAC item href), queried via
geopandas ``.sindex``.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import geopandas as gpd
from shapely.geometry import box, shape

from . import config, io
from . import cog

INDEX_CRS = "EPSG:4326"


def index_uri(asset: str, year: int | None = None) -> str:
    if year is None:
        return config.staged_uri(asset, f"{asset}_index.parquet")
    return config.staged_uri(asset, str(year), f"{asset}_{year}_index.parquet")


def _write_parquet(gdf: gpd.GeoDataFrame, uri: str) -> None:
    with io.staged_local_path(uri) as path:
        gdf.to_parquet(path)


def _to_gdf(footprints) -> gpd.GeoDataFrame:
    """Build a GeoDataFrame from ``(uri, geom_or_bounds)`` pairs."""
    geoms, uris = [], []
    for uri, geom in footprints:
        if isinstance(geom, (tuple, list)):  # (west, south, east, north)
            geoms.append(box(*geom))
        elif hasattr(geom, "geom_type"):
            geoms.append(geom)
        else:  # geojson-like mapping
            geoms.append(shape(geom))
        uris.append(uri)
    return gpd.GeoDataFrame({"uri": uris}, geometry=geoms, crs=INDEX_CRS)


def build_index(asset: str, footprints, year: int | None = None) -> str:
    """Write ``{geometry, uri}`` GeoParquet for ``asset``. ``footprints`` is an iterable of
    ``(uri, geometry | (west, south, east, north))``. Dedupes by uri."""
    gdf = _to_gdf(footprints).drop_duplicates(subset="uri").reset_index(drop=True)
    uri = index_uri(asset, year)
    _write_parquet(gdf, uri)
    return uri


def _asset_cogs(asset: str, year: int | None) -> list[str]:
    """Staged COG URIs for ``asset`` filtered to ``year`` — annual assets embed the year in the
    COG path; single-epoch assets (``year=None``) take all."""
    uris = io.list_uris(config.staged_uri(asset) + "/")
    return [u for u in uris if u.endswith(".tif") and (year is None or str(year) in u)]


def index_cogs(asset: str, year: int | None = None) -> str:
    """Rebuild ``asset``'s index from its staged COGs."""
    uris = _asset_cogs(asset, year)
    if not uris:
        raise FileNotFoundError(f"no staged COGs for {asset} {year or ''}".strip())
    # Parallel reads of COG headers
    with ThreadPoolExecutor(max_workers=16) as ex:
        footprints = ex.map(lambda u: cog.footprint(u, INDEX_CRS), uris)
        return build_index(asset, zip(uris, footprints), year=year)


def lookup(asset: str, bounds: tuple[float, float, float, float], year: int | None = None) -> list[str]:
    """Return source URIs whose footprints intersect ``bounds`` (EPSG:4326)."""
    uri = index_uri(asset, year)
    if not io.exists(uri):
        return []
    gdf = gpd.read_parquet(uri)
    query = box(*bounds)
    idx = list(gdf.sindex.query(query, predicate="intersects"))
    return gdf.iloc[idx]["uri"].tolist()


def read_index(asset: str, year: int | None = None) -> gpd.GeoDataFrame | None:
    """``asset``'s footprint index as a GeoDataFrame, or ``None`` if absent."""
    uri = index_uri(asset, year)
    if not io.exists(uri):
        return None
    return gpd.read_parquet(uri)
