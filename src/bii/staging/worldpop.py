"""Stage WorldPop R2025A per-country 100 m population -> COG per country/year.

A multipart dataset: one COG per (country, year). Population counts are continuous, so
overviews use ``average``; the source nodata is preserved by the translate.
"""

from __future__ import annotations

import requests

from .. import config, tile_index
from . import cog

ASSET = "population"

# WorldPop R2025A, per-country 100 m, annual. NOTE: the R2025A 100 m release is *constrained*
# (the only 100 m product published); the original whitepaper used the older unconstrained
# product. The host does not support HTTP range requests, so WorldPop is fetched to ephemeral
# disk per country and then re-COG'd (not pure-streamed).
BASE = "https://data.worldpop.org/GIS/Population/Global_2015_2030/R2025A"
COUNTRIES = [
    "AFG", "AGO", "ALB", "ARE", "ARG", "AUS", "BGD", "BRA", "CAN", "CHN",
    "COD", "COL", "DEU", "EGY", "ETH", "FRA", "GBR", "GHA", "IDN", "IND",
    "IRN", "IRQ", "ITA", "JPN", "KEN", "MEX", "MMR", "MOZ", "MYS", "NGA",
    "PAK", "PER", "PHL", "POL", "RUS", "SAU", "SDN", "THA", "TUR", "TZA",
    "UGA", "UKR", "USA", "VNM", "ZAF", "ZMB", "ZWE",
]


def _url(iso3: str, year: int) -> str:
    return (
        f"{BASE}/{year}/{iso3}/v1/100m/constrained/"
        f"{iso3.lower()}_pop_{year}_CN_100m_R2025A_v1.tif"
    )


def _dst(iso3: str, year: int) -> str:
    return config.staged_uri(ASSET, str(year), f"{iso3}_{year}.tif")


def list_units(
    countries: list[str] | None = None, years: list[int] | None = None
) -> list[dict]:
    countries = countries or COUNTRIES
    years = years or config.years()
    return [
        {"id": f"{iso3}_{year}", "iso3": iso3, "year": year, "url": _url(iso3, year),
         "dst": _dst(iso3, year)}
        for year in years
        for iso3 in countries
    ]


def stage_unit(
    unit: dict,
    register_index: bool = True,
    missing_ok: bool = False,
) -> dict | None:
    dst = _dst(unit["iso3"], unit["year"])
    # translate_to_cog downloads the GeoTIFF to disk before re-COG'ing (WorldPop's host has no
    # HTTP range support anyway); a missing country 404s.
    try:
        footprint = cog.translate_to_cog(unit["url"], dst, resampling="average")
    except requests.HTTPError:
        if missing_ok:
            return None
        raise
    return tile_index.finalize(ASSET, dst, footprint, unit["year"], register_index)
