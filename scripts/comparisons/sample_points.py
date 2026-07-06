"""Stratified random sample points by continent and by biome, for comparing BII 2020 to the
Global Human Modification Index and the NHM BII.

Biomes come from RESOLVE Ecoregions 2017; continents from Natural Earth admin-0 countries.
Points are drawn uniformly by ground area (sampling in an equal-area CRS) within each stratum's
land polygons, then written as EPSG:4326 GeoParquet.
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path
from urllib.request import urlretrieve

import geopandas as gpd
import numpy as np

DATA = Path("data/compare")
EQUAL_AREA = "EPSG:6933"  # WGS84 World Equal Area

RESOLVE_URL = "https://storage.googleapis.com/teow2016/Ecoregions2017.zip"
NE_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_0_countries.geojson"


def _download(url: str, dst: Path) -> Path:
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading {url}")
        urlretrieve(url, dst)
    return dst


def load_biomes() -> gpd.GeoDataFrame:
    zpath = _download(RESOLVE_URL, DATA / "Ecoregions2017.zip")
    with zipfile.ZipFile(zpath) as z:
        shp = next(n for n in z.namelist() if n.endswith(".shp"))
    gdf = gpd.read_file(f"zip://{zpath}!{shp}")[["BIOME_NAME", "geometry"]]
    return gdf[~gdf["BIOME_NAME"].isin(["N/A"])].rename(columns={"BIOME_NAME": "biome"})


def load_continents() -> gpd.GeoDataFrame:
    gpath = _download(NE_URL, DATA / "ne_50m_admin_0_countries.geojson")
    gdf = gpd.read_file(gpath)[["CONTINENT", "geometry"]]
    drop = ["Antarctica", "Seven seas (open ocean)"]
    return gdf[~gdf["CONTINENT"].isin(drop)].rename(columns={"CONTINENT": "continent"})


def sample(gdf: gpd.GeoDataFrame, col: str, n: int, seed: int) -> gpd.GeoDataFrame:
    """n uniform-by-area random points per unique value of `col`, returned in EPSG:4326."""
    strata = gdf.dissolve(by=col).to_crs(EQUAL_AREA)
    rng = np.random.default_rng(seed)
    pts = strata.geometry.sample_points(n, rng=rng)  # one MultiPoint per stratum
    out = gpd.GeoDataFrame(geometry=pts, crs=EQUAL_AREA).reset_index().explode(index_parts=False)
    return out.to_crs("EPSG:4326").reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-n", type=int, default=1000, help="points per stratum")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    DATA.mkdir(parents=True, exist_ok=True)
    for name, loader in (("continent", load_continents), ("biome", load_biomes)):
        pts = sample(loader(), name, args.n, args.seed)
        dst = DATA / f"sample_{name}.parquet"
        pts.to_parquet(dst)
        print(f"{dst}: {len(pts)} points across {pts[name].nunique()} {name}s")


if __name__ == "__main__":
    main()
