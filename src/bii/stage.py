"""Staging driver: enumerate staging units, run the missing ones, rebuild footprint indexes.

Enumerates every staging unit (one COG/tile/year per dataset), stages the ones whose output is
missing (or all, with ``overwrite``) via :mod:`bii.orchestration`'s docker/Batch executors, and
rebuilds each fully-staged asset's footprint index from its staged COGs. Both executors dispatch the
``bii-stage-worker`` entrypoint in :mod:`bii.stage_worker`.

A thin driver over :mod:`bii.staging`: each module exposes ``list_units`` / ``stage_unit`` and carries
``dst`` + ``ASSET``, so this module stays dataset-agnostic. The skip-if-exists decision lives here;
the per-unit staging lives in the worker. A unit that legitimately produces nothing (an ocean Hansen
tile 404s) exits 0 and is not a failure; one whose worker exits non-zero (docker) or whose Batch child
ends FAILED after retries is reported, and its asset's index is left unrebuilt.
"""

from __future__ import annotations

import inspect
import sys

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


def manifest_items(dataset: str | None = None, year: int | None = None) -> list[dict]:
    """The plan as worker-readable manifest lines: ``{dataset, unit, asset, year}``. ``asset``/``year``
    are recorded so the driver can rebuild indexes after a Batch/docker run without the workers'
    return values."""
    return [{"dataset": name, "unit": unit, "asset": MODULES[name].ASSET, "year": unit.get("year")}
            for name, unit in plan(dataset, year)]


def _pending(items: list[dict]) -> list[dict]:
    """Items whose output ``dst`` does not yet exist (the skip-if-exists filter)."""
    return [it for it in items if not s3io.exists(it["unit"]["dst"])]


def print_summary(items: list[dict], pending: list[dict]) -> None:
    """Per-execution job summary on stderr (stdout stays JSON)."""
    print(f"staging {len(pending)} of {len(items)} units "
          f"({len(items) - len(pending)} skipped, already staged)", file=sys.stderr)


def _manifest_uri() -> str:
    return config.out_uri("stage", "manifest.jsonl")


# --------------------------------------------------------------------------------------
# Executors — write the manifest, run it, map failed lines back to units
# --------------------------------------------------------------------------------------
def _failures(items: list[dict], failed: list[dict]) -> list[dict]:
    """Map ``orchestration`` ``{"index", "error"}`` failures to per-unit run-report records (also
    drives the incomplete-index skip)."""
    return [{"dataset": (it := items[f["index"]])["dataset"], "id": it["unit"]["id"],
             "asset": it["asset"], "year": it["year"], "error": f["error"]} for f in failed]


def run_docker(items: list[dict], store: str | None = None) -> list[dict]:
    """Stage ``items`` in one ``docker run`` per unit (local mirror of the Batch array); return the
    failed units."""
    muri = orchestration.write_manifest(items, _manifest_uri())
    failed = orchestration.run_docker(items, ["bii-stage-worker"], manifest_uri=muri,
                                      manifest_env="BII_STAGE_MANIFEST", store=store,
                                      label=lambda it: f"{it['dataset']} {it['unit']['id']}")
    return _failures(items, failed)


def run_batch(items: list[dict], *, client=None, wait_fn=None) -> list[dict]:
    """Submit ``items`` as one Batch array job; return the units whose child ended FAILED."""
    muri = orchestration.write_manifest(items, _manifest_uri())
    failed = orchestration.run_batch(items, ["bii-stage-worker"], manifest_uri=muri,
                                     manifest_env="BII_STAGE_MANIFEST", job_name="bii-stage",
                                     client=client, wait_fn=wait_fn)
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
def run(dataset: str | None = None, year: int | None = None, *, executor: str = "docker",
        overwrite: bool = False, client=None, wait_fn=None) -> dict:
    """Stage the planned units (skipping those whose ``dst`` exists unless ``overwrite``) via the
    chosen executor (``docker`` locally / ``batch`` on AWS), then rebuild the footprint index of
    every fully-staged asset. The run continues past a failed unit; failures are reported and their
    asset's index is left unrebuilt."""
    items = manifest_items(dataset, year)
    pending = items if overwrite else _pending(items)
    print_summary(items, pending)
    if not pending:
        return {"planned": len(items), "pending": 0, "executor": executor, "failed": [],
                "incomplete_indexes": [], "indexes": []}

    if executor == "docker":
        failed = run_docker(pending)
    elif executor == "batch":
        failed = run_batch(pending, client=client, wait_fn=wait_fn)
    else:
        raise SystemExit(f"unknown executor {executor!r} (docker | batch)")

    # Rebuild set = the plan (every staged dataset but landcover, which builds its index in place),
    # minus assets with a failed unit: a missing COG would silently under-cover the orchestrator's
    # ocean-drop (real land chunks dropped), so leave that asset's prior index.
    assets = {(it["asset"], it["year"]) for it in pending if it["dataset"] not in INDEX_IN_PLACE}
    incomplete = {(f["asset"], f["year"]) for f in failed}
    return {"planned": len(items), "pending": len(pending), "executor": executor, "failed": failed,
            "incomplete_indexes": sorted(incomplete & assets, key=lambda a: (a[0], a[1] or 0)),
            "indexes": _consolidate(assets - incomplete)}
