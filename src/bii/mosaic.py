"""Mosaic a run's per-chunk tile COGs into one monolithic COG per year.

Tiles already share one grid, so assembly needs no resampling. Building the COG straight from the
``/vsis3`` VRT with ``OVERVIEWS=IGNORE_EXISTING`` makes the COG driver read the remote source 2-3
times -- a mask-overview pass, an imagery-overview pass, and the base CreateCopy all read
``poCurDS`` (the VRT) at full resolution (see ``cogdriver.cpp``). So we first fetch every tile to
local disk, then run the COG conversion against a VRT over those local files: the repeated reads
hit local disk instead of S3, so no separate local-base pass is needed. Written beside, not inside,
the tile tree (``<run_id>_mosaic/``) so ``generate_catalog_mosaic.py``'s recursive ``*.tif`` scan
doesn't pick it up as a chunk.
"""

from __future__ import annotations

import os
import subprocess
import tempfile

from . import cog, config, io, orchestration

# Tiles are read from local disk now, so only the block cache needs tuning; 4 GB stays well under
# the 15 GB container (500 OOM'd).
GDAL_ENV = dict(cog.GDAL_READ_ENV, GDAL_CACHEMAX="4096")


ENC = ["-co", "COMPRESS=ZSTD", "-co", "PREDICTOR=3", "-co", "BIGTIFF=YES",
       "-co", "NUM_THREADS=ALL_CPUS"]


def tile_uris(run_id: str, year: int) -> list[str]:
    prefix = config.out_uri(run_id, f"bii_{year}") + "/"
    return sorted(u for u in io.list_uris(prefix) if u.endswith(".tif"))


def mosaic_uri(run_id: str, year: int) -> str:
    return config.out_uri(f"{run_id}_mosaic", f"bii_{year}.tif")


def build_mosaic(year: int, run_id: str | None = None) -> str:
    """Assemble ``run_id``'s ``bii_<year>`` tiles into one COG; return its uri. Every tile is
    fetched to local disk first, then the COG conversion (which cascade-builds the overviews) runs
    against a VRT over those local files -- identical output to converting the remote tiles
    directly, without the COG driver's repeated remote reads."""
    run_id = run_id or config.RUN_ID
    tiles = tile_uris(run_id, year)
    if not tiles:
        raise FileNotFoundError(f"no tiles for {run_id} bii_{year}")
    dst = mosaic_uri(run_id, year)
    env = {**os.environ, **GDAL_ENV}
    with io.staged_local_path(dst) as out, tempfile.TemporaryDirectory() as tiledir:
        local = []
        for i, t in enumerate(tiles):
            p = os.path.join(tiledir, f"{i}.tif")
            io.download(t, p)
            local.append(p)
        vrt = f"{out}.vrt"
        subprocess.run(["gdalbuildvrt", vrt, *local], check=True, env=env)
        subprocess.run(["gdal_translate", vrt, out, "-of", "COG", *ENC, "-co", "BLOCKSIZE=512",
                        "-co", "OVERVIEW_RESAMPLING=AVERAGE", "-co", "OVERVIEWS=IGNORE_EXISTING"],
                       check=True, env=env)
        os.remove(vrt)
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
