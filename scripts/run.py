#!/usr/bin/env python
"""Thin wrapper around the orchestrator CLI (build manifest -> submit Batch array -> verify/retry).

    python scripts/run.py --bounds -86 9 -84 11 --chunksize 4096          # submit + verify a run
    python scripts/run.py --bounds -86 9 -84 11 --no-submit               # write manifest only
"""
import sys

from bii.orchestrate import main

if __name__ == "__main__":
    main(sys.argv[1:])
