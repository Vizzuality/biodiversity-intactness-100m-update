"""Staging driver: enumerate staging units, run the missing ones, rebuild footprint indexes.

Enumerates every staging unit (one COG/tile/year per dataset), stages the ones whose output is
missing (or all, with ``overwrite``) via :mod:`bii.orchestration`'s docker/Batch executors, and
rebuilds each fully-staged asset's footprint index from its staged COGs. Both executors dispatch the
``bii-stage`` entrypoint (:func:`stage`) in this module — driver + worker together, mirroring
:mod:`bii.process`.

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
    array index) when called with no argument. Producing nothing (an ocean tile) is not a failure."""
    item = item or orchestration.manifest_line()
    MODULES[item["dataset"]].stage_unit(item["unit"])


def staged_dsts(items: list[dict]) -> set[str]:
    """The ``dst`` URIs already staged, listing only the per-asset prefixes ``items`` span (every dst
    is ``staged_uri(asset, ...)``) — one listing per asset rather than a HEAD per unit, and a
    single-dataset run skips the rest of the tree. Shared by ``_pending`` and the dry-run summary."""
    prefixes = {config.staged_uri(it["asset"]) + "/" for it in items}
    return {uri for p in prefixes for uri in s3io.list_uris(p)}


def _pending(items: list[dict]) -> list[dict]:
    """Items whose output ``dst`` does not yet exist (the skip-if-exists filter)."""
    have = staged_dsts(items)
    return [it for it in items if it["unit"]["dst"] not in have]


def print_summary(items: list[dict], pending: list[dict]) -> None:
    """Per-execution job summary on stderr (stdout stays JSON)."""
    print(f"staging {len(pending)} of {len(items)} units "
          f"({len(items) - len(pending)} skipped, already staged)", file=sys.stderr)


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


def _consolidate(assets) -> list[str]:
    """Rebuild each ``(asset, year)`` index from its staged COGs; skip assets with none staged."""
    out = []
    for asset, yr in sorted(assets, key=lambda a: (a[0], a[1] or 0)):
        try:
            out.append(tile_index.index_cogs(asset, yr))
        except FileNotFoundError:
            pass
    return out


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
    print_summary(items, pending)
    if not pending:
        return {"planned": len(items), "pending": 0, "executor": executor, "failed": [],
                "incomplete_indexes": [], "indexes": []}

    failed = _run(pending, executor, store=store, client=client, wait_fn=wait_fn)

    # Rebuild set = the plan (every staged dataset but landcover, which builds its index in place),
    # minus assets with a failed unit: a missing COG would silently under-cover the orchestrator's
    # ocean-drop (real land chunks dropped), so leave that asset's prior index.
    assets = {(it["asset"], it["year"]) for it in pending if it["dataset"] not in INDEX_IN_PLACE}
    incomplete = {(f["asset"], f["year"]) for f in failed}
    return {"planned": len(items), "pending": len(pending), "executor": executor, "failed": failed,
            "incomplete_indexes": sorted(incomplete & assets, key=lambda a: (a[0], a[1] or 0)),
            "indexes": _consolidate(assets - incomplete)}
