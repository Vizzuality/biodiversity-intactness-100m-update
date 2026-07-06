"""Central configuration: S3 layout, BII target grid, and year range.

Only constants shared across datasets belong here; per-dataset constants live in that dataset's
module (``bii.staging.*``, or ``bii.tile_index`` for landcover/LULC).
"""

from __future__ import annotations

import os

BUCKET = "vizz-bii"
PROCESSING_BUCKET = "vizz-bii-processing"

# Where to save pre-process COGs
STAGED_PREFIX = "input_cogs"
STAGED_ROOT = os.environ.get("BII_STAGED_ROOT", f"s3://{PROCESSING_BUCKET}/{STAGED_PREFIX}")

# Where to save final outputs (per-run subdirs)
OUT_PREFIX = "out"
OUT_ROOT = os.environ.get("BII_OUT_ROOT", f"s3://{BUCKET}/{OUT_PREFIX}")
RUN_ID = os.environ.get("BII_RUN_ID", "v1_1")

def _join(root: str, parts: tuple[str, ...]) -> str:
    key = "/".join(str(p).strip("/") for p in parts if p is not None and p != "")
    return f"{root.rstrip('/')}/{key}" if key else root.rstrip("/")


def staged_uri(*parts: str) -> str:
    """Build a URI under the staged root (s3 or local), e.g. ``staged_uri("forestLoss", x)``."""
    return _join(STAGED_ROOT, parts)


def out_uri(*parts: str) -> str:
    """Build a URI under the output root (s3 or local)."""
    return _join(OUT_ROOT, parts)


DEG2METERS = 111319.49079327357

PROJ = "EPSG:4326"
SCALE_METERS = 100.0
SCALE_DEG = SCALE_METERS / DEG2METERS  # deg/px (~100 m at equator)
# 10 km / 100 m: buffer keeps focal + distance-transform ops correct at chunk edges
# (distRoads also clipped to 10 km).
BUFFER = round(10000 / SCALE_METERS)

# LULC + WorldPop + nightlights are per-year; the rest single-epoch.
START_YEAR = int(os.environ.get("BII_START_YEAR", 2017))
END_YEAR = int(os.environ.get("BII_END_YEAR", 2025))


def years() -> list[int]:
    return list(range(START_YEAR, END_YEAR + 1))
