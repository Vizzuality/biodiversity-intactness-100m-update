"""Stage Impact Observatory annual LULC (landcover) -> footprint index only (no re-COG).

Unlike every other staged asset, IO LULC is **never copied**: it is already cloud-optimized
and AWS-hosted, so the model reads the original STAC item hrefs in place at processing time
(the project's "avoid moving data" principle). This module moves no pixels — it walks the IO
STAC once at staging time and writes the ``{geometry, uri}`` GeoParquet index (one per year)
that :func:`bii.tile_index.lookup` queries via ``.sindex``.

This replaces the old per-chunk live STAC search (``tile_index._lookup_lulc``): the global
walk happens once during staging instead of on every chunk read.

One unit per year -> one Batch array index. Each year writes a distinct per-year index file
(:func:`bii.tile_index.index_uri`), so parallel jobs never race on a shared file and the
``register``/``consolidate`` parts machinery is unnecessary.
"""

from __future__ import annotations

from shapely.geometry import box, shape

from .. import config, tile_index

ASSET = "landcover"
# Impact Observatory 10 m annual LULC, AWS-hosted, covers 2017-2024. Read in place; the index
# stores the original item hrefs so cog_worker mosaics them directly at processing time.
STAC_URL = "https://api.impactobservatory.com/stac-aws"
COLLECTION = "io-10m-annual-lulc"


def list_units(years: list[int] | None = None) -> list[dict]:
    return [{"id": str(y), "year": y} for y in (years or config.years())]


def _item_footprints(year: int) -> list[tuple[str, object]]:
    """Walk the IO STAC for ``year``; return ``(href, geometry)`` for each raster asset."""
    import pystac_client

    client = pystac_client.Client.open(STAC_URL)
    search = client.search(
        collections=[COLLECTION],
        datetime=f"{year}-01-01/{year}-12-31",
        limit=500,
    )
    footprints: list[tuple[str, object]] = []
    for item in search.items():
        geom = shape(item.geometry) if item.geometry else box(*item.bbox)
        for key, asset in item.assets.items():
            mt = (asset.media_type or "").lower()
            if "tif" in mt or key in ("data", "supercell"):
                footprints.append((asset.href, geom))
    return footprints


def stage_unit(
    unit: dict | None = None,
    *,
    year: int | None = None,
    overwrite: bool = False,
    register_index: bool = True,
    **_,
) -> dict | None:
    """Build the landcover footprint index for one year. ``register_index=False`` is a no-op
    skip (this module's only product *is* the index). Skip-if-exists unless ``overwrite``."""
    year = (unit or {}).get("year", year) if unit else year
    if year is None:
        raise ValueError("iolulc.stage_unit requires a year (via unit['year'] or year=)")

    uri = tile_index.index_uri(ASSET, year)
    if not register_index:
        return None
    if not overwrite and tile_index.cog.exists(uri):
        return {"asset": ASSET, "uri": uri, "year": year, "index_part": None, "skipped": True}

    footprints = _item_footprints(year)
    tile_index.build_index(ASSET, footprints, year=year)
    return {
        "asset": ASSET,
        "uri": uri,
        "year": year,
        "index_part": None,
        "n_items": len(footprints),
    }
