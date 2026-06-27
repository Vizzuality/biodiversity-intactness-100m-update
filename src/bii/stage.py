"""Staging driver: enumerate cogs to be staged, run the missing ones, rebuild footprint indexes.

Stages units whose output is missing (or all, with ``overwrite``) via :mod:`bii.orchestration`'s
docker/Batch executors, then rebuilds each fully-staged asset's footprint index. 
Units that produce nothing have an empty sentinel written to mark their absence.
"""

from __future__ import annotations

import inspect
import sys
import time

from . import config, orchestration, io, tile_index
from .staging import MODULES

# landcover builds its index in place (STAC hrefs, no COGs) -> rebuild-exempt.
INDEX_IN_PLACE = ("iolulc",)

# Sentinel marker: a unit that staged nothing (Hansen ocean tile 404) writes this so
# skip-if-exists won't refetch it.
EMPTY_MARKER = ".empty"


def _list_units(module, year: int | None) -> list[dict]:
    """``module.list_units`` filtered to ``year`` when the module is per-year."""
    if year is not None and "years" in inspect.signature(module.list_units).parameters:
        return module.list_units(years=[year])
    return module.list_units()


def plan(dataset: str | None = None, year: int | None = None) -> list[tuple[str, dict]]:
    names = [dataset] if dataset else list(MODULES)
    return [(name, unit) for name in names for unit in _list_units(MODULES[name], year)]


def manifest_item(name: str, unit: dict) -> dict:
    """``asset``/``year`` are recorded so the driver can rebuild indexes after a run without the
    workers' return values."""
    return {"dataset": name, "unit": unit, "asset": MODULES[name].ASSET, "year": unit.get("year")}


def manifest_items(dataset: str | None = None, year: int | None = None) -> list[dict]:
    return [manifest_item(name, unit) for name, unit in plan(dataset, year)]


def stage(item: dict | None = None) -> None:
    """``bii-stage`` entrypoint: Stage one unit (always overwriting; the driver decides skip-if-exists). 
    Reads its unit from the manifest when called with no argument."""
    item = item or orchestration.manifest_line()
    if not MODULES[item["dataset"]].stage_unit(item["unit"]):
        io.put_bytes(b"", item["unit"]["dst"] + EMPTY_MARKER)


def index(item: dict | None = None) -> None:
    """``bii-index`` entrypoint: Rebuild one asset's footprint index from its staged COGs."""
    item = item or orchestration.manifest_line()
    try:
        tile_index.index_cogs(item["asset"], item["year"])
    except FileNotFoundError:
        pass


def staged_dsts(items: list[dict]) -> set[str]:
    """Lists the already existing outputs (or empty markers) for the given manifest items"""
    prefixes = {config.staged_uri(it["asset"]) + "/" for it in items}
    return {uri for p in prefixes for uri in io.list_uris(p)}


def _pending(items: list[dict]) -> list[dict]:
    have = staged_dsts(items)
    return [it for it in items
            if it["unit"]["dst"] not in have and it["unit"]["dst"] + EMPTY_MARKER not in have]


def _manifest_uri() -> str:
    # Unique per run e.g. allowing parallel staging of different datasets/years.
    return config.out_uri("stage", f"manifest_{time.strftime('%Y%m%dT%H%M%S')}.jsonl")


def _failures(items: list[dict], failed: list[dict]) -> list[dict]:
    return [{"dataset": (it := items[f["index"]])["dataset"], "id": it["unit"]["id"],
             "asset": it["asset"], "year": it["year"], "error": f["error"]} for f in failed]


def _run(items: list[dict], executor: str, *, store=None, client=None, wait_fn=None) -> list[dict]:
    """Stage ``items`` (one container per unit); return the failed units."""
    failed = orchestration.run_manifest(
        items, ["bii-stage"], executor=executor, manifest_uri=_manifest_uri(),
        job_name="bii-stage", store=store, client=client, wait_fn=wait_fn,
        label=lambda it: f"{it['dataset']} {it['unit']['id']}")
    return _failures(items, failed)


def reindex(dataset: str | None = None, year: int | None = None, *, executor: str = "docker",
            store: str | None = None, client=None, wait_fn=None) -> dict:
    """Rebuild the footprint indexes for the selected datasets, without staging (e.g. after a manual
    restage, or to repair an index)."""
    todo = sorted({(it["asset"], it["year"]) for it in manifest_items(dataset, year)
                   if it["dataset"] not in INDEX_IN_PLACE}, key=lambda a: (a[0], a[1] or 0))
    failed = _index(todo, executor, store=store, client=client, wait_fn=wait_fn) if todo else []
    return {"executor": executor, "indexes": [a for a in todo if a not in failed], "failed": failed}


def _index(pairs: list[tuple], executor: str, *, store=None, client=None, wait_fn=None) -> list[tuple]:
    """Rebuild each ``(asset, year)`` index (one ``bii-index`` container per pair); return the pairs
    whose index task failed."""
    items = [{"asset": a, "year": y} for a, y in pairs]
    failed = orchestration.run_manifest(
        items, ["bii-index"], executor=executor, manifest_uri=_manifest_uri(),
        job_name="bii-index", store=store, client=client, wait_fn=wait_fn,
        label=lambda it: f"index {it['asset']} {it['year'] or ''}".strip())
    return [pairs[f["index"]] for f in failed]


# --------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------
def run(dataset: str | None = None, year: int | None = None, *, items: list[dict] | None = None,
        executor: str = "docker", overwrite: bool = False, store: str | None = None,
        client=None, wait_fn=None) -> dict:
    """Stage the planned units (skipping those whose ``dst`` exists unless ``overwrite``), then
    rebuild the footprint index of every fully-staged asset. The run continues past a failed unit;
    its asset's index is left unrebuilt. ``items`` overrides the plan (e.g. an AOI subset); ``store``
    is the local stand-in store for the docker executor."""
    items = manifest_items(dataset, year) if items is None else items
    pending = items if overwrite else _pending(items)
    print(f"staging {len(pending)} of {len(items)} units "
          f"({len(items) - len(pending)} skipped, already staged)", file=sys.stderr)
    if not pending:
        return {"planned": len(items), "pending": 0, "executor": executor, "failed": [],
                "incomplete_indexes": [], "indexes": []}

    failed = _run(pending, executor, store=store, client=client, wait_fn=wait_fn)

    # Rebuild excludes assets with a failed unit
    assets = {(it["asset"], it["year"]) for it in pending if it["dataset"] not in INDEX_IN_PLACE}
    incomplete = {(f["asset"], f["year"]) for f in failed}
    todo = sorted(assets - incomplete, key=lambda a: (a[0], a[1] or 0))
    index_failed = _index(todo, executor, store=store, client=client, wait_fn=wait_fn) if todo else []
    return {"planned": len(items), "pending": len(pending), "executor": executor, "failed": failed,
            "incomplete_indexes": sorted(incomplete & assets, key=lambda a: (a[0], a[1] or 0)) + index_failed,
            "indexes": [a for a in todo if a not in index_failed]}
