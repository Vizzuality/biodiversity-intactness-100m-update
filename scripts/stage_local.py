#!/usr/bin/env python
"""Stage only the inputs overlapping a small AOI, locally, via the Batch-mirroring docker executor.

For a one-region / one-year BII run, staging the whole world is wasteful. This subsets each dataset
to an AOI before staging. The AOI overlap logic lives here so the dataset modules stay untouched:

* hansen (10deg tiles) + roads (Geofabrik regions) filter by their own unit bounds.
* worldpop (ISO3) + sdpt (region code) filter by a vendored country bbox table
  (``scripts/data/country_bounds.json``); pass ``--countries`` / ``--regions`` to override.
* iolulc (index-only) + the global single-file datasets (nightlights, accessibility, fml) are
  staged whole — ``tile_index.lookup`` windows them to the chunk at processing time.

Staging runs through ``bii.stage``'s docker executor — the same manifest + array-index + env
contract as AWS Batch, with a bind-mounted local dir standing in for the S3 store:

    python scripts/stage_local.py --bounds -86 9 -84 11 --year 2020 --staged ./data/staged_local
    python scripts/stage_local.py --bounds -86 9 -84 11 --year 2020 --build   # build image first
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from shapely.geometry import box

from bii import config, stage
from bii.staging import roads, sdpt, worldpop

COUNTRY_BOUNDS = os.path.join(os.path.dirname(__file__), "data", "country_bounds.json")


def _intersects(a, b) -> bool:
    """Whether ``a`` overlaps the ``(west, south, east, north)`` box ``b``. ``a`` is one such box,
    or a list of boxes (antimeridian-crossing countries are split at the dateline)."""
    boxes = a if isinstance(a[0], (list, tuple)) else [a]
    return any(not (x[2] < b[0] or x[0] > b[2] or x[3] < b[1] or x[1] > b[3]) for x in boxes)


def _hansen_bounds(unit: dict) -> tuple[float, float, float, float]:
    """The EPSG:4326 extent of a Hansen 10deg tile from its NW-corner ``lat``/``lon`` labels."""
    lat, lon = unit["lat"], unit["lon"]
    north = int(lat[:2]) * (1 if lat[2] == "N" else -1)
    west = int(lon[:3]) * (1 if lon[3] == "E" else -1)
    return (west, north - 10, west + 10, north)


def _roads_ids(aoi) -> set:
    """Region ids whose actual Geofabrik geometry intersects the AOI (precise, not the bbox — some
    regions cross the antimeridian, so their bounding box spans the globe)."""
    gdf = roads._manifest()
    return set(gdf[gdf.intersects(box(*aoi))]["id"])


def _wrap(name: str, units: list[dict]) -> list[dict]:
    """Wrap a module's units as orchestrator manifest items ``{dataset, unit, asset, year}``."""
    mod = stage.MODULES[name]
    return [{"dataset": name, "unit": u, "asset": mod.ASSET, "year": u.get("year")} for u in units]


def _aoi_items(datasets, year, aoi, cbounds, countries, regions, road_ids) -> list[dict]:
    """The AOI-scoped manifest items per dataset (dataset modules untouched). worldpop/sdpt units
    are *generated* for the AOI's countries via ``list_units(countries=/regions=)`` — not filtered
    from the module's partial default list — so any country the source covers is reachable."""
    aoi_isos = {c for c, b in cbounds.items() if _intersects(b, aoi)}
    items: list[dict] = []
    for name in datasets:
        mod = stage.MODULES[name]
        if name == "hansen":
            units = [u for u in mod.list_units() if _intersects(_hansen_bounds(u), aoi)]
        elif name == "roads":
            units = [u for u in mod.list_units() if u["id"] in road_ids]
        elif name == "worldpop":
            units = mod.list_units(countries=sorted(countries or aoi_isos), years=[year])
        elif name == "sdpt":  # map AOI ISO3s to sdpt regions (Europe folds into "eu")
            regs = {r.lower() for r in regions} if regions else sdpt.regions_for(aoi_isos)
            units = mod.list_units(regions=sorted(regs)) if regs else []
        elif name in ("nightlights", "iolulc"):
            units = mod.list_units(years=[year])
        else:  # travel_time, fml — single global file
            units = mod.list_units()
        items += _wrap(name, units)
    return items


def main(argv=None) -> dict:
    p = argparse.ArgumentParser(description="Stage AOI-subset BII inputs locally via docker.")
    p.add_argument("--bounds", type=float, nargs=4, metavar=("W", "S", "E", "N"),
                   default=[-5.0, 39.0, -1.32, 42.68],
                   help="AOI extent in EPSG:4326 (default: ~4096px central Spain)")
    p.add_argument("--year", type=int, default=2020, help="year to stage (per-year datasets)")
    p.add_argument("--staged", default="./data/staged_local", help="local staged root (bind-mounted into the container)")
    p.add_argument("--countries", nargs="*", help="ISO3 override for worldpop (default: from bbox table)")
    p.add_argument("--regions", nargs="*", help="region override for sdpt (default: from bbox table)")
    p.add_argument("--dataset", choices=sorted(stage.MODULES), help="stage only this dataset (default: all)")
    p.add_argument("--build", action="store_true", help="docker build the bii image first")
    p.add_argument("--overwrite", action="store_true", help="restage units whose output already exists")
    p.add_argument("--dry-run", action="store_true", help="list the AOI-selected units and exit")
    args = p.parse_args(argv)

    staged = os.path.abspath(args.staged)
    config.STAGED_ROOT = config.OUT_ROOT = staged
    config.START_YEAR = config.END_YEAR = args.year

    # Pad the AOI by the processing buffer so edge focal/distance ops have source data.
    pad = config.BUFFER * config.SCALE_DEG
    w, s, e, n = args.bounds
    aoi = (w - pad, s - pad, e + pad, n + pad)

    cbounds = json.load(open(COUNTRY_BOUNDS))
    countries = {c.upper() for c in args.countries} if args.countries else None
    regions = {r.upper() for r in args.regions} if args.regions else None
    road_ids = _roads_ids(aoi)
    datasets = [args.dataset] if args.dataset else list(stage.MODULES)

    items = _aoi_items(datasets, args.year, aoi, cbounds, countries, regions, road_ids)
    pending = items if args.overwrite else stage._pending(items)

    if args.dry_run:
        result = {"staged_root": staged, "planned": len(items), "pending": len(pending),
                  "units": [{"dataset": it["dataset"], "id": it["unit"]["id"]} for it in items]}
        print(json.dumps(result))
        return result

    if args.build:
        subprocess.run(["docker", "build", "-t", "bii", "-f", "Dockerfile", "."], check=True)

    stage.print_summary(items, pending)
    failed = stage._run_docker(pending, store=staged) if pending else []
    # Skip rebuilding any asset with a failed unit (incomplete footprint -> dropped land chunks).
    incomplete = {(f["asset"], f["year"]) for f in failed}
    assets = {(it["asset"], it["year"]) for it in pending if it["dataset"] not in stage.INDEX_IN_PLACE}
    indexes = stage._consolidate(assets - incomplete)

    result = {"staged_root": staged, "planned": len(items), "pending": len(pending),
              "indexes": indexes, "failed": failed,
              "incomplete_indexes": sorted(incomplete & assets, key=lambda a: (a[0], a[1] or 0))}
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    main(sys.argv[1:])
