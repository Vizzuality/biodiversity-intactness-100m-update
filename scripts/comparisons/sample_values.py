#!/usr/bin/env python
"""Sample comparison datasets at the stratified points and write values to parquet.

Datasets: our 2020 BII (tiled COGs in S3), the Planetary Computer io-biodiversity product, 2020
gHM, NHM BII v2.1.1, the expert-elicited Africa BII, and a 2017-2025 BII timeseries (one
``bii_<year>`` column per year). Points come from sample_points.py. Tiled sources (our S3 BII,
PC, timeseries) are sampled by first assigning each point to its tile, then opening each COG once
so only the touched blocks are fetched (one GET per block), instead of an open-per-point.

    uv run --extra dev python scripts/sample_values.py
    uv run --extra dev python scripts/sample_values.py --strata biome --datasets bii timeseries
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio as rio
from rasterio.warp import transform

from bii import config, tile_index
from bii.cog import GDAL_READ_ENV

DATA = Path("data/compare")
YEAR = 2020
LOCAL = {
    "ghm": DATA / "ghm_2020_AA_300m.tif",
    "nhm_bii": DATA / "bii-v2-1-1-nhm-data-portal" / "bii-2020_v2-1-1.tif",
    "bii_africa": DATA / "bii_africa_1km.tif",
}
PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
PC_COLLECTION = "io-biodiversity"


def _sample(uri: str, lons: np.ndarray, lats: np.ndarray) -> np.ndarray:
    """Band-1 values at lon/lat points; nodata (masked, dtype-correct) and out-of-extent -> nan."""
    with rio.Env(**GDAL_READ_ENV), rio.open(uri) as s:
        xs, ys = (transform("EPSG:4326", s.crs, list(lons), list(lats))
                  if s.crs and s.crs.to_epsg() != 4326 else (lons, lats))
        vals = np.array([np.nan if np.ma.getmaskarray(v).any() else float(v[0])
                         for v in s.sample(zip(xs, ys), indexes=1, masked=True)])
        w, so, e, no = s.bounds
        vals[(np.asarray(xs) < w) | (np.asarray(xs) > e)
             | (np.asarray(ys) < so) | (np.asarray(ys) > no)] = np.nan
    return vals


@lru_cache
def _catalog() -> gpd.GeoDataFrame:
    """The run's full STAC catalog GeoParquet, fetched from S3 once and reused across years."""
    g = gpd.read_parquet(config.out_uri(config.RUN_ID, "catalog.parquet"))
    g["uri"] = g["assets"].apply(lambda a: a["data"]["href"])
    return g[["datetime", "uri", "geometry"]]


def _tile_index(year: int) -> gpd.GeoDataFrame:
    """{uri, footprint} per BII tile for ``year``."""
    g = _catalog()
    return g[g["datetime"].dt.year == year][["uri", "geometry"]]


def _sample_by_tile(lons: np.ndarray, lats: np.ndarray, tiles: gpd.GeoDataFrame) -> np.ndarray:
    """Sample points against a {uri, geometry} tile index: one open + block reads per tile."""
    pts = gpd.GeoDataFrame(geometry=gpd.points_from_xy(lons, lats), crs="EPSG:4326")
    hit = gpd.sjoin(pts, tiles, how="inner", predicate="within").groupby(level=0)["uri"].first()

    out = np.full(len(lons), np.nan)

    def _tile(item):
        uri, idx = item
        idx = np.asarray(idx)
        return idx, _sample(uri, lons[idx], lats[idx])
    with ThreadPoolExecutor(32) as ex:
        for idx, vals in ex.map(_tile, hit.groupby(hit).groups.items()):
            out[idx] = vals
    return out


def sample_bii_s3(lons: np.ndarray, lats: np.ndarray, year: int = YEAR) -> np.ndarray:
    """BII COGs for ``year`` (2020 by default), tile-grouped via _sample_by_tile."""
    return _sample_by_tile(lons, lats, _tile_index(year))


def _pc_tile_index(year: int, bounds: tuple[float, float, float, float]) -> gpd.GeoDataFrame:
    """{uri, footprint} per Planetary Computer io-biodiversity item covering ``bounds``."""
    import planetary_computer
    import pystac_client

    client = pystac_client.Client.open(PC_STAC, modifier=planetary_computer.sign_inplace)
    items = client.search(collections=[PC_COLLECTION], bbox=bounds,
                           datetime=f"{year}-01-01/{year}-12-31").items()
    return tile_index._to_gdf((it.assets["data"].href, it.geometry) for it in items)


def sample_pc(lons: np.ndarray, lats: np.ndarray, year: int = YEAR) -> np.ndarray:
    """Sample the Planetary Computer io-biodiversity product, tile-grouped like sample_bii_s3."""
    bounds = (lons.min(), lats.min(), lons.max(), lats.max())
    return _sample_by_tile(lons, lats, _pc_tile_index(year, bounds))


DATASETS = ["bii", "pc", *LOCAL, "timeseries"]
STRATA = ["continent", "biome", "global"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strata", nargs="+", choices=STRATA, default=STRATA)
    ap.add_argument("--datasets", nargs="+", choices=DATASETS, default=["bii", "pc", *LOCAL])
    ap.add_argument("--suffix", default="", help="suffix for output filename, e.g. sampled_biome_<suffix>.parquet")
    args = ap.parse_args()

    for inp in (str(DATA / f"sample_{s}.parquet") for s in args.strata):
        gdf = gpd.read_parquet(inp)
        lons, lats = gdf.geometry.x.to_numpy(), gdf.geometry.y.to_numpy()
        new_cols = {}
        if "timeseries" in args.datasets:
            new_cols.update({f"bii_{y}": sample_bii_s3(lons, lats, y) for y in config.years()})
        producers = {
            "bii": lambda: {"bii": new_cols.get(f"bii_{YEAR}", sample_bii_s3(lons, lats))},
            "pc": lambda: {"pc": sample_pc(lons, lats)},
            **{n: (lambda p=p, n=n: {n: _sample(str(p), lons, lats)}) for n, p in LOCAL.items()},
        }
        for name in (n for n in args.datasets if n != "timeseries"):
            new_cols.update(producers[name]())
        gdf = gdf.assign(**new_cols)

        suffix = f"_{args.suffix}" if args.suffix else ""
        out = Path(inp).with_name(Path(inp).stem.replace("sample_", "sampled_") + suffix + ".parquet")
        gdf.to_parquet(out)
        print(f"{out}: {len(gdf)} rows, {gdf[list(new_cols)].notna().sum().to_dict()} non-null")


if __name__ == "__main__":
    main()
