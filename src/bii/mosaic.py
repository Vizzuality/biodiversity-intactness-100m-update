"""Mosaic a run's per-chunk tile COGs into one monolithic COG per year.

Tiles already share one grid, so assembly needs no resampling: ``gdalbuildvrt`` stitches
them by extent (carrying each tile's mask band through), and ``gdal_translate`` re-encodes
the result as a single COG with average overviews. Written beside, not inside, the tile tree
(``<run_id>_mosaic/``) so ``generate_catalog_mosaic.py``'s recursive ``*.tif`` scan doesn't
pick it up as a chunk.
"""

from __future__ import annotations

import os
import subprocess

from . import cog, config, io, orchestration

# GDAL_MAX_DATASET_POOL_SIZE keeps more source tiles open at once (default 100, ~750/year) —
# the read-side bottleneck when the VRT stitches the /vsis3 tiles.
GDAL_ENV = dict(cog.GDAL_READ_ENV, VSI_CACHE="TRUE", GDAL_CACHEMAX="4096",
                GDAL_HTTP_MULTIPLEX="YES", GDAL_MAX_DATASET_POOL_SIZE="500")


def _vsi(uri: str) -> str:
    """``s3://bucket/key`` -> ``/vsis3/bucket/key``: the GDAL CLI tools don't accept ``s3://``."""
    return uri.replace("s3://", "/vsis3/", 1) if uri.startswith("s3://") else uri


def tile_uris(run_id: str, year: int) -> list[str]:
    prefix = config.out_uri(run_id, f"bii_{year}") + "/"
    return sorted(u for u in io.list_uris(prefix) if u.endswith(".tif"))


def mosaic_uri(run_id: str, year: int) -> str:
    return config.out_uri(f"{run_id}_mosaic", f"bii_{year}.tif")


def build_mosaic(year: int, run_id: str | None = None) -> str:
    """Assemble ``run_id``'s ``bii_<year>`` tiles into one COG; return its uri."""
    run_id = run_id or config.RUN_ID
    tiles = tile_uris(run_id, year)
    if not tiles:
        raise FileNotFoundError(f"no tiles for {run_id} bii_{year}")
    dst = mosaic_uri(run_id, year)
    env = {**os.environ, **GDAL_ENV}
    with io.staged_local_path(dst) as out:
        vrt = f"{out}.vrt"
        subprocess.run(["gdalbuildvrt", vrt, *(_vsi(t) for t in tiles)], check=True, env=env)
        subprocess.run([
            "gdal_translate", vrt, out, "-of", "COG",
            "-co", "COMPRESS=ZSTD", "-co", "PREDICTOR=3", "-co", "BLOCKSIZE=512",
            "-co", "BIGTIFF=YES", "-co", "NUM_THREADS=ALL_CPUS",
            "-co", "OVERVIEW_RESAMPLING=AVERAGE",
        ], check=True, env=env)
    return dst


def mosaic(year: int | None = None, run_id: str | None = None) -> None:
    """Batch worker entrypoint: mosaics one year, read from the manifest when not given."""
    year = year if year is not None else orchestration.manifest_line()["year"]
    build_mosaic(year, run_id)


def run(years: list[int] | None = None, *, run_id: str | None = None,
        overwrite: bool = False) -> dict:
    """Run one Batch mosaic job per missing year; return the years that failed. A failed year
    leaves no output, so re-running resumes it."""
    run_id = run_id or config.RUN_ID
    years = years or config.years()
    pending = years if overwrite else [y for y in years if not io.exists(mosaic_uri(run_id, y))]
    if not pending:
        return {"run_id": run_id, "years": years, "pending": [], "failed": []}
    failed = orchestration.run_manifest(
        [{"year": y} for y in pending], ["bii-mosaic"], executor="batch",
        manifest_uri=config.out_uri(f"{run_id}_mosaic", "years.jsonl"),
        job_name=f"bii-mosaic-{run_id}", env={"BII_RUN_ID": run_id},
        label=lambda c: f"year {c['year']}")
    return {"run_id": run_id, "pending": pending, "failed": failed}
