#!/usr/bin/env python
"""Build monolithic per-year BII COGs from the per-chunk tile outputs.

    python scripts/mosaic.py                         # mosaic every missing year on Batch
    python scripts/mosaic.py --overwrite             # rebuild years even if output exists
"""
from __future__ import annotations

import argparse
import json
import sys

from bii import mosaic


def main(argv=None) -> dict:
    parser = argparse.ArgumentParser(description="Mosaic per-year BII tiles into monolithic COGs.")
    parser.add_argument("--run-id", default=None, help="output prefix (default: BII_RUN_ID / config.RUN_ID)")
    parser.add_argument("--overwrite", action="store_true", help="rebuild years whose output already exists")
    args = parser.parse_args(argv)

    result = mosaic.run(run_id=args.run_id, overwrite=args.overwrite)
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    main(sys.argv[1:])
