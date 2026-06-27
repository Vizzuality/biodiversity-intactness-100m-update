"""Source-to-COG conversion for staging.

* :func:`translate_to_cog` re-COGs a raster. Remote sources download to a temp file first.
* :func:`rasterize_to_cog` burns a *local EPSG:4326* OGR vector onto the BII grid via
  ``gdal_rasterize``, caller must stage a local EPSG:4326 copy first.
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

from . import config, io

# Cache off. Retry transient HTTP failures.
GDAL_READ_ENV = dict(
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    VSI_CACHE="FALSE",
    GDAL_HTTP_MAX_RETRY="3",
    GDAL_HTTP_RETRY_DELAY="1",
)


def footprint(uri: str, dst_crs: str) -> tuple[float, float, float, float]:
    """``uri``'s footprint reprojected to ``dst_crs`` as ``(west, south, east, north)``."""
    with rio.Env(**GDAL_READ_ENV), rio.open(uri) as s:
        return tuple(transform_bounds(s.crs, dst_crs, *s.bounds))


def fetch(url: str, headers: dict | None = None, suffix: str | None = None) -> str:
    """Download ``url`` to a temp file and return its path. Suffix defaults from the URL."""
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
    """Convert raster ``src`` (local path or ``http(s)://`` URL) into a COG at ``dst``.
    A ``.gz`` source is read through ``/vsigzip``."""
    local = fetch(src, headers) if src.startswith(("http://", "https://")) else src
    try:
        path = f"/vsigzip/{local}" if local.endswith(".gz") else local
        with rio.Env(**GDAL_READ_ENV), rio.open(path) as s, io.staged_local_path(dst) as out:
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
    """Snap ``bounds`` (EPSG:4326 w, s, e, n) outward to multiples of ``res``, returning
    ``(width, height, snapped_bounds)``. Keeps staged tiles pixel-aligned for clean mosaicking."""
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
    """Burn local OGR vector ``src`` onto the BII grid and write a COG at ``dst``. ``src`` must
    be EPSG:4326. ``layer`` selects the OGR layer; ``bounds`` is snapped to the grid for the extent. 
    Empty ``bounds`` burns an all-zero mask."""
    width, height, snapped = snap_grid(bounds)

    # all touched (-at)
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
