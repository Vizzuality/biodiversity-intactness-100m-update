"""Central configuration: S3 layout, BII target grid, and year range.

Everything that is "where does data live" or "what grid do we compute on" lives here so the
staging modules, tile index, and (later) the processing worker share one source of truth.

Only constants shared across datasets belong here; a constant used by a single dataset lives in
that dataset's module (``bii.staging.*``, or ``bii.tile_index`` for landcover/LULC).
"""

from __future__ import annotations

import os

# --------------------------------------------------------------------------------------
# S3 layout
# --------------------------------------------------------------------------------------
# Outputs + manifests (kept forever); regenerable staged inputs/index (auto-deleted) live separately.
BUCKET = "vizz-bii"
PROCESSING_BUCKET = "vizz-bii-processing"

# Key prefixes within the buckets.
STAGED_PREFIX = "input_cogs"
OUT_PREFIX = "out"


# Roots for staged inputs and outputs. Default to S3; set ``BII_STAGED_ROOT`` / ``BII_OUT_ROOT``
# (or assign the attribute) to a local directory to run staging/processing on local disk — env so
# a docker/Batch container picks up the same root the host built the manifest with (the index-part
# paths are derived from ``STAGED_ROOT`` at runtime inside the container).
STAGED_ROOT = os.environ.get("BII_STAGED_ROOT", f"s3://{PROCESSING_BUCKET}/{STAGED_PREFIX}")
OUT_ROOT = os.environ.get("BII_OUT_ROOT", f"s3://{BUCKET}/{OUT_PREFIX}")

# Run id: the sub-prefix under the output root segregating one processing run's COGs
# (``out/<run_id>/...``) and its ``chunks.jsonl`` manifest. Operational, not analysis config —
# override per run via the ``BII_RUN_ID`` env var.
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


# --------------------------------------------------------------------------------------
# BII target grid (default, ported from the original notebooks)
# --------------------------------------------------------------------------------------
DEG2METERS = 111319.49079327357

PROJ = "EPSG:4326"
SCALE_METERS = 100.0
SCALE_DEG = SCALE_METERS / DEG2METERS  # ~0.000898 deg ~= 100 m at the equator
# 100 px buffer makes the focal (uniform_filter) and distance-transform ops safe at chunk
# edges: 10 km / 100 m. distRoads is clipped to 10 km for the same reason.
BUFFER = round(10000 / SCALE_METERS)

# --------------------------------------------------------------------------------------
# Year range. LULC + WorldPop + nightlights are per-year; the rest are single-epoch (one
# snapshot reused across all years).
# --------------------------------------------------------------------------------------
START_YEAR = int(os.environ.get("BII_START_YEAR", 2017))
END_YEAR = int(os.environ.get("BII_END_YEAR", 2024))


def years() -> list[int]:
    return list(range(START_YEAR, END_YEAR + 1))
