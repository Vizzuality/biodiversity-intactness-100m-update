"""Source-to-COG conversion for staging.

Two cases, both producing a Cloud-Optimized GeoTIFF via GDAL's native ``COG`` driver:

* :func:`translate_to_cog` — re-COG an existing raster (Hansen, WorldPop, VNL, travel time,
  FML). A remote source is downloaded to a temp file first, then converted locally: the COG
  driver re-reads the source to build overviews, so a local file beats repeated remote range
  reads with no memory cost.
* :func:`rasterize_to_cog` — burn a *local EPSG:4326* OGR vector source onto the BII grid with
  ``gdal_rasterize`` (OSM roads, SDPT planted GDB). ``gdal_rasterize`` burns coordinates onto the
  degree grid as-is and never reprojects, so a remote or non-EPSG:4326 source must be staged to a
  local EPSG:4326 copy by the caller first (see :func:`bii.staging.sdpt._localized`). Geometries
  never enter Python.

Both writers just produce the COG; the footprint index is rebuilt from the written COGs' headers
afterwards (:func:`bii.tile_index.index_cogs`). Destination handling (local path vs ``s3://``)
lives in :mod:`bii.s3io`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from urllib.parse import urlparse

import numpy as np
import rasterio as rio
import rasterio.shutil
import requests
from rasterio.warp import transform_bounds

from . import config, s3io

# GDAL options for reading sources during staging: caches off so a Batch worker's memory stays
# bounded; retry transient HTTP failures. Also reused by bii.process for the compute reads.
GDAL_READ_ENV = dict(
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    VSI_CACHE="FALSE",
    GDAL_HTTP_MAX_RETRY="3",
    GDAL_HTTP_RETRY_DELAY="1",
)


def footprint(uri: str, dst_crs: str) -> tuple[float, float, float, float]:
    """Read ``uri``'s raster footprint from its header, reprojected to ``dst_crs``
    ``(west, south, east, north)`` — how :func:`bii.tile_index.index_cogs` builds the index."""
    with rio.Env(**GDAL_READ_ENV), rio.open(uri) as s:
        return tuple(transform_bounds(s.crs, dst_crs, *s.bounds))


def fetch(url: str, headers: dict | None = None, suffix: str | None = None) -> str:
    """Download ``url`` to a temp file and return its path. The suffix is taken from the URL
    unless given (roads passes ``.osm.pbf``); ``headers`` carry e.g. an auth bearer token."""
    suffix = suffix or os.path.splitext(urlparse(url).path)[1] or ".tmp"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        with requests.get(url, headers=headers or {}, stream=True, timeout=300) as r:
            r.raise_for_status()
            for chunk in r.iter_content(1 << 20):
                tmp.write(chunk)
        tmp.close()
        return tmp.name
    except BaseException:
        tmp.close()
        os.path.exists(tmp.name) and os.remove(tmp.name)
        raise


def translate_to_cog(
    src: str,
    dst: str,
    *,
    resampling: str = "average",
    headers: dict | None = None,
) -> None:
    """Convert raster ``src`` into a COG at ``dst`` (overwriting).

    ``src`` is a local path or an ``http(s)://`` URL; URLs are downloaded to a temp file first.
    A ``.gz`` source is read through ``/vsigzip``. ``headers`` are sent with the download.
    """
    local = fetch(src, headers) if src.startswith(("http://", "https://")) else src
    try:
        path = f"/vsigzip/{local}" if local.endswith(".gz") else local
        with rio.Env(**GDAL_READ_ENV), rio.open(path) as s, s3io.staged_local_path(dst) as out:
            # Predictor decorrelates neighbouring samples before ZSTD: 3 (floating-point) for
            # float bands, 2 (horizontal) for ints. Predictor 2 on raw float bytes can *inflate*
            # the output, so the COG driver's dtype-blind ``YES`` is not safe here.
            predictor = 3 if s.dtypes[0].startswith("f") else 2
            rio.shutil.copy(
                s, out, driver="COG", compress="ZSTD", predictor=predictor,
                blocksize=512, overview_resampling=resampling,
            )
    finally:
        if local is not src:
            os.path.exists(local) and os.remove(local)


def snap_grid(
    bounds: tuple[float, float, float, float], res: float | None = None
) -> tuple[int, int, tuple[float, float, float, float]]:
    """Snap ``bounds`` (EPSG:4326 west, south, east, north) outward to the global BII grid and
    return ``(width, height, snapped_bounds)``.

    Snapping to multiples of ``res`` keeps every staged tile pixel-aligned with the processing
    grid and with neighbouring tiles, so cog_worker can mosaic overlapping tiles cleanly.
    """
    res = res or config.SCALE_DEG
    w, s, e, n = bounds
    w = np.floor(w / res) * res
    s = np.floor(s / res) * res
    e = np.ceil(e / res) * res
    n = np.ceil(n / res) * res
    width = max(1, int(round((e - w) / res)))
    height = max(1, int(round((n - s) / res)))
    return width, height, (w, s, e, n)


def rasterize_to_cog(
    src: str,
    dst: str,
    bounds: tuple[float, float, float, float],
    layer: str | None = None,
) -> None:
    """Burn a *local EPSG:4326* OGR vector ``src`` onto the BII grid with ``gdal_rasterize`` and
    write a COG at ``dst`` (overwriting).

    ``src`` must already be in EPSG:4326 — ``gdal_rasterize`` burns coordinates onto the degree
    grid as-is and never reprojects, so a remote or reprojected source must be staged to a local
    EPSG:4326 copy by the caller first (see :func:`bii.staging.sdpt._localized`). ``layer`` selects
    the OGR layer (e.g. ``"lines"`` for an ``.osm.pbf``). ``bounds`` (EPSG:4326) is snapped outward
    to the grid for the output extent. A source with no features in ``bounds`` burns to an all-zero
    mask (the caller decides whether such a tile is worth keeping).
    """
    width, height, snapped = snap_grid(bounds)

    # Burn 1 where a feature touches a pixel (``-at`` keeps thin features like single-pixel roads
    # from dropping out), else 0; then COG the burn (``nearest`` overviews — a categorical mask).
    w, s, e, n = (float(x) for x in snapped)
    cmd = ["gdal_rasterize", "-burn", "1", "-init", "0", "-ot", "Byte", "-of", "GTiff", "-at",
           "-te", repr(w), repr(s), repr(e), repr(n), "-ts", str(width), str(height),
           "-co", "TILED=YES", "-co", "BLOCKXSIZE=512", "-co", "BLOCKYSIZE=512"]
    if layer:
        cmd += ["-l", layer]
    tmpdir = tempfile.mkdtemp(prefix="bii_rasterize_")
    try:
        tmp_tif = os.path.join(tmpdir, "burn.tif")
        cmd += [src, tmp_tif]
        subprocess.run(cmd, check=True)
        translate_to_cog(tmp_tif, dst, resampling="nearest")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
