"""Staging orchestrator: enumerate every staging unit, stage the ones whose output is missing
(or all, with ``overwrite``), then consolidate each asset's footprint index from its parts.

A thin driver over :mod:`bii.staging`: each dataset module exposes ``list_units`` / ``stage_unit``
and each unit carries its destination ``dst``, so this module stays dataset-agnostic — it lists,
skips by ``s3io.exists(dst)``, stages, and merges the per-unit index parts via
:func:`bii.tile_index.consolidate`. Because ``stage_unit`` always overwrites (the existence check
is the orchestrator's job), the skip/overwrite decision lives here.

Restrict to one dataset (``--dataset``) or one year (``--year``, per-year datasets only). An asset
is consolidated only when a unit registered a part this run; landcover builds its index in place
and registers none, so it is consolidated-exempt automatically.
"""

from __future__ import annotations

import argparse
import inspect
import json

from . import s3io, tile_index
from .staging import MODULES


def _list_units(module, year: int | None) -> list[dict]:
    """``module.list_units`` filtered to ``year`` when the module is per-year, else its full list."""
    if year is not None and "years" in inspect.signature(module.list_units).parameters:
        return module.list_units(years=[year])
    return module.list_units()


def plan(dataset: str | None = None, year: int | None = None) -> list[tuple[str, dict]]:
    """``[(dataset_name, unit), ...]`` — every staging task for the selected datasets/year."""
    names = [dataset] if dataset else list(MODULES)
    return [(name, unit) for name in names for unit in _list_units(MODULES[name], year)]


def run(dataset: str | None = None, year: int | None = None, *, overwrite: bool = False) -> dict:
    """Stage the planned units (skipping those whose ``dst`` already exists unless ``overwrite``),
    then consolidate the footprint index of every asset whose units registered a part this run."""
    tasks = plan(dataset, year)
    staged: list[str] = []
    skipped: list[str] = []
    assets: set[tuple[str, int | None]] = set()
    for name, unit in tasks:
        if not overwrite and s3io.exists(unit["dst"]):
            skipped.append(unit["dst"])
            continue
        result = MODULES[name].stage_unit(unit)
        if result is None:  # ocean tile / empty layer / missing source
            continue
        staged.append(result["uri"])
        if result.get("index_part"):  # iolulc builds its index in place -> no part -> no consolidate
            assets.add((result["asset"], result["year"]))

    indexes = sorted(tile_index.consolidate(asset, yr) for asset, yr in assets)
    return {"planned": len(tasks), "staged": len(staged), "skipped": len(skipped), "indexes": indexes}


def main(argv=None) -> dict:
    """``scripts/stage.py``-style entrypoint: stage selected datasets and consolidate their indexes."""
    parser = argparse.ArgumentParser(description="Stage BII input datasets and consolidate their indexes.")
    parser.add_argument("--dataset", choices=sorted(MODULES), help="stage only this dataset (default: all)")
    parser.add_argument("--year", type=int, help="stage only this year (per-year datasets only)")
    parser.add_argument("--overwrite", action="store_true", help="restage units whose output already exists")
    parser.add_argument("--dry-run", action="store_true", help="list the planned units + existence and exit")
    args = parser.parse_args(argv)

    if args.dry_run:
        units = [{"dataset": n, "id": u["id"], "dst": u["dst"], "exists": s3io.exists(u["dst"])}
                 for n, u in plan(args.dataset, args.year)]
        result = {"planned": len(units), "exists": sum(u["exists"] for u in units), "units": units}
    else:
        result = run(args.dataset, args.year, overwrite=args.overwrite)
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    main()
