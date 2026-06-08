"""Footprint index: build + query. Replaces the original private STAC API.

Each staged asset has a GeoParquet index of ``{geometry (EPSG:4326), uri}`` rows — one row
per staged COG tile. :func:`lookup` answers "which tiles overlap this chunk?" for the
processing worker, with two backends behind one interface:

* **staged assets** — spatial query the cached GeoParquet via geopandas ``.sindex``.
* **landcover** — a live STAC search against the Impact Observatory ``io-10m-annual-lulc``
  collection (never staged; covers 2017-2024, AWS-hosted).

Staging writes the index: each unit registers a one-row *part* (so parallel Batch jobs don't
race on a single file); :func:`consolidate` merges parts into the asset index. :func:`build_index`
is the all-at-once path used locally and by the orchestrator.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from urllib.parse import urlparse

import geopandas as gpd
import pandas as pd
from shapely.geometry import box, shape

from . import config
from .staging import cog

INDEX_CRS = "EPSG:4326"

# Impact Observatory LULC (landcover) — read live from this STAC, never staged. Covers
# 2017-2024, AWS-hosted.
LULC_STAC_URL = "https://api.impactobservatory.com/stac-aws"
LULC_COLLECTION = "io-10m-annual-lulc"


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
# GeoParquet I/O (routes s3 through temp files so we don't need s3fs)
# --------------------------------------------------------------------------------------
def _read_parquet(uri: str) -> gpd.GeoDataFrame:
    if cog.is_s3(uri):
        bucket, key = cog._split_s3(uri)
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
            cog._s3_client().download_file(bucket, key, tmp.name)
            gdf = gpd.read_parquet(tmp.name)
        os.remove(tmp.name)
        return gdf
    return gpd.read_parquet(uri)


def _write_parquet(gdf: gpd.GeoDataFrame, uri: str) -> None:
    if cog.is_s3(uri):
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
            gdf.to_parquet(tmp.name)
            cog.upload(tmp.name, uri)
        os.remove(tmp.name)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(uri)), exist_ok=True)
        gdf.to_parquet(uri)


def _list(prefix_uri: str) -> list[str]:
    """List object URIs under a prefix (s3 or local directory)."""
    if cog.is_s3(prefix_uri):
        p = urlparse(prefix_uri)
        bucket, prefix = p.netloc, p.path.lstrip("/")
        client = cog._s3_client()
        out: list[str] = []
        token = None
        while True:
            kw = dict(Bucket=bucket, Prefix=prefix)
            if token:
                kw["ContinuationToken"] = token
            resp = client.list_objects_v2(**kw)
            out += [f"s3://{bucket}/{o['Key']}" for o in resp.get("Contents", [])]
            if not resp.get("IsTruncated"):
                break
            token = resp["NextContinuationToken"]
        return out
    if os.path.isdir(prefix_uri):
        return [os.path.join(prefix_uri, f) for f in os.listdir(prefix_uri)]
    return []


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
    if append and cog.exists(uri):
        gdf = gpd.GeoDataFrame(
            pd.concat([_read_parquet(uri), gdf], ignore_index=True),
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


def consolidate(asset: str, year: int | None = None) -> str:
    """Merge all registered parts into the asset index GeoParquet."""
    parts = [p for p in _list(_parts_prefix(asset, year)) if p.endswith(".parquet")]
    frames = [_read_parquet(p) for p in parts]
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

    ``landcover`` is resolved live from the IO STAC; everything else from its GeoParquet."""
    if asset == "landcover":
        return _lookup_lulc(bounds, year)
    return _lookup_staged(asset, bounds, year)


def _lookup_staged(asset: str, bounds, year: int | None) -> list[str]:
    uri = index_uri(asset, year)
    if not cog.exists(uri):
        return []
    gdf = _read_parquet(uri)
    query = box(*bounds)
    idx = list(gdf.sindex.query(query, predicate="intersects"))
    return gdf.iloc[idx]["uri"].tolist()


def _lookup_lulc(bounds, year: int | None) -> list[str]:
    import pystac_client

    client = pystac_client.Client.open(LULC_STAC_URL)
    search_kw = dict(collections=[LULC_COLLECTION], bbox=list(bounds), limit=500)
    if year is not None:
        search_kw["datetime"] = f"{year}-01-01/{year}-12-31"
    hrefs: list[str] = []
    for item in client.search(**search_kw).items():
        for key, asset in item.assets.items():
            mt = (asset.media_type or "").lower()
            if "tif" in mt or key in ("data", "supercell"):
                hrefs.append(asset.href)
    return hrefs
