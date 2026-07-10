#!/usr/bin/env python
"""Build monolithic per-year BII COGs from the per-chunk tile outputs.

    python scripts/mosaic.py --year 2020 --year 2021
    python scripts/mosaic.py --executor docker      # local test run
    python scripts/mosaic.py --no-submit             # write the manifest only
"""
from __future__ import annotations

import argparse
import json
import sys

from bii import mosaic


def main(argv=None) -> dict:
    parser = argparse.ArgumentParser(description="Mosaic per-year BII tiles into monolithic COGs.")
    parser.add_argument("--run-id", default=None, help="output prefix (default: BII_RUN_ID / config.RUN_ID)")
    parser.add_argument("--year", type=int, action="append", help="limit to this year (repeatable; default: all)")
    parser.add_argument("--executor", choices=("docker", "batch"), default="batch",
                        help="where to run jobs (default: batch on AWS)")
    parser.add_argument("--no-submit", action="store_true", help="write the manifest only; don't run it")
    args = parser.parse_args(argv)

    result = mosaic.run(args.year, run_id=args.run_id, executor=args.executor, submit=not args.no_submit)
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    main(sys.argv[1:])
