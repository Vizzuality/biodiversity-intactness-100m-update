#!/usr/bin/env python
"""Build, run, and retry a BII processing run (manifest -> docker/Batch array -> retry-failed).

Batch queue/job-def come from the environment (``BII_BATCH_QUEUE`` / ``BII_BATCH_JOB_DEF``).

    python scripts/run.py --bounds -86 9 -84 11                       # fan out + retry on Batch
    python scripts/run.py --bounds -86 9 -84 11 --no-submit           # write manifest only (size gate)
    python scripts/run.py --bounds -86 9 -84 11 --executor docker --staged ./data/staged_local
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from bii import config, process
from cog_worker import Manager


def main(argv=None) -> dict:
    parser = argparse.ArgumentParser(description="Build, run, and retry a BII processing run.")
    parser.add_argument("--run-id", default=None, help="output prefix (default: BII_RUN_ID / config.RUN_ID)")
    parser.add_argument("--bounds", type=float, nargs=4, metavar=("W", "S", "E", "N"),
                        default=[-180.0, -85.0, 180.0, 85.0], help="analysis extent in EPSG:4326")
    parser.add_argument("--executor", choices=("docker", "batch"), default="batch",
                        help="where to run chunks (default: batch on AWS)")
    parser.add_argument("--staged", default=None,
                        help="local staged/output root for --executor docker (bind-mounted into the container)")
    parser.add_argument("--overwrite", action="store_true", help="reprocess chunks whose outputs already exist")
    parser.add_argument("--no-submit", action="store_true", help="write the manifest only; don't run it")
    args = parser.parse_args(argv)

    store = os.path.abspath(args.staged) if args.staged else None
    if store:  # set host-side so chunk_manifest reads the local coverage index
        config.STAGED_ROOT = config.OUT_ROOT = store

    manager = Manager(bounds=tuple(args.bounds), scale=config.SCALE_DEG, proj=config.PROJ, buffer=config.BUFFER)
    result = process.run(manager, run_id=args.run_id, executor=args.executor, store=store,
                         overwrite=args.overwrite, submit=not args.no_submit)
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    main(sys.argv[1:])
