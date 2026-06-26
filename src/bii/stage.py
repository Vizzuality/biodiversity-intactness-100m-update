"""Staging driver: enumerate staging units, run the missing ones, rebuild footprint indexes.

Enumerates every staging unit (one COG/tile/year per dataset), stages the ones whose output is
missing (or all, with ``overwrite``) via :mod:`bii.orchestration`'s docker/Batch executors, then
rebuilds each fully-staged asset's footprint index from its staged COGs as a second fan-out over the
same executor (so the COG-header reads run cloud-adjacent on Batch). Both phases dispatch entrypoints
in this module — ``bii-stage`` (:func:`stage`) and ``bii-index`` (:func:`index`) — driver + worker
together, mirroring :mod:`bii.process`.

A thin driver over :mod:`bii.staging`: each module exposes ``list_units`` / ``stage_unit`` and carries
``dst`` + ``ASSET``, so this module stays dataset-agnostic. The skip-if-exists decision lives in the
driver; the per-unit staging lives in :func:`stage`. A unit that legitimately produces nothing (an
ocean Hansen tile 404s) exits 0 and is not a failure; one whose worker exits non-zero (docker) or
whose Batch child ends FAILED after retries is reported, and its asset's index is left unrebuilt.
"""

from __future__ import annotations

import inspect
import sys
import time

from . import config, orchestration, s3io, tile_index
from .staging import MODULES

# landcover builds its index in place (STAC hrefs, no COGs) -> rebuild-exempt.
INDEX_IN_PLACE = ("iolulc",)

# Sentinel sibling of a unit's dst: a unit that legitimately staged nothing (an ocean Hansen tile
# 404s) writes this so skip-if-exists treats it as done instead of refetching it every run. Not a
# ``.tif``, so tile_index ignores it when rebuilding the footprint index from staged COGs.
EMPTY_MARKER = ".empty"


def _list_units(module, year: int | None) -> list[dict]:
    """``module.list_units`` filtered to ``year`` when the module is per-year, else its full list."""
    if year is not None and "years" in inspect.signature(module.list_units).parameters:
        return module.list_units(years=[year])
    return module.list_units()


def plan(dataset: str | None = None, year: int | None = None) -> list[tuple[str, dict]]:
    """``[(dataset_name, unit), ...]`` — every staging task for the selected datasets/year."""
    names = [dataset] if dataset else list(MODULES)
    return [(name, unit) for name in names for unit in _list_units(MODULES[name], year)]


def manifest_item(name: str, unit: dict) -> dict:
    """One worker-readable manifest line ``{dataset, unit, asset, year}``. ``asset``/``year`` are
    recorded so the driver can rebuild indexes after a run without the workers' return values."""
    return {"dataset": name, "unit": unit, "asset": MODULES[name].ASSET, "year": unit.get("year")}


def manifest_items(dataset: str | None = None, year: int | None = None) -> list[dict]:
    """The plan as worker-readable manifest lines."""
    return [manifest_item(name, unit) for name, unit in plan(dataset, year)]


def stage(item: dict | None = None) -> None:
    """Stage one unit (always overwriting; the driver decides skip-if-exists). As the
    ``bii-stage`` container entrypoint, reads its unit from the manifest (``BII_MANIFEST`` +
    array index) when called with no argument. Producing nothing (an ocean tile) is not a failure —
    it writes an ``EMPTY_MARKER`` sentinel so skip-if-exists won't refetch it next run."""
    item = item or orchestration.manifest_line()
    if not MODULES[item["dataset"]].stage_unit(item["unit"]):
        s3io.put_bytes(b"", item["unit"]["dst"] + EMPTY_MARKER)


def index(item: dict | None = None) -> None:
    """Rebuild one asset's footprint index from its staged COGs — the ``bii-index`` consolidation
    worker. As an entrypoint it reads its ``{asset, year}`` from the manifest. Run on the executor so
    the COG-header reads are cloud-adjacent on Batch. An asset that staged only ocean tiles has no
    COGs, so there is nothing to index — not a failure."""
    item = item or orchestration.manifest_line()
    try:
        tile_index.index_cogs(item["asset"], item["year"])
    except FileNotFoundError:
        pass


def staged_dsts(items: list[dict]) -> set[str]:
    """The ``dst`` URIs already staged, listing only the per-asset prefixes ``items`` span (every dst
    is ``staged_uri(asset, ...)``) — one listing per asset rather than a HEAD per unit, and a
    single-dataset run skips the rest of the tree. Shared by ``_pending`` and the dry-run summary."""
    prefixes = {config.staged_uri(it["asset"]) + "/" for it in items}
    return {uri for p in prefixes for uri in s3io.list_uris(p)}


def _pending(items: list[dict]) -> list[dict]:
    """Items whose output ``dst`` (or its ``EMPTY_MARKER`` sentinel) does not yet exist (the
    skip-if-exists filter)."""
    have = staged_dsts(items)
    return [it for it in items
            if it["unit"]["dst"] not in have and it["unit"]["dst"] + EMPTY_MARKER not in have]


def _manifest_uri() -> str:
    # Unique per run: Batch workers read the manifest lazily at runtime, so a fixed name would let a
    # later run's manifest clobber the lines an in-flight job still has to read.
    return config.out_uri("stage", f"manifest_{time.strftime('%Y%m%dT%H%M%S')}.jsonl")


# --------------------------------------------------------------------------------------
# Executor — run the units, map failed lines back to units
# --------------------------------------------------------------------------------------
def _failures(items: list[dict], failed: list[dict]) -> list[dict]:
    """Map ``orchestration`` ``{"index", "error"}`` failures to per-unit run-report records (also
    drives the incomplete-index skip)."""
    return [{"dataset": (it := items[f["index"]])["dataset"], "id": it["unit"]["id"],
             "asset": it["asset"], "year": it["year"], "error": f["error"]} for f in failed]


def _run(items: list[dict], executor: str, *, store=None, client=None, wait_fn=None) -> list[dict]:
    """Stage ``items`` via the chosen executor (one container per unit); return the failed units."""
    failed = orchestration.run_manifest(
        items, ["bii-stage"], executor=executor, manifest_uri=_manifest_uri(),
        job_name="bii-stage", store=store, client=client, wait_fn=wait_fn,
        label=lambda it: f"{it['dataset']} {it['unit']['id']}")
    return _failures(items, failed)


def reindex(dataset: str | None = None, year: int | None = None, *, executor: str = "docker",
            store: str | None = None, client=None, wait_fn=None) -> dict:
    """Rebuild the footprint indexes for the selected datasets from their staged COGs, without
    staging — the index fan-out on its own (e.g. after a manual restage, or to repair an index)."""
    todo = sorted({(it["asset"], it["year"]) for it in manifest_items(dataset, year)
                   if it["dataset"] not in INDEX_IN_PLACE}, key=lambda a: (a[0], a[1] or 0))
    failed = _index(todo, executor, store=store, client=client, wait_fn=wait_fn) if todo else []
    return {"executor": executor, "indexes": [a for a in todo if a not in failed], "failed": failed}


def _index(pairs: list[tuple], executor: str, *, store=None, client=None, wait_fn=None) -> list[tuple]:
    """Rebuild each ``(asset, year)`` index via the chosen executor (one ``bii-index`` container per
    pair, so the reads are cloud-adjacent on Batch); return the pairs whose index task failed."""
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
    """Stage the planned units (skipping those whose ``dst`` exists unless ``overwrite``) via the
    chosen executor (``docker`` locally / ``batch`` on AWS), then rebuild the footprint index of
    every fully-staged asset. The run continues past a failed unit; failures are reported and their
    asset's index is left unrebuilt. ``items`` overrides the plan (e.g. an AOI subset); ``store`` is
    the local stand-in store for the docker executor."""
    items = manifest_items(dataset, year) if items is None else items
    pending = items if overwrite else _pending(items)
    print(f"staging {len(pending)} of {len(items)} units "
          f"({len(items) - len(pending)} skipped, already staged)", file=sys.stderr)
    if not pending:
        return {"planned": len(items), "pending": 0, "executor": executor, "failed": [],
                "incomplete_indexes": [], "indexes": []}

    failed = _run(pending, executor, store=store, client=client, wait_fn=wait_fn)

    # Rebuild set = the plan (every staged dataset but landcover, which builds its index in place),
    # minus assets with a failed unit: a missing COG would silently under-cover the orchestrator's
    # ocean-drop (real land chunks dropped), so leave that asset's prior index.
    assets = {(it["asset"], it["year"]) for it in pending if it["dataset"] not in INDEX_IN_PLACE}
    incomplete = {(f["asset"], f["year"]) for f in failed}
    todo = sorted(assets - incomplete, key=lambda a: (a[0], a[1] or 0))
    index_failed = _index(todo, executor, store=store, client=client, wait_fn=wait_fn) if todo else []
    return {"planned": len(items), "pending": len(pending), "executor": executor, "failed": failed,
            "incomplete_indexes": sorted(incomplete & assets, key=lambda a: (a[0], a[1] or 0)) + index_failed,
            "indexes": [a for a in todo if a not in index_failed]}
