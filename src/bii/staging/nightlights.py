"""Stage VIIRS Nighttime Lights (VNL) annual median composites -> COG per year.

The source is a gzipped global GeoTIFF (``.tif.gz``) downloaded then read via ``/vsigzip``.
Two wrinkles handled here:

* The filename embeds a per-release timestamp (``c<TIMESTAMP>``) that isn't predictable, so
  the exact URL must be resolved. Provide it explicitly via :data:`URLS`, or let
  :func:`_resolve_url` scrape the EOG annual directory.
* EOG only allows browser (session) access, not bearer tokens. Log in at eogdata.mines.edu,
  copy the ``mod_auth_openidc_session`` cookie value, and pass it in the ``BII_EOG_COOKIE`` env
  var for both the directory scrape and the source download.
"""

from __future__ import annotations

import os
import re

import requests

from .. import config, tile_index
from . import cog

ASSET = "nightlights"

# VIIRS Nighttime Lights (VNL) v2.1/v2.2 annual median composites, .tif.gz.
BASE = "https://eogdata.mines.edu/nighttime_light/annual"

# Optional explicit {year: url} overrides (skips directory scraping).
URLS: dict[int, str] = {}


def _version(year: int) -> str:
    return "v22" if year >= 2022 else "v21"


def _headers() -> dict:
    cookie = os.environ.get("BII_EOG_COOKIE")
    return {"Cookie": f"mod_auth_openidc_session={cookie}"} if cookie else {}


def _resolve_url(year: int) -> str:
    if year in URLS:
        return URLS[year]

    base = f"{BASE}/{_version(year)}/{year}/"
    resp = requests.get(base, headers=_headers(), timeout=60)
    resp.raise_for_status()
    # Prefer the median masked composite (used in the whitepaper).
    matches = re.findall(r'href="([^"]*median_masked(?:\.dat)?\.tif\.gz)"', resp.text)
    if not matches:
        matches = re.findall(r'href="([^"]*median(?:\.dat)?\.tif\.gz)"', resp.text)
    if not matches:
        raise FileNotFoundError(f"could not resolve VNL median URL for {year} at {base}")
    href = matches[0]
    return href if href.startswith("http") else base + href


def _dst(year: int) -> str:
    return config.staged_uri(ASSET, f"{ASSET}_{year}.tif")


def list_units(years: list[int] | None = None) -> list[dict]:
    years = years or config.years()
    return [{"id": str(y), "year": y, "dst": _dst(y)} for y in years]


def stage_unit(
    unit: dict,
    register_index: bool = True,
) -> dict | None:
    year = unit["year"]
    url = unit.get("url") or _resolve_url(year)
    dst = _dst(year)
    # EOG only allows browser (session) access; pass the openidc cookie as a download header. The
    # source is a .tif.gz, which translate_to_cog reads through /vsigzip after fetching it to disk.
    footprint = cog.translate_to_cog(url, dst, resampling="average", headers=_headers() or None)
    return tile_index.finalize(ASSET, dst, footprint, year, register_index)
