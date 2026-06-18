"""Stage Impact Observatory annual LULC (landcover) -> footprint index only (no re-COG).

Unlike every other staged asset, IO LULC is **never copied**: it is already cloud-optimized
and AWS-hosted, so the model reads the original STAC item hrefs in place at processing time
(the project's "avoid moving data" principle). This module moves no pixels — it walks the IO
STAC once at staging time and writes the ``{geometry, uri}`` GeoParquet index (one per year)
that :func:`bii.tile_index.lookup` queries via ``.sindex``.

This replaces the old per-chunk live STAC search (``tile_index._lookup_lulc``): the global
walk happens once during staging instead of on every chunk read.

One unit per year -> one Batch array index. Each year writes a distinct per-year index file
(:func:`bii.tile_index.index_uri`) directly — this asset has no COGs, so the orchestrator's
rebuild-from-COGs step skips it (it is listed in ``stage.INDEX_IN_PLACE``).
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
    # ``dst`` is the per-year index parquet itself (this asset's only product — no COG), so the
    # orchestrator's skip-if-exists check works uniformly with the COG-producing modules.
    return [{"id": str(y), "year": y, "dst": tile_index.index_uri(ASSET, y)}
            for y in (years or config.years())]


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


def stage_unit(unit: dict | None = None, year: int | None = None) -> dict | None:
    """Build (overwriting) the landcover footprint index for one year."""
    year = (unit or {}).get("year", year) if unit else year
    if year is None:
        raise ValueError("iolulc.stage_unit requires a year (via unit['year'] or year=)")

    footprints = _item_footprints(year)
    uri = tile_index.build_index(ASSET, footprints, year=year)
    return {"asset": ASSET, "uri": uri, "year": year, "n_items": len(footprints)}
