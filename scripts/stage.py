#!/usr/bin/env python
"""Thin wrapper around the staging CLI (also installed as the ``bii-stage`` console script).

    python scripts/stage.py hansen run --id 10N_090W --local /tmp/bii_staged
"""
import sys

from bii.cli import stage_main

if __name__ == "__main__":
    raise SystemExit(stage_main(sys.argv[1:]))
