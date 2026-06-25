"""Processing-run driver: build a chunk manifest, submit it as a Batch array, verify the outputs,
and resubmit the chunks still missing — looping until none remain.

Ports notebook 3's ``_cog_worker_run`` track/retry loop off Dask onto Batch, over the docker/Batch
executors in :mod:`bii.orchestration`:

1. **Manifest** (:func:`chunk_manifest`) — walk ``manager.chunk_params(chunksize)``, drop non-finite
   (off-projection) and ocean chunks (those overlapping no staged ``landcover``/``roads`` footprint),
   and write the survivors as JSONL. A Batch array index N maps to line N, which is exactly what
   :func:`bii.process.process` consumes.
2. **Submit** (:func:`orchestration.submit_array`) — one Batch array job over the manifest, with the
   ``BII_CHUNKS_URI``/``BII_RUN_ID`` env :func:`bii.process.main` reads.
3. **Verify + retry** (:func:`run`) — list ``out/<run_id>/`` once, treat a chunk as done only when
   every output layer key is present, and resubmit just the missing chunks as a smaller array.
"""

from __future__ import annotations

import os

import geopandas as gpd
import numpy as np
import pandas as pd
from cog_worker import Manager, Worker
from shapely.geometry import box

from . import config, model, orchestration, process, s3io, tile_index

# Any chunk overlapping a staged landcover or roads footprint is land we must process; one
# overlapping neither is open water and is dropped.
COVERAGE_ASSETS = ("landcover", "roads")


def manifest_uri(run_id: str, round_: int = 0) -> str:
    """``chunks.jsonl`` for the initial run, ``chunks_retry<n>.jsonl`` for each retry round."""
    name = "chunks.jsonl" if round_ == 0 else f"chunks_retry{round_}.jsonl"
    return config.out_uri(run_id, name)


# --------------------------------------------------------------------------------------
# Manifest build — drop non-finite + ocean chunks
# --------------------------------------------------------------------------------------
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


# --------------------------------------------------------------------------------------
# Verify — diff manifest chunks against the outputs present in S3
# --------------------------------------------------------------------------------------
def _list_keys(prefix_uri: str) -> set[str]:
    """All object URIs under ``prefix_uri`` (recursive; S3 or local), as a set for membership."""
    if s3io.is_s3(prefix_uri):
        return set(s3io.list_uris(prefix_uri))  # list_objects_v2 is already recursive
    return {
        os.path.join(root, f)
        for root, _, files in os.walk(prefix_uri)
        for f in files
    } if os.path.isdir(prefix_uri) else set()


def expected_uris(chunk: dict, run_id: str) -> list[str]:
    """The output COG keys ``chunk`` must produce (one per ``<metric>_<year>`` layer)."""
    worker = Worker(**chunk)
    return [process.output_uri(run_id, layer, worker) for layer in process.output_layers()]


def missing_chunks(chunks: list[dict], run_id: str) -> list[dict]:
    """Chunks not yet fully written. Lists the output prefix once, then checks membership in-memory."""
    present = _list_keys(config.out_uri(run_id))
    return [c for c in chunks if not all(uri in present for uri in expected_uris(c, run_id))]


# --------------------------------------------------------------------------------------
# Driver — submit, wait, verify, retry-missing until empty
# --------------------------------------------------------------------------------------
def run(
    manager: Manager,
    *,
    run_id: str | None = None,
    chunksize: int = 4096,
    coverage_assets: tuple[str, ...] = COVERAGE_ASSETS,
    coverage_year: int | None = None,
    max_rounds: int = 5,
    submit: bool = True,
    client=None,
    wait_fn=None,
) -> dict:
    """Build the manifest and (when ``submit``) verify/retry-missing until complete or ``max_rounds``.

    ``submit=False`` writes only the manifest — the size gate before any Batch spend. ``wait_fn`` is
    injectable so the loop can be driven synchronously in tests (defaults to
    :func:`orchestration.wait_for_array`)."""
    run_id = run_id or config.RUN_ID
    wait_fn = wait_fn or orchestration.wait_for_array
    chunks = chunk_manifest(manager, chunksize, coverage_assets=coverage_assets, coverage_year=coverage_year)

    if not submit or not chunks:
        orchestration.write_manifest(chunks, manifest_uri(run_id))
        return {"run_id": run_id, "n_chunks": len(chunks), "manifest": manifest_uri(run_id),
                "submitted": bool(chunks) and submit, "complete": not chunks}

    remaining, rounds = chunks, []
    for r in range(max_rounds):
        n = len(remaining)
        muri = orchestration.write_manifest(remaining, manifest_uri(run_id, r))
        job_id = orchestration.submit_array(
            size=n, job_name=f"bii-{run_id}",
            environment={"BII_CHUNKS_URI": muri, "BII_RUN_ID": run_id}, client=client)
        wait_fn(job_id, client=client)
        remaining = missing_chunks(remaining, run_id)
        rounds.append({"round": r, "job_id": job_id, "submitted": n, "missing": len(remaining)})
        if not remaining:
            break

    return {"run_id": run_id, "n_chunks": len(chunks), "manifest": manifest_uri(run_id),
            "rounds": rounds, "missing": len(remaining), "complete": not remaining, "submitted": True}
