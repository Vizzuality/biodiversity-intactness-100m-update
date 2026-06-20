#!/usr/bin/env python
"""Single-chunk local docker test — the gate before any Batch fan-out (architecture plan step 5).

Builds a ``cog_worker`` ``Manager`` over the AOI, takes its first chunk, and runs ``bii-process``
on that chunk inside the production ``bii`` image — the *identical* code path a Batch array index
runs. Inputs are read from the locally staged store by default (``--staged``, bind-mounted into the
container, mirroring ``stage_local.py``), or from the remote S3 store with ``--remote``.

    python scripts/test_chunk.py                          # central Spain, 2020, ./data/staged_local
    python scripts/test_chunk.py --remote                 # read staged inputs from S3 instead
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from cog_worker import Manager, Worker

from bii import config, process, s3io, stage

CHUNKSIZE = 4096


def main(argv=None) -> dict:
    p = argparse.ArgumentParser(description="Run ONE BII chunk in local docker (pre-fan-out gate).")
    p.add_argument("--bounds", type=float, nargs=4, metavar=("W", "S", "E", "N"),
                   default=[-5.0, 39.0, -1.32, 42.68],
                   help="analysis extent in EPSG:4326 (default: ~4096px central Spain)")
    p.add_argument("--year", type=int, default=2020, help="year to process")
    p.add_argument("--staged", default="./data/staged_local",
                   help="local staged root (bind-mounted into the container)")
    p.add_argument("--remote", action="store_true",
                   help="read staged inputs from the S3 store instead of the local --staged dir")
    args = p.parse_args(argv)

    config.START_YEAR = config.END_YEAR = args.year
    run_id = config.RUN_ID
    store = None
    if not args.remote:
        store = os.path.abspath(args.staged)
        config.STAGED_ROOT = config.OUT_ROOT = store

    manager = Manager(bounds=tuple(args.bounds), scale=config.SCALE_DEG, proj=config.PROJ, buffer=config.BUFFER)
    params = list(manager.chunk_params(CHUNKSIZE))
    chunk = json.loads(json.dumps(dict(params[0], proj_bounds=list(params[0]["proj_bounds"]))))
    worker = Worker(**chunk)
    print(f"chunk: bounds={tuple(worker.bounds)} size={worker.width}x{worker.height}", file=sys.stderr)

    muri = config.out_uri(run_id, "chunks.jsonl")
    s3io.put_bytes((json.dumps(chunk) + "\n").encode(), muri)
    stage.docker_run("bii", ["bii-process"], store=store, env={
        "BII_CHUNKS_URI": muri, "BII_RUN_ID": run_id,
        "BII_START_YEAR": config.START_YEAR, "BII_END_YEAR": config.END_YEAR,
        "AWS_BATCH_JOB_ARRAY_INDEX": 0,
    })

    outputs = [process.output_uri(run_id, layer, worker) for layer in process.output_layers()]
    result = {"run_id": run_id, "bounds": list(worker.bounds), "outputs": outputs}
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    main(sys.argv[1:])
