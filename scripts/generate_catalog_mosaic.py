#!/usr/bin/env python
"""Enumerate the out folder and build per-year MosaicJSONs + a STAC GeoParquet.

Host-side post-processing for a finished run; needs the ``catalog`` extra:

    uv run --extra catalog python scripts/generate_catalog_mosaic.py --run-id v1_1
    uv run --extra catalog python scripts/generate_catalog_mosaic.py --year 2020 --year 2021

Each COG header is read once (``get_dataset_info``) and feeds both the mosaics and the STAC items.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pystac
from cogeo_mosaic.mosaic import MosaicJSON
from cogeo_mosaic.utils import get_dataset_info
from stac_geoparquet.arrow import parse_stac_items_to_arrow, to_parquet

from bii import config, io
from bii import cog


def cogs_by_year(run_id: str) -> dict[int, list[str]]:
    """``{year: [COG uri, ...]}`` from ``out/<run_id>/bii_<year>/bii_<year>_*.tif``."""
    prefix = config.out_uri(run_id) + "/"
    by_year: dict[int, list[str]] = {}
    for u in io.list_uris(prefix):
        if not u.endswith(".tif"):
            continue
        layer = u[len(prefix):].split("/")[0]  # bii_<year>
        by_year.setdefault(int(layer.rsplit("_", 1)[-1]), []).append(u)
    return by_year


def read_footprints(uris: list[str]) -> list[dict]:
    """One header read per COG, as cogeo-mosaic GeoJSON features (geometry + bounds + zooms)."""
    with ThreadPoolExecutor(max_workers=32) as ex:
        return list(ex.map(get_dataset_info, uris))


def build_mosaic(run_id: str, year: int, features: list[dict]) -> str:
    """Write a MosaicJSON over one year's footprints next to its COGs; return its uri."""
    minzoom = max(f["properties"]["minzoom"] for f in features)
    maxzoom = max(f["properties"]["maxzoom"] for f in features)
    mosaic = MosaicJSON.from_features(features, minzoom, maxzoom)
    uri = config.out_uri(run_id, f"bii_{year}", f"bii_{year}_mosaic.json")
    io.put_bytes(mosaic.model_dump_json(exclude_none=True).encode(), uri)
    return uri


def stac_item(feature: dict, year: int, run_id: str) -> pystac.Item:
    uri = feature["properties"]["path"]
    item = pystac.Item(
        id=uri.rsplit("/", 1)[-1][:-4],
        geometry=feature["geometry"],
        bbox=list(feature["properties"]["bounds"]),
        datetime=datetime(year, 1, 1, tzinfo=timezone.utc),
        properties={},
        collection=run_id,
    )
    item.add_asset("data", pystac.Asset(href=uri, media_type=pystac.MediaType.COG, roles=["data"]))
    return item


def build_catalog(run_id: str, items: list[pystac.Item]) -> str:
    """Write one STAC GeoParquet over every COG in the out folder; return its uri."""
    uri = config.out_uri(run_id, "catalog.parquet")
    with io.staged_local_path(uri) as path:
        to_parquet(parse_stac_items_to_arrow(items), path)
    return uri


def main(argv=None) -> dict:
    parser = argparse.ArgumentParser(description="Build per-year MosaicJSONs + a STAC GeoParquet over the out folder.")
    parser.add_argument("--run-id", default=None, help="output prefix (default: BII_RUN_ID / config.RUN_ID)")
    parser.add_argument("--year", type=int, action="append", help="limit to this year (repeatable; default: all)")
    args = parser.parse_args(argv)

    # INGESTED_BYTES_AT_OPEN grabs each COG header in one GET.
    os.environ.update(cog.GDAL_READ_ENV, GDAL_INGESTED_BYTES_AT_OPEN="32768")

    run_id = args.run_id or config.RUN_ID
    by_year = cogs_by_year(run_id)
    if args.year:
        by_year = {y: by_year[y] for y in args.year if y in by_year}
    if not by_year:
        raise SystemExit(f"no COGs under {config.out_uri(run_id)}")

    items: list[pystac.Item] = []
    mosaics: dict[int, str] = {}
    for year, uris in sorted(by_year.items()):
        features = read_footprints(uris)
        items += [stac_item(f, year, run_id) for f in features]
        mosaics[year] = build_mosaic(run_id, year, features)
    catalog = build_catalog(run_id, items)
    result = {"run_id": run_id, "years": sorted(mosaics),
              "n_cogs": len(items), "mosaics": mosaics, "catalog": catalog}
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    main(sys.argv[1:])
