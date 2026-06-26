"""Processing: compute BII per chunk (the worker) and drive a whole run (the fan-out).

The per-chunk worker and the run driver live together because they share the output layout:
:func:`process` computes one chunk and writes its layer COGs; :func:`run` builds the chunk manifest,
fans it out via the shared docker/Batch executors in :mod:`bii.orchestration`, and reports the
chunks that failed. Mirrors :mod:`bii.stage` (driver + worker in one module):

* **worker** — :func:`process` rebuilds a :class:`cog_worker.Worker` from a ``chunk_params`` dict,
  runs :func:`bii.model.compute_all`, and writes each ``<metric>_<year>`` layer to the deterministic
  key ``<out>/<run_id>/<layer>/<layer>_<north>_<west>.tif`` (ports the notebook's ``persist_cog`` to
  S3/boto3). It always overwrites — the skip-if-exists decision lives in :func:`run`.
* **driver** — :func:`run` drops non-finite and ocean chunks, skips chunks already fully written
  (unless ``overwrite``, mirroring ``stage._pending``), fans the rest out, and reports the failed
  lines (Batch retries each child internally; rerun to pick them up via skip-if-exists).

A chunk is JSON-serializable, so the manifest is plain JSONL and array index N maps to line N — the
same contract :func:`process` (the ``bii-process`` container command) consumes.
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio as rio
from cog_worker import Manager, Worker
from shapely.geometry import box

from . import config, model, orchestration, s3io, tile_index
from .staging import cog

# The metrics :func:`bii.model.calc_bii` returns per year; ``compute_all`` emits one COG per
# ``<metric>_<year>``. Kept here (not imported from model) only as the layer-key source —
# it must stay in sync with calc_bii's result keys.
BII_METRICS = ("abundance", "community_similarity", "bii")

# GDAL read tuning for the remote source reads inside compute_all: caches off so a Batch
# worker's memory stays bounded, retries on transient HTTP failures. Shared with staging.
READ_ENV = cog.GDAL_READ_ENV

# Any chunk overlapping a staged landcover or roads footprint is land we must process; one
# overlapping neither is open water and is dropped.
COVERAGE_ASSETS = ("landcover", "roads")


# --------------------------------------------------------------------------------------
# Output layout
# --------------------------------------------------------------------------------------
def output_layers() -> list[str]:
    """The ``<metric>_<year>`` layer keys one chunk produces — the keys of ``compute_all``."""
    return [f"{metric}_{year}" for year in config.years() for metric in BII_METRICS]


def _coord(v: float) -> str:
    # Fixed precision so the same chunk always maps to the same key (the skip-if-exists check relies on it).
    return f"{v:.6f}"


def output_uri(run_id: str, layer: str, worker: Worker) -> str:
    """Deterministic output key for ``layer`` of the chunk ``worker`` covers.

    ``<out>/<run_id>/<layer>/<layer>_<north>_<west>.tif`` — ``worker.bounds`` is the chunk's
    unbuffered extent in the target CRS (EPSG:4326), so ``north`` = top, ``west`` = left.
    """
    _, _, _, north = worker.bounds
    west = worker.bounds[0]
    return config.out_uri(run_id, layer, f"{layer}_{_coord(north)}_{_coord(west)}.tif")


# --------------------------------------------------------------------------------------
# Worker — compute one chunk, persist its layer COGs (ports notebook 3's persist_cog)
# --------------------------------------------------------------------------------------
def persist_cog(worker: Worker, arr: np.ndarray, uri: str) -> None:
    """Write ``arr`` (cast to float32) as a COG to ``uri`` (S3 or local) via an in-memory rasterio
    ``MemoryFile`` — no temp file. ``worker.write`` clips the buffer and carries the nodata mask.
    Always overwrites; the skip-if-exists decision lives in :func:`run`."""
    arr = arr.astype(np.float32)
    with rio.MemoryFile() as memfile:
        worker.write(arr, memfile, driver="COG", overview_resampling="average")
        data = memfile.read()
    s3io.put_bytes(data, uri)


def process(chunk: dict | None = None, run_id: str | None = None) -> None:
    """Compute BII for one chunk and persist every output layer (always overwriting; :func:`run` is
    what skips chunks already written). As the ``bii-process`` container entrypoint, reads its chunk
    from the manifest (``BII_MANIFEST`` + array index) when called with no argument."""
    chunk = chunk or orchestration.manifest_line()
    run_id = run_id or config.RUN_ID
    worker = Worker(**chunk)
    with rio.Env(**READ_ENV):
        layers = model.compute_all(worker)
    for key, arr in layers.items():
        persist_cog(worker, arr, output_uri(run_id, key, worker))


# --------------------------------------------------------------------------------------
# Driver — manifest build (drop non-finite + ocean), skip-done, fan out, retry failed
# --------------------------------------------------------------------------------------
def manifest_uri(run_id: str) -> str:
    return config.out_uri(run_id, "chunks.jsonl")


def _coverage(assets: tuple[str, ...], year: int) -> gpd.GeoDataFrame | None:
    """Union ``assets``' footprint indexes into one GeoDataFrame with a built ``.sindex``, or
    ``None`` if none exist (so the caller keeps every chunk rather than dropping the whole globe).
    Annual assets are read at ``year``; single-epoch assets at ``year=None``."""
    frames = []
    for asset in assets:
        gdf = tile_index.read_index(asset, year if asset in model.ANNUAL_ASSETS else None)
        if gdf is not None and len(gdf):
            frames.append(gdf[["geometry"]])
    if not frames:
        return None
    gdf = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=tile_index.INDEX_CRS)
    gdf.sindex  # build once; reused across every chunk query below
    return gdf


def chunk_manifest(
    manager: Manager,
    chunksize: int = 4096,
    *,
    coverage_assets: tuple[str, ...] = COVERAGE_ASSETS,
    coverage_year: int | None = None,
) -> list[dict]:
    """Enumerate the processable chunks of ``manager`` as ``chunk_params`` dicts (non-finite and
    ocean chunks dropped). ``coverage_year`` selects the annual coverage index (default: first year)."""
    cov = _coverage(coverage_assets, coverage_year or config.START_YEAR) if coverage_assets else None
    chunks: list[dict] = []
    for params in manager.chunk_params(chunksize):
        bounds = manager.proj.transform_bounds(*params["proj_bounds"], direction="inverse")
        if not np.isfinite(bounds).all():
            continue
        if cov is not None and len(cov.sindex.query(box(*bounds), predicate="intersects")) == 0:
            continue
        # Plain list so the JSONL round-trips identically (chunk_params yields a BoundingBox).
        chunks.append(dict(params, proj_bounds=list(params["proj_bounds"])))
    return chunks


def _pending(chunks: list[dict], run_id: str) -> list[dict]:
    """Chunks missing at least one output layer (skip-if-exists; mirrors ``stage._pending``). Lists
    the output prefix once, then checks membership in-memory."""
    present = set(s3io.list_uris(config.out_uri(run_id)))
    layers = output_layers()
    pending = []
    for c in chunks:
        worker = Worker(**c)
        if not all(output_uri(run_id, layer, worker) in present for layer in layers):
            pending.append(c)
    return pending


def run(
    manager: Manager,
    *,
    run_id: str | None = None,
    chunksize: int = 4096,
    coverage_assets: tuple[str, ...] = COVERAGE_ASSETS,
    coverage_year: int | None = None,
    executor: str = "batch",
    overwrite: bool = False,
    submit: bool = True,
    store: str | None = None,
    client=None,
    wait_fn=None,
) -> dict:
    """Build the manifest and (when ``submit``) run it via ``executor`` (``docker`` locally / ``batch``
    on AWS), reporting the chunks whose container/child failed. Failed chunks are not resubmitted —
    Batch already retries each child (``attempts=3``); rerun ``run`` to pick them up via skip-if-exists.

    Chunks already fully written are skipped unless ``overwrite``. ``submit=False`` writes only the
    manifest — the size gate before any Batch spend. ``store`` is the local stand-in store for the
    docker executor; ``wait_fn`` is injectable so the Batch wait can be driven synchronously in tests."""
    run_id = run_id or config.RUN_ID
    chunks = chunk_manifest(manager, chunksize, coverage_assets=coverage_assets, coverage_year=coverage_year)
    pending = chunks if overwrite else _pending(chunks, run_id)

    if not submit or not pending:
        orchestration.write_manifest(pending, manifest_uri(run_id))
        return {"run_id": run_id, "n_chunks": len(chunks), "pending": len(pending),
                "manifest": manifest_uri(run_id), "submitted": bool(pending) and submit,
                "complete": not pending}

    failed = orchestration.run_manifest(
        pending, ["bii-process"], executor=executor, manifest_uri=manifest_uri(run_id),
        job_name=f"bii-{run_id}", env={"BII_RUN_ID": run_id}, store=store,
        label=lambda c: f"chunk {c['proj_bounds']}", client=client, wait_fn=wait_fn)

    return {"run_id": run_id, "n_chunks": len(chunks), "pending": len(pending),
            "manifest": manifest_uri(run_id), "failed": len(failed),
            "complete": not failed, "submitted": True}
