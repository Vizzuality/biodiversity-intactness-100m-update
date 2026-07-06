#!/usr/bin/env python
"""Sample each 2020 comparison dataset at the stratified points and write values to parquet.

Datasets: our BII (tiled COGs in S3), 2020 gHM, NHM BII v2.1.1, and the expert-elicited Africa
BII. Points come from sample_points.py. The S3 BII is sampled by first assigning each point to
its tile, then opening each COG once so only the touched blocks are fetched (one GET per block),
instead of an open-per-point.

    uv run --extra dev python scripts/sample_values.py
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio as rio
from rasterio.warp import transform

from bii import config
from bii.cog import GDAL_READ_ENV

DATA = Path("data/compare")
LOCAL = {
    "ghm": DATA / "ghm_2020_AA_300m.tif",
    "nhm_bii": DATA / "bii-v2-1-1-nhm-data-portal" / "bii-2020_v2-1-1.tif",
    "bii_africa": DATA / "bii_africa_1km.tif",
}


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


def _tile_index(run_id: str, year: int) -> gpd.GeoDataFrame:
    """{uri, footprint} per BII tile for ``year``, from the run's STAC catalog GeoParquet."""
    g = gpd.read_parquet(config.out_uri(run_id, "catalog.parquet"))
    g = g[g["datetime"].dt.year == year].copy()
    g["uri"] = g["assets"].apply(lambda a: a["data"]["href"])
    return g[["uri", "geometry"]]


def sample_bii_s3(lons: np.ndarray, lats: np.ndarray, run_id: str, year: int) -> np.ndarray:
    """Sample tiled BII COGs by grouping points per tile: one open + block reads per tile."""
    pts = gpd.GeoDataFrame(geometry=gpd.points_from_xy(lons, lats), crs="EPSG:4326")
    hit = gpd.sjoin(pts, _tile_index(run_id, year), how="inner", predicate="within")
    hit = hit.groupby(level=0)["uri"].first()

    out = np.full(len(lons), np.nan)

    def _tile(item):
        uri, idx = item
        idx = np.asarray(idx)
        return idx, _sample(uri, lons[idx], lats[idx])
    with ThreadPoolExecutor(32) as ex:
        for idx, vals in ex.map(_tile, hit.groupby(hit).groups.items()):
            out[idx] = vals
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("inputs", nargs="*",
                    default=[str(DATA / "sample_continent.parquet"), str(DATA / "sample_biome.parquet")])
    ap.add_argument("--run-id", default=config.RUN_ID)
    ap.add_argument("--year", type=int, default=2020)
    args = ap.parse_args()

    for inp in args.inputs:
        gdf = gpd.read_parquet(inp)
        lons, lats = gdf.geometry.x.to_numpy(), gdf.geometry.y.to_numpy()
        gdf["bii"] = sample_bii_s3(lons, lats, args.run_id, args.year)
        for name, path in LOCAL.items():
            gdf[name] = _sample(str(path), lons, lats)
        out = Path(inp).with_name(Path(inp).stem.replace("sample_", "sampled_") + ".parquet")
        gdf.to_parquet(out)
        print(f"{out}: {len(gdf)} rows, "
              f"{gdf[['bii', *LOCAL]].notna().sum().to_dict()} non-null")


if __name__ == "__main__":
    main()
