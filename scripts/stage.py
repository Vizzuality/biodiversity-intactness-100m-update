#!/usr/bin/env python
"""Thin wrapper around the staging CLI (also installed as the ``bii-stage`` console script).

    python scripts/stage.py --dataset roads --executor docker      # test the images locally
    python scripts/stage.py --executor batch                       # fan out on AWS Batch
"""
import sys

from bii.stage import main

if __name__ == "__main__":
    main(sys.argv[1:])  # prints JSON; returns a dict, so don't pass it to SystemExit (exits 1)
