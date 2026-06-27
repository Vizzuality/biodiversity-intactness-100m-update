"""Stage WorldPop R2025A per-country 100 m population -> COG per (country, year).

Counts are continuous, so overviews use ``average``.
"""

from __future__ import annotations

from .. import config
from .. import cog

ASSET = "population"

# R2025A 100 m is *constrained* (the only 100 m product); the original version used the older
# unconstrained product.
BASE = "https://data.worldpop.org/GIS/Population/Global_2015_2030/R2025A"
# R2025A countries minus confirmed with no data (uninhabited/special territories);
# Regenerate from ``{BASE}/<year>/`` 3-letter dir names.
COUNTRIES = [
    "ABW", "AFG", "AGO", "AIA", "ALA", "ALB", "AND", "ARE", "ARG", "ARM",
    "ASM", "ATG", "AUS", "AUT", "AZE", "BDI", "BEL", "BEN", "BES",
    "BFA", "BGD", "BGR", "BHR", "BHS", "BIH", "BLM", "BLR", "BLZ", "BMU",
    "BOL", "BRA", "BRB", "BRN", "BTN", "BWA", "CAF", "CAN", "CCK",
    "CHE", "CHL", "CHN", "CIV", "CMR", "COD", "COG", "COK", "COL", "COM",
    "CPV", "CRI", "CUB", "CUW", "CXR", "CYM", "CYP", "CZE", "DEU",
    "DJI", "DMA", "DNK", "DOM", "DZA", "ECU", "EGY", "ERI", "ESH", "ESP",
    "EST", "ETH", "FIN", "FJI", "FLK", "FRA", "FRO", "FSM", "GAB", "GBR",
    "GEO", "GGY", "GHA", "GIB", "GIN", "GLP", "GMB", "GNB", "GNQ", "GRC",
    "GRD", "GRL", "GTM", "GUF", "GUM", "GUY", "HKG", "HND", "HRV",
    "HTI", "HUN", "IDN", "IMN", "IND", "IRL", "IRN", "IRQ", "ISL",
    "ISR", "ITA", "JAM", "JEY", "JOR", "JPN", "KAZ", "KEN", "KGZ", "KHM",
    "KIR", "KNA", "KOR", "KWT", "LAO", "LBN", "LBR", "LBY", "LCA", "LIE",
    "LKA", "LSO", "LTU", "LUX", "LVA", "MAC", "MAF", "MAR", "MCO", "MDA",
    "MDG", "MDV", "MEX", "MHL", "MKD", "MLI", "MLT", "MMR", "MNE", "MNG",
    "MNP", "MOZ", "MRT", "MSR", "MTQ", "MUS", "MWI", "MYS", "MYT", "NAM",
    "NCL", "NER", "NFK", "NGA", "NIC", "NIU", "NLD", "NOR", "NPL", "NRU",
    "NZL", "OMN", "PAK", "PAN", "PCN", "PER", "PHL", "PLW", "PNG", "POL",
    "PRI", "PRK", "PRT", "PRY", "PSE", "PYF", "QAT", "REU", "ROU", "RUS",
    "RWA", "SAU", "SDN", "SEN", "SGP", "SHN", "SJM", "SLB", "SLE",
    "SLV", "SMR", "SOM", "SPM", "SRB", "SSD", "STP", "SUR", "SVK", "SVN",
    "SWE", "SWZ", "SXM", "SYC", "SYR", "TCA", "TCD", "TGO", "THA", "TJK",
    "TKL", "TKM", "TLS", "TON", "TTO", "TUN", "TUR", "TUV", "TWN", "TZA",
    "UGA", "UKR", "URY", "USA", "UZB", "VCT", "VEN", "VGB",
    "VIR", "VNM", "VUT", "WLF", "WSM", "XKX",
    "YEM", "ZAF", "ZMB", "ZWE",
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


def stage_unit(unit: dict) -> bool:
    dst = _dst(unit["iso3"], unit["year"])
    cog.translate_to_cog(unit["url"], dst, resampling="average")
    return True
