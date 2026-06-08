"""Stage VIIRS Nighttime Lights (VNL) annual median composites -> COG per year.

The source is a gzipped global GeoTIFF (``.tif.gz``) streamed in place via
``/vsigzip//vsicurl/...`` — no decompress-to-disk. Two wrinkles handled here:

* The filename embeds a per-release timestamp (``c<TIMESTAMP>``) that isn't predictable, so
  the exact URL must be resolved. Provide it explicitly via :data:`URLS`, or let
  :func:`_resolve_url` scrape the EOG annual directory.
* EOG now requires a free account; pass a bearer token in the ``BII_EOG_TOKEN`` env var (a
  credential, not analysis config) for both the directory scrape and the GDAL read.
"""

from __future__ import annotations

import os
import re

import requests

from .. import config
from . import _base, cog

ASSET = "nightlights"

# VIIRS Nighttime Lights (VNL) v2.1/v2.2 annual median composites, .tif.gz.
BASE = "https://eogdata.mines.edu/nighttime_light/annual"

# Optional explicit {year: url} overrides (skips directory scraping).
URLS: dict[int, str] = {}


def _version(year: int) -> str:
    return "v22" if year >= 2022 else "v21"


def _resolve_url(year: int) -> str:
    if year in URLS:
        return URLS[year]

    base = f"{BASE}/{_version(year)}/{year}/"
    headers = {}
    token = os.environ.get("BII_EOG_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.get(base, headers=headers, timeout=60)
    resp.raise_for_status()
    # Prefer the median masked composite (used in the whitepaper).
    matches = re.findall(r'href="([^"]*median_masked\.tif\.gz)"', resp.text)
    if not matches:
        matches = re.findall(r'href="([^"]*median\.tif\.gz)"', resp.text)
    if not matches:
        raise FileNotFoundError(f"could not resolve VNL median URL for {year} at {base}")
    href = matches[0]
    return href if href.startswith("http") else base + href


def _dst(year: int) -> str:
    return config.staged_uri(ASSET, f"{ASSET}_{year}.tif")


def list_units(years: list[int] | None = None) -> list[dict]:
    years = years or config.years()
    return [{"id": str(y), "year": y} for y in years]


def stage_unit(
    unit: dict,
    *,
    overwrite: bool = False,
    register_index: bool = True,
    **_,
) -> dict | None:
    year = unit["year"]
    url = unit.get("url") or _resolve_url(year)
    src = f"/vsigzip//vsicurl/{url}"
    dst = _dst(year)
    extra_env = {}
    token = os.environ.get("BII_EOG_TOKEN")
    if token:
        extra_env["GDAL_HTTP_HEADERS"] = f"Authorization: Bearer {token}"
    footprint = cog.translate_to_cog(
        src, dst, resampling="average", overwrite=overwrite, extra_env=extra_env
    )
    return _base.finalize(ASSET, dst, footprint, year, register_index)
