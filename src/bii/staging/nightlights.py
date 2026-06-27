"""Stage VIIRS Nighttime Lights (VNL) annual median composites -> COG per year.

Filenames embed an unpredictable per-release timestamp, so each year is listed explicitly.
"""

from __future__ import annotations

from .. import config
from .. import cog

ASSET = "nightlights"

BASE = "https://vizz-bii.s3.amazonaws.com/source/nighttime_light_v2"
URLS = {
    2017: f"{BASE}/VNL_v21_npp_2017_global_vcmslcfg_c202205302300.median_masked.dat.tif.gz",
    2018: f"{BASE}/VNL_v21_npp_2018_global_vcmslcfg_c202205302300.median_masked.dat.tif.gz",
    2019: f"{BASE}/VNL_v21_npp_2019_global_vcmslcfg_c202205302300.median_masked.dat.tif.gz",
    2020: f"{BASE}/VNL_v21_npp_2020_global_vcmslcfg_c202205302300.median_masked.dat.tif.gz",
    2021: f"{BASE}/VNL_v21_npp_2021_global_vcmslcfg_c202205302300.median_masked.dat.tif.gz",
    2022: f"{BASE}/VNL_v22_npp-j01_2022_global_vcmslcfg_c202303062300.median_masked.dat.tif.gz",
    2023: f"{BASE}/VNL_npp_2023_global_vcmslcfg_v2_c202402081600.median_masked.dat.tif.gz",
    2024: f"{BASE}/VNL_npp_2024_global_vcmslcfg_v2_c202502261200.median_masked.dat.tif.gz",
    2025: f"{BASE}/VNL_npp_2025_global_vcmslcfg_v2_c202604011200.median_masked.dat.tif.gz",
}


def _dst(year: int) -> str:
    return config.staged_uri(ASSET, f"{ASSET}_{year}.tif")


def list_units(years: list[int] | None = None) -> list[dict]:
    years = years or config.years()
    return [{"id": str(y), "year": y, "dst": _dst(y)} for y in years]


def stage_unit(unit: dict) -> bool:
    year = unit["year"]
    url = unit.get("url") or URLS[year]
    cog.translate_to_cog(url, _dst(year), resampling="average")
    return True
