"""Stage annual LULC (landcover) -> footprint index only (no re-COG).

IO LULC is never copied: it is already cloud-optimized and hosted by Esri's Living Atlas mirror
(the IO STAC/S3 source lags Esri by a year, e.g. no 2025 as of writing), so the model reads Esri's
tiles in place. This walks the IO STAC once per year for the stable supercell tile grid, builds
each tile's Esri href, and writes a ``{geometry, uri}`` GeoParquet index per year. No COGs, so the
orchestrator's rebuild-from-COGs step skips it (listed in ``stage.INDEX_IN_PLACE``).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import requests

from .. import config, tile_index
from .. import cog

ASSET = "landcover"
STAC_URL = "https://api.impactobservatory.com/stac-aws"
COLLECTION = "io-10m-annual-lulc"
# Newest year with full (757-tile) STAC coverage; used only to enumerate tile codes, since the
# supercell grid is stable across years -- bump if the grid ever changes.
TILE_LIST_YEAR = 2024

ESRI_BASE = "https://lulctimeseries.blob.core.windows.net/lulctimeseriesv003"


def list_units(years: list[int] | None = None) -> list[dict]:
    # ``dst`` is the per-year index parquet itself so skip-if-exists works like COG modules.
    return [{"id": str(y), "year": y, "dst": tile_index.index_uri(ASSET, y)}
            for y in (years or config.years())]


def _tile_codes() -> list[str]:
    """Supercell tile codes (e.g. ``"60W"``) from the IO STAC's ``TILE_LIST_YEAR`` listing."""
    import pystac_client

    client = pystac_client.Client.open(STAC_URL)
    search = client.search(
        collections=[COLLECTION],
        datetime=f"{TILE_LIST_YEAR}-01-01/{TILE_LIST_YEAR}-12-31",
        limit=500,
    )
    return [item.id.rsplit("-", 1)[0] for item in search.items()]


def _esri_url(tile: str, year: int) -> str:
    """Esri Living Atlas href for ``tile``/``year``. Naming changed at 2024: year-to-year-end
    range before, same-year Jan-Dec range from 2024 on."""
    end = f"{year}1231" if year >= 2024 else f"{year + 1}0101"
    return f"{ESRI_BASE}/lc{year}/{tile}_{year}0101-{end}.tif"


def _footprint_or_none(uri: str) -> tuple[str, tuple[float, float, float, float]] | None:
    """``(uri, footprint)``, or ``None`` if the tile wasn't published for this year. Only a
    confirmed 404 is treated as absent; any other failure (throttling, outage, ...) propagates."""
    try:
        requests.head(uri, timeout=30).raise_for_status()
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise
    return uri, cog.footprint(uri, tile_index.INDEX_CRS)


def stage_unit(unit: dict) -> bool:
    """Build (overwriting) the landcover footprint index for one year."""
    urls = [_esri_url(tile, unit["year"]) for tile in _tile_codes()]
    with ThreadPoolExecutor(max_workers=16) as ex:
        footprints = [r for r in ex.map(_footprint_or_none, urls) if r is not None]
    tile_index.build_index(ASSET, footprints, year=unit["year"])
    return True
