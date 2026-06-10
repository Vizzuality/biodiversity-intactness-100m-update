"""Orchestrator: build a chunk manifest, submit it as an AWS Batch array job, verify the
outputs, and resubmit the chunks still missing — looping until none remain.

Ports notebook 3's ``_cog_worker_run`` track/retry loop off Dask onto Batch:

1. **Manifest** (:func:`chunk_manifest`) — walk ``manager.chunk_params(chunksize)``, drop
   non-finite (off-projection) chunks and ocean chunks (those overlapping no staged
   ``landcover``/``roads`` footprint), and write the survivors as JSONL. A Batch array index N
   maps to line N, which is exactly what :func:`bii.process.process` consumes.
2. **Submit** (:func:`submit_array`) — one Batch array job over the manifest, with a Spot-friendly
   ``retryStrategy`` and the ``BII_CHUNKS_URI``/``BII_RUN_ID`` env :func:`bii.process.main` reads.
3. **Verify + retry** (:func:`run`) — list ``out/<run_id>/`` once, treat a chunk as done only when
   every output layer key is present, and resubmit just the missing chunks as a smaller array.

Batch infra is deployment-specific, so the queue/definition come from ``BII_BATCH_QUEUE`` /
``BII_BATCH_JOB_DEF`` (or explicit args). The boto3 Batch client and the wait step are injectable
for testing.
"""

from __future__ import annotations

import argparse
import json
import os
import time

import geopandas as gpd
import numpy as np
import pandas as pd
from cog_worker import Manager, Worker
from shapely.geometry import box

from . import config, model, process, tile_index
from .staging import cog

# Any chunk overlapping a staged landcover or roads footprint is land we must process; one
# overlapping neither is open water and is dropped.
COVERAGE_ASSETS = ("landcover", "roads")
_POLL_SECONDS = 30.0


# --------------------------------------------------------------------------------------
# Manifest locations + JSONL I/O (reuses process.py's S3/local byte helpers)
# --------------------------------------------------------------------------------------
def manifest_uri(run_id: str, round_: int = 0) -> str:
    """``chunks.jsonl`` for the initial run, ``chunks_retry<n>.jsonl`` for each retry round."""
    name = "chunks.jsonl" if round_ == 0 else f"chunks_retry{round_}.jsonl"
    return config.out_uri(run_id, name)


def write_manifest(items: list[dict], uri: str) -> str:
    """Write ``items`` as JSONL (one chunk dict per line) to ``uri`` (S3 or local)."""
    process._put_bytes("".join(json.dumps(it) + "\n" for it in items).encode(), uri)
    return uri


def read_manifest(uri: str) -> list[dict]:
    return [json.loads(ln) for ln in process._read_text(uri).splitlines() if ln.strip()]


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
    if cog.is_s3(prefix_uri):
        return set(tile_index._list(prefix_uri))  # list_objects_v2 is already recursive
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
# Batch submit + wait
# --------------------------------------------------------------------------------------
def _batch_client(client=None):
    if client is not None:
        return client
    import boto3  # lazy so unit tests don't need credentials (mirrors cog._s3_client)

    return boto3.client("batch", region_name=config.AWS_REGION)


def submit_array(
    *,
    manifest_uri: str,
    size: int,
    run_id: str,
    job_name: str | None = None,
    job_queue: str | None = None,
    job_definition: str | None = None,
    attempts: int = 3,
    environment: dict | None = None,
    client=None,
) -> str:
    """Submit the manifest as a Batch array job; return the Batch job id.

    Index N processes line N (``size == 1`` submits a plain non-array job, since arrays need ≥ 2).
    ``attempts`` drives the Spot-friendly ``retryStrategy``. The queue/definition fall back to
    ``BII_BATCH_QUEUE`` / ``BII_BATCH_JOB_DEF``."""
    job_queue = job_queue or os.environ.get("BII_BATCH_QUEUE")
    job_definition = job_definition or os.environ.get("BII_BATCH_JOB_DEF")
    if not job_queue or not job_definition:
        raise SystemExit("set BII_BATCH_QUEUE and BII_BATCH_JOB_DEF (or pass job_queue/job_definition)")

    env = {"BII_CHUNKS_URI": manifest_uri, "BII_RUN_ID": run_id, **(environment or {})}
    kwargs = dict(
        jobName=job_name or f"bii-{run_id}",
        jobQueue=job_queue,
        jobDefinition=job_definition,
        containerOverrides={"environment": [{"name": k, "value": str(v)} for k, v in env.items()]},
        retryStrategy={"attempts": attempts},
    )
    if size > 1:
        kwargs["arrayProperties"] = {"size": size}
    return _batch_client(client).submit_job(**kwargs)["jobId"]


def wait_for_array(job_id: str, *, client=None) -> str:
    """Poll Batch until ``job_id`` is SUCCEEDED or FAILED; return that state. A FAILED array is not
    fatal here — the :func:`missing_chunks` diff resubmits whatever indices didn't write."""
    client = _batch_client(client)
    while True:
        status = client.describe_jobs(jobs=[job_id])["jobs"][0].get("status", "")
        if status in ("SUCCEEDED", "FAILED"):
            return status
        time.sleep(_POLL_SECONDS)


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
    injectable so the loop can be driven synchronously in tests (defaults to :func:`wait_for_array`)."""
    run_id = run_id or process.default_run_id()
    wait_fn = wait_fn or wait_for_array
    chunks = chunk_manifest(manager, chunksize, coverage_assets=coverage_assets, coverage_year=coverage_year)

    if not submit or not chunks:
        write_manifest(chunks, manifest_uri(run_id))
        return {"run_id": run_id, "n_chunks": len(chunks), "manifest": manifest_uri(run_id),
                "submitted": bool(chunks) and submit, "complete": not chunks}

    remaining, rounds = chunks, []
    for r in range(max_rounds):
        n = len(remaining)
        muri = write_manifest(remaining, manifest_uri(run_id, r))
        job_id = submit_array(manifest_uri=muri, size=n, run_id=run_id, client=client)
        wait_fn(job_id, client=client)
        remaining = missing_chunks(remaining, run_id)
        rounds.append({"round": r, "job_id": job_id, "submitted": n, "missing": len(remaining)})
        if not remaining:
            break

    return {"run_id": run_id, "n_chunks": len(chunks), "manifest": manifest_uri(run_id),
            "rounds": rounds, "missing": len(remaining), "complete": not remaining, "submitted": True}


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def main(argv=None) -> dict:
    """``scripts/run.py`` entrypoint: build + submit a processing run, then verify/retry."""
    parser = argparse.ArgumentParser(description="Build, submit, and verify a BII processing run.")
    parser.add_argument("--run-id", default=None, help="output prefix (default: BII_RUN_ID / config.RUN_ID)")
    parser.add_argument("--bounds", type=float, nargs=4, metavar=("W", "S", "E", "N"),
                        default=[-180.0, -85.0, 180.0, 85.0], help="analysis extent in EPSG:4326")
    parser.add_argument("--no-submit", action="store_true", help="write the manifest only; don't submit to Batch")
    args = parser.parse_args(argv)

    manager = Manager(bounds=tuple(args.bounds), scale=config.SCALE_DEG, proj=config.PROJ, buffer=config.BUFFER)
    result = run(manager, run_id=args.run_id, submit=not args.no_submit)
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    main()
