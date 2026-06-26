#!/usr/bin/env python
"""Stage BII input datasets and consolidate their indexes.

Batch queue/job-def and the docker image name come from the environment (``BII_BATCH_*`` /
``BII_STAGE_IMAGE``), like the rest of the pipeline.

    python scripts/stage.py --dataset roads --executor docker     # test the images locally
    python scripts/stage.py --executor batch                      # fan out on AWS Batch
"""
from __future__ import annotations

import argparse
import json
import sys

from bii import stage
from bii.staging import MODULES


def main(argv=None) -> dict:
    parser = argparse.ArgumentParser(description="Stage BII input datasets and consolidate their indexes.")
    parser.add_argument("--dataset", choices=sorted(MODULES), help="stage only this dataset (default: all)")
    parser.add_argument("--year", type=int, help="stage only this year (per-year datasets only)")
    parser.add_argument("--executor", choices=("docker", "batch"), default="docker",
                        help="where to run units (default: docker locally)")
    parser.add_argument("--overwrite", action="store_true", help="restage units whose output already exists")
    parser.add_argument("--dry-run", action="store_true", help="list the planned units + existence and exit")
    args = parser.parse_args(argv)

    if args.dry_run:
        items = stage.manifest_items(args.dataset, args.year)
        have = stage.staged_dsts(items)
        units = [{"dataset": it["dataset"], "id": it["unit"]["id"], "dst": it["unit"]["dst"],
                  "exists": it["unit"]["dst"] in have}
                 for it in items]
        result = {"planned": len(units), "exists": sum(u["exists"] for u in units), "units": units}
    else:
        result = stage.run(args.dataset, args.year, executor=args.executor, overwrite=args.overwrite)
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    main(sys.argv[1:])
