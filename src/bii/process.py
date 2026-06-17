"""Worker entrypoint — compute BII for one chunk and persist the output COGs.

The per-chunk unit of the processing fan-out. :func:`process` runs three ways with one code path:
locally from a chunk dict (``scripts/test_chunk.py``), as one index of a Batch array job
(:func:`main`, dispatched by ``AWS_BATCH_JOB_ARRAY_INDEX`` against an S3 ``chunks.jsonl`` manifest),
and from the orchestrator's retry loop.

A chunk is a ``cog_worker`` ``chunk_params()`` dict — JSON-serializable, so the manifest is plain
JSONL. :func:`process` rebuilds a :class:`cog_worker.Worker`, runs :func:`bii.model.compute_all`,
and writes each layer to ``<out>/<run_id>/<layer>/<layer>_<north>_<west>.tif`` (ports the notebook's
``persist_cog`` to S3/boto3). It is idempotent: an existing layer is skipped, and a chunk whose every
layer exists short-circuits before any reads, so the resubmit-missing loop never recomputes work.
"""

from __future__ import annotations

import json
import os

import numpy as np
import rasterio as rio
from cog_worker import Worker

from . import config, model, s3io
from .staging import cog

# The metrics :func:`bii.model.calc_bii` returns per year; ``compute_all`` emits one COG per
# ``<metric>_<year>``. Kept here (not imported from model) only as the precheck key source —
# it must stay in sync with calc_bii's result keys.
BII_METRICS = ("abundance", "community_similarity", "bii")

# GDAL read tuning for the remote source reads inside compute_all: caches off so a Batch
# worker's memory stays bounded, retries on transient HTTP failures. Shared with staging.
READ_ENV = cog.GDAL_READ_ENV


# --------------------------------------------------------------------------------------
# Output layout
# --------------------------------------------------------------------------------------
def output_layers() -> list[str]:
    """The ``<metric>_<year>`` layer keys one chunk produces — the keys of ``compute_all``."""
    return [f"{metric}_{year}" for year in config.years() for metric in BII_METRICS]


def _coord(v: float) -> str:
    # Fixed precision so the same chunk always maps to the same key (idempotent skip relies on it).
    return f"{v:.6f}"


def output_uri(run_id: str, layer: str, worker: Worker) -> str:
    """Deterministic output key for ``layer`` of the chunk ``worker`` covers.

    ``<out>/<run_id>/<layer>/<layer>_<north>_<west>.tif`` — ``worker.bounds`` is the chunk's
    unbuffered extent in the target CRS (EPSG:4326), so ``north`` = top, ``west`` = left.
    """
    _, _, _, north = worker.bounds
    west = worker.bounds[0]
    return config.out_uri(run_id, layer, f"{layer}_{_coord(north)}_{_coord(west)}.tif")


def default_run_id() -> str:
    """Run id for the output prefix — ``BII_RUN_ID`` (operational) overrides the config default."""
    return os.environ.get("BII_RUN_ID") or config.RUN_ID


# --------------------------------------------------------------------------------------
# COG persistence (ports notebook 3's persist_cog; S3 or local)
# --------------------------------------------------------------------------------------
def persist_cog(worker: Worker, arr: np.ndarray, uri: str, *, skip_existing: bool = True) -> bool:
    """Write ``arr`` (cast to float32) as a COG to ``uri`` (S3 or local) via an in-memory rasterio
    ``MemoryFile`` — no temp file. ``worker.write`` clips the buffer and carries the nodata mask.
    Returns ``False`` if skipped (already there)."""
    if skip_existing and s3io.exists(uri):
        return False
    arr = arr.astype(np.float32)
    with rio.MemoryFile() as memfile:
        worker.write(arr, memfile, driver="COG", overview_resampling="average")
        data = memfile.read()
    s3io.put_bytes(data, uri)
    return True


# --------------------------------------------------------------------------------------
# Process one chunk
# --------------------------------------------------------------------------------------
def process(chunk: dict, run_id: str | None = None, *, skip_existing: bool = True) -> dict:
    """Compute BII for one chunk and persist every output layer; return a result summary.

    ``chunk`` is a ``cog_worker`` ``chunk_params()`` dict. Idempotent: if every output already
    exists the reads + compute are skipped entirely; otherwise only missing layers are written.
    """
    run_id = run_id or default_run_id()
    worker = Worker(**chunk)
    result = {"run_id": run_id, "bounds": list(worker.bounds), "complete": True}

    targets = {layer: output_uri(run_id, layer, worker) for layer in output_layers()}
    if skip_existing and all(s3io.exists(uri) for uri in targets.values()):
        return result | {"written": [], "skipped": list(targets.values())}

    with rio.Env(**READ_ENV):
        layers = model.compute_all(worker)

    written, skipped = [], []
    for key, arr in layers.items():
        uri = targets[key]
        (written if persist_cog(worker, arr, uri, skip_existing=skip_existing) else skipped).append(uri)

    return result | {"written": written, "skipped": skipped}


# --------------------------------------------------------------------------------------
# Batch entrypoint — AWS_BATCH_JOB_ARRAY_INDEX -> line N of the S3 chunks.jsonl manifest
# --------------------------------------------------------------------------------------
def load_chunk(manifest_uri: str, index: int) -> dict:
    """Return chunk ``index`` (0-based line) of a JSONL manifest at ``manifest_uri`` (S3 or local)."""
    lines = [ln for ln in s3io.read_text(manifest_uri).splitlines() if ln.strip()]
    return json.loads(lines[index])


def main(argv=None) -> dict:
    """Batch array entrypoint: resolve this index's chunk from the manifest and process it.

    Reads ``BII_CHUNKS_URI`` (the manifest), ``AWS_BATCH_JOB_ARRAY_INDEX`` (defaults to 0 for a
    one-off local run), and ``BII_RUN_ID`` (output prefix) from the environment — see ``.env``.
    """
    manifest = os.environ.get("BII_CHUNKS_URI")
    if not manifest:
        raise SystemExit("BII_CHUNKS_URI must point at the chunks.jsonl manifest")
    index = int(os.environ.get("AWS_BATCH_JOB_ARRAY_INDEX", "0"))
    chunk = load_chunk(manifest, index)
    result = process(chunk, run_id=default_run_id())
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    main()
