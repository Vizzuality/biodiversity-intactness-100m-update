#!/usr/bin/env python
"""Stage only the inputs overlapping a small AOI, locally, via the docker executor.

worldpop/sdpt filter by a vendored country bbox table (``scripts/data/country_bounds.json``).

    python scripts/test_stage_local.py --bounds -86 9 -84 11 --year 2020 --out ./data/staged_local
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from shapely.geometry import box

from bii import config, stage
from bii.staging import roads, sdpt

COUNTRY_BOUNDS = os.path.join(os.path.dirname(__file__), "data", "country_bounds.json")


def _intersects(a, b) -> bool:
    """``a`` is a box or list of boxes (antimeridian-crossing countries split at the dateline)."""
    boxes = a if isinstance(a[0], (list, tuple)) else [a]
    return any(not (x[2] < b[0] or x[0] > b[2] or x[3] < b[1] or x[1] > b[3]) for x in boxes)


def _hansen_bounds(unit: dict) -> tuple[float, float, float, float]:
    """EPSG:4326 extent of a Hansen 10deg tile from its NW-corner ``lat``/``lon`` labels."""
    lat, lon = unit["lat"], unit["lon"]
    north = int(lat[:2]) * (1 if lat[2] == "N" else -1)
    west = int(lon[:3]) * (1 if lon[3] == "E" else -1)
    return (west, north - 10, west + 10, north)


def _roads_ids(aoi) -> set:
    """Region ids by precise Geofabrik geometry, not bbox (some regions' bbox spans the globe)."""
    gdf = roads._manifest()
    return set(gdf[gdf.intersects(box(*aoi))]["id"])


def _aoi_items(datasets, year, aoi, cbounds) -> list[dict]:
    """worldpop/sdpt units are generated for the AOI's countries (not filtered from the module's
    partial default list) so any country the source covers is reachable."""
    aoi_isos = {c for c, b in cbounds.items() if _intersects(b, aoi)}
    items: list[dict] = []
    for name in datasets:
        mod = stage.MODULES[name]
        if name == "hansen":
            units = [u for u in mod.list_units() if _intersects(_hansen_bounds(u), aoi)]
        elif name == "roads":
            road_ids = _roads_ids(aoi)
            units = [u for u in mod.list_units() if u["id"] in road_ids]
        elif name == "worldpop":
            units = mod.list_units(countries=sorted(aoi_isos), years=[year])
        elif name == "sdpt":  # Europe folds into "eu"
            regs = sdpt.regions_for(aoi_isos)
            units = mod.list_units(regions=sorted(regs)) if regs else []
        elif name in ("nightlights", "iolulc"):
            units = mod.list_units(years=[year])
        else:
            units = mod.list_units()
        items += [stage.manifest_item(name, u) for u in units]
    return items


def main(argv=None) -> dict:
    p = argparse.ArgumentParser(description="Stage AOI-subset BII inputs locally via docker.")
    p.add_argument("--bounds", type=float, nargs=4, metavar=("W", "S", "E", "N"),
                   default=[-5.0, 39.0, -1.32, 42.68],
                   help="AOI extent in EPSG:4326 (default: ~4096px central Spain)")
    p.add_argument("--year", type=int, default=2020, help="year to stage (per-year datasets)")
    p.add_argument("--out", default="./data/staged_local", help="local staged-root output dir (bind-mounted into the container)")
    p.add_argument("--dataset", choices=sorted(stage.MODULES), help="stage only this dataset (default: all)")
    p.add_argument("--overwrite", action="store_true", help="restage units whose output already exists")
    p.add_argument("--dry-run", action="store_true", help="list the AOI-selected units and exit")
    args = p.parse_args(argv)

    staged = os.path.abspath(args.out)
    config.STAGED_ROOT = config.OUT_ROOT = staged
    config.START_YEAR = config.END_YEAR = args.year

    # Pad by the processing buffer so edge focal/distance ops have source data.
    pad = config.BUFFER * config.SCALE_DEG
    w, s, e, n = args.bounds
    aoi = (w - pad, s - pad, e + pad, n + pad)

    cbounds = json.load(open(COUNTRY_BOUNDS))
    datasets = [args.dataset] if args.dataset else list(stage.MODULES)

    items = _aoi_items(datasets, args.year, aoi, cbounds)

    if args.dry_run:
        pending = items if args.overwrite else stage._pending(items)
        result = {"staged_root": staged, "planned": len(items), "pending": len(pending),
                  "units": [{"dataset": it["dataset"], "id": it["unit"]["id"]} for it in items]}
        print(json.dumps(result))
        return result

    result = {"staged_root": staged,
              **stage.run(items=items, executor="docker", overwrite=args.overwrite, store=staged)}
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    main(sys.argv[1:])
