#!/usr/bin/env python
"""Single-chunk local test — the gate before any Batch fan-out (architecture plan step 5).

Builds a ``cog_worker`` ``Manager`` over the original Costa Rica test bounds ``(-86, 9, -84, 11)``,
takes the first chunk of ``chunk_params()``, and runs :func:`bii.process.process` on it — the
*identical* code path a Batch array index runs, just driven locally and writing to a local output
dir instead of S3. Every output COG is then validated (openable, EPSG:4326, float32, expected
unbuffered shape, some finite data), so a green run here means the per-chunk worker is sound
before spending on the global submit.

    python scripts/test_chunk.py                         # Costa Rica, first chunk, ./data/test_chunk_out
    python scripts/test_chunk.py --bounds -86 9 -84 11 --chunksize 2048 --out /tmp/bii_chunk

This reads the real remote sources through ``tile_index.lookup`` (and live LULC STAC), so it needs
network + staged inputs in S3; it is not part of the offline ``-m "not integration"`` unit suite.
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import rasterio as rio
from cog_worker import Manager, Worker

from bii import config, process

# Original test region from the notebooks — Costa Rica, small enough to compute one chunk locally
# yet covering land/ocean/roads so every predictor is exercised.
DEFAULT_BOUNDS = (-86.0, 9.0, -84.0, 11.0)


def first_chunk(manager: Manager, chunksize: int, index: int = 0) -> dict:
    """The ``index``-th ``chunk_params`` dict, JSON round-tripped so it matches exactly what a Batch
    worker loads from ``chunks.jsonl`` (``proj_bounds`` becomes a plain list, as in the manifest)."""
    params = list(manager.chunk_params(chunksize))
    if not params:
        raise SystemExit("manager produced no chunks for these bounds/chunksize")
    if index >= len(params):
        raise SystemExit(f"chunk index {index} out of range ({len(params)} chunks)")
    return json.loads(json.dumps(dict(params[index], proj_bounds=list(params[index]["proj_bounds"]))))


def validate_output(uri: str, worker: Worker) -> None:
    """Assert ``uri`` is a usable BII output COG: openable, EPSG:4326, float32, sized to the chunk's
    unbuffered grid, and carrying at least one finite (unmasked) value. Raises on any failure."""
    with rio.open(uri) as src:
        assert src.crs and src.crs.to_epsg() == 4326, f"{uri}: crs {src.crs} != EPSG:4326"
        assert src.dtypes[0] == "float32", f"{uri}: dtype {src.dtypes[0]} != float32"
        assert (src.height, src.width) == (worker.height, worker.width), (
            f"{uri}: shape {(src.height, src.width)} != {(worker.height, worker.width)}"
        )
        arr = src.read(1, masked=True)
    finite = np.isfinite(arr.filled(np.nan))
    assert finite.any(), f"{uri}: no finite data"


def run(bounds: tuple[float, ...], chunksize: int, run_id: str | None, index: int) -> dict:
    manager = Manager(bounds=bounds, scale=config.SCALE_DEG, proj=config.PROJ, buffer=config.BUFFER)
    chunk = first_chunk(manager, chunksize, index)
    worker = Worker(**chunk)
    print(f"chunk {index}: bounds={tuple(worker.bounds)} size={worker.width}x{worker.height}", file=sys.stderr)

    result = process.process(chunk, run_id=run_id)

    outputs = result["written"] + result["skipped"]
    for uri in outputs:
        validate_output(uri, worker)
    print(f"validated {len(outputs)} output COG(s) under {config.OUT_ROOT}", file=sys.stderr)
    return result | {"validated": len(outputs)}


def main(argv=None) -> dict:
    parser = argparse.ArgumentParser(description="Run and validate ONE BII chunk locally (pre-fan-out gate).")
    parser.add_argument("--bounds", type=float, nargs=4, metavar=("W", "S", "E", "N"),
                        default=list(DEFAULT_BOUNDS), help="analysis extent in EPSG:4326 (default: Costa Rica)")
    parser.add_argument("--chunksize", type=int, default=4096, help="chunk side in pixels (default: 4096)")
    parser.add_argument("--index", type=int, default=0, help="which chunk to run (default: 0, the first)")
    parser.add_argument("--run-id", default=None, help="output prefix (default: BII_RUN_ID / config.RUN_ID)")
    parser.add_argument("--out", default="./data/test_chunk_out",
                        help="local output root (redirects config.OUT_ROOT off S3; default: ./data/test_chunk_out)")
    args = parser.parse_args(argv)

    # Redirect outputs to a local dir — the documented monkeypatch point for running off S3.
    config.OUT_ROOT = args.out

    result = run(tuple(args.bounds), args.chunksize, args.run_id, args.index)
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    main(sys.argv[1:])
