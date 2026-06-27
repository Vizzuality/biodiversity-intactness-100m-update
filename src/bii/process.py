"""Processing: compute BII per chunk (worker) and drive a run (fan-out).

A chunk is JSON-serializable, so the manifest is plain JSONL and array index N maps to line N.
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio as rio
from cog_worker import Manager, Worker
from shapely.geometry import box

from . import config, model, orchestration, io, tile_index
from . import cog

# landcover is the model's nodata mask, so a chunk overlapping no landcover footprint is open
# water and dropped.
COVERAGE_ASSETS = ("landcover",)


def output_layers() -> list[str]:
    return [f"bii_{year}" for year in config.years()]


def _coord(v: float) -> str:
    # Fixed precision so a chunk always maps to the same key.
    return f"{v:.6f}"


def output_uri(run_id: str, layer: str, worker: Worker) -> str:
    """Deterministic output key ``<out>/<run_id>/<layer>/<layer>_<north>_<west>.tif``.
    ``worker.bounds`` is the unbuffered extent in EPSG:4326 (north = top, west = left)."""
    _, _, _, north = worker.bounds
    west = worker.bounds[0]
    return config.out_uri(run_id, layer, f"{layer}_{_coord(north)}_{_coord(west)}.tif")


def process(chunk: dict | None = None, run_id: str | None = None) -> None:
    """Worker entrypoint: compute BII for one chunk and write every output layer as a COG.
    Reads its chunk from the manifest (from environment variables) when called with no argument.
    """
    chunk = chunk or orchestration.manifest_line()
    run_id = run_id or config.RUN_ID
    worker = Worker(**chunk)
    with rio.Env(**cog.GDAL_READ_ENV):
        for key, arr in model.compute_all(worker):
            with io.staged_local_path(output_uri(run_id, key, worker)) as out:
                worker.write(arr, out, driver="COG", overview_resampling="average")


def manifest_uri(run_id: str) -> str:
    return config.out_uri(run_id, "chunks.jsonl")


def _coverage(assets: tuple[str, ...], year: int) -> gpd.GeoDataFrame | None:
    """Get coverage assets' index footprints as a GeoDataFrame."""
    frames = []
    for asset in assets:
        gdf = tile_index.read_index(asset, year if asset in model.ANNUAL_ASSETS else None)
        if gdf is not None and len(gdf):
            frames.append(gdf[["geometry"]])
    if not frames:
        return None
    gdf = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=tile_index.INDEX_CRS)
    gdf.sindex  # build once; reused across every chunk query
    return gdf


def chunk_manifest(
    manager: Manager,
    chunksize: int = 8192,
    *,
    coverage_assets: tuple[str, ...] = COVERAGE_ASSETS,
    coverage_year: int | None = None,
) -> list[dict]:
    """Processable chunks of ``manager`` as ``chunk_params`` dicts (non-finite and ocean chunks
    dropped)."""
    cov = _coverage(coverage_assets, coverage_year or config.START_YEAR) if coverage_assets else None
    chunks: list[dict] = []
    for params in manager.chunk_params(chunksize):
        bounds = manager.proj.transform_bounds(*params["proj_bounds"], direction="inverse")
        if not np.isfinite(bounds).all():
            continue
        if cov is not None and len(cov.sindex.query(box(*bounds), predicate="intersects")) == 0:
            continue
        # list() so JSONL round-trips identically (chunk_params yields a BoundingBox).
        chunks.append(dict(params, proj_bounds=list(params["proj_bounds"])))
    return chunks


def _pending(chunks: list[dict], run_id: str) -> list[dict]:
    """Chunks missing at least one output layer (skip-if-exists). Lists the output prefix once,
    then checks membership in-memory."""
    present = set(io.list_uris(config.out_uri(run_id)))
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
    chunksize: int = 8192,
    coverage_assets: tuple[str, ...] = COVERAGE_ASSETS,
    coverage_year: int | None = None,
    executor: str = "batch",
    overwrite: bool = False,
    submit: bool = True,
    store: str | None = None,
    client=None,
    wait_fn=None,
) -> dict:
    """Build the manifest and (when ``submit``) run it via ``executor`` (``docker`` locally /
    ``batch`` on AWS), reporting failed chunks. 

    Already-written chunks are skipped unless ``overwrite``. 
    ``submit=False`` writes only the manifest. 
    ``store`` is the local stand-in for the docker executor; 
    ``wait_fn`` is injectable so the Batch wait can be driven synchronously in tests."""
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
