"""Shared stream-to-COG writer and S3 I/O for staging.

Staging modules use this to turn a remote source (read in place via GDAL ``/vsicurl`` etc.)
into a valid Cloud-Optimized GeoTIFF at a destination that is either a local path or an
``s3://`` URI. Every COG is produced by GDAL's native ``COG`` driver (GDAL >= 3.1), which
builds the tiled, overview-bearing, ZSTD-compressed output in a single pass. Two write
paths:

* :func:`translate_to_cog` — pure re-COG of an existing raster (Hansen, WorldPop, VNL,
  travel time, FML). Streams via ``rasterio.shutil.copy`` (GDAL ``CreateCopy``); no full
  read into memory.
* :func:`rasterize_to_cog` — burn an OGR vector source (SDPT planted GDB, OSM roads) onto the
  BII grid by shelling out to the ``gdal_rasterize`` CLI, which streams features in GDAL and
  never reads geometries into Python/geopandas, then stream-converts the burn to a COG.

All writers return the COG footprint as ``(west, south, east, north)`` in EPSG:4326 so the
caller can register it in the tile index.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from urllib.parse import urlparse

import numpy as np
import rasterio as rio
import rasterio.shutil
from rasterio.warp import transform_bounds

from .. import config

# GDAL options used whenever we read a remote source for staging. Keep caches off so a
# Batch worker's memory footprint stays bounded; let GDAL retry transient HTTP failures.
READ_ENV = dict(
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    VSI_CACHE="FALSE",
    GDAL_HTTP_MAX_RETRY="3",
    GDAL_HTTP_RETRY_DELAY="1",
)


# --------------------------------------------------------------------------------------
# Destination handling (local path or s3://)
# --------------------------------------------------------------------------------------
def is_s3(uri: str) -> bool:
    return str(uri).startswith("s3://")


def _split_s3(uri: str) -> tuple[str, str]:
    p = urlparse(uri)
    return p.netloc, p.path.lstrip("/")


def _s3_client():
    import boto3  # imported lazily so unit tests don't require credentials

    return boto3.client("s3", region_name=config.AWS_REGION)


def exists(uri: str) -> bool:
    """Whether a destination already exists (skip-if-exists idiom from ``download.py``)."""
    if is_s3(uri):
        import botocore

        bucket, key = _split_s3(uri)
        try:
            _s3_client().head_object(Bucket=bucket, Key=key)
            return True
        except botocore.exceptions.ClientError:
            return False
    return os.path.exists(uri)


def upload(local_path: str, uri: str) -> None:
    """Put a local file at ``uri`` (s3 upload or local copy/move)."""
    if is_s3(uri):
        bucket, key = _split_s3(uri)
        _s3_client().upload_file(local_path, bucket, key)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(uri)), exist_ok=True)
        if os.path.abspath(local_path) != os.path.abspath(uri):
            shutil.copyfile(local_path, uri)


@contextmanager
def _staged_local_path(dst: str):
    """Yield a local path to write the COG to, then publish it to ``dst``.

    For local destinations we write straight to the final path (via a sibling temp file
    so a crash never leaves a half-written COG). For S3 we write to a temp file and upload.
    """
    if is_s3(dst):
        tmp = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
        tmp.close()
        try:
            yield tmp.name
            upload(tmp.name, dst)
        finally:
            os.path.exists(tmp.name) and os.remove(tmp.name)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
        tmp = dst + ".tmp"
        try:
            yield tmp
            os.replace(tmp, dst)
        finally:
            os.path.exists(tmp) and os.remove(tmp)


# --------------------------------------------------------------------------------------
# Footprint helpers
# --------------------------------------------------------------------------------------
def footprint_4326(path_or_uri: str) -> tuple[float, float, float, float]:
    """Return a raster's bounds as ``(west, south, east, north)`` in EPSG:4326."""
    src_path = _vsi(path_or_uri)
    with rio.Env(**READ_ENV), rio.open(src_path) as src:
        return tuple(transform_bounds(src.crs, "EPSG:4326", *src.bounds))  # type: ignore[return-value]


def _vsi(uri: str) -> str:
    """Map an http(s) URL to a GDAL ``/vsicurl/`` path; pass other paths through."""
    if uri.startswith(("http://", "https://")):
        return "/vsicurl/" + uri
    return uri


# --------------------------------------------------------------------------------------
# COG creation options (GDAL "COG" driver)
# --------------------------------------------------------------------------------------
def _cog_options(resampling: str, dtype: str) -> dict:
    """Creation options for the GDAL ``COG`` driver: ZSTD compression with a dtype-correct
    predictor, 512px tiles, and overviews built with ``resampling`` (``average`` for
    continuous data, ``nearest`` for categorical).

    The predictor decorrelates neighbouring samples before compression: 3 (floating-point)
    for float bands, 2 (horizontal) for integers. Picking it by dtype matters — predictor 2
    on raw float bytes can *inflate* the output, so the COG driver's dtype-blind ``YES`` is
    not safe here.
    """
    predictor = 3 if str(dtype).startswith(("float", "complex")) else 2
    return dict(
        driver="COG",
        compress="ZSTD",
        predictor=predictor,
        blocksize=512,
        overview_resampling=resampling,
    )


# --------------------------------------------------------------------------------------
# Writers
# --------------------------------------------------------------------------------------
def fetch(url: str, headers: dict | None = None, suffix: str = ".tif") -> str:
    """Download ``url`` to a temp file and return its path (for servers without HTTP range
    support, where ``/vsicurl`` can't do the random reads a COG translate needs)."""
    import requests

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
    overwrite: bool = False,
    extra_env: dict | None = None,
    download: bool = False,
) -> tuple[float, float, float, float]:
    """Stream-convert an existing raster ``src`` into a COG at ``dst``.

    ``src`` may be a local path, an ``http(s)://`` URL (read via ``/vsicurl``), or any GDAL
    VSI path (``/vsigzip/...``, ``/vsizip/...``). Set ``download=True`` to fetch the source to
    a temp file first (for hosts that don't support HTTP range requests). Returns the COG
    footprint in EPSG:4326.
    """
    if not overwrite and exists(dst):
        return footprint_4326(dst)

    fetched = None
    if download and src.startswith(("http://", "https://")):
        fetched = fetch(src)
        src_path = fetched
    else:
        src_path = _vsi(src)
    env = dict(READ_ENV)
    if extra_env:
        env.update(extra_env)

    try:
        with rio.Env(**env), rio.open(src_path) as s, _staged_local_path(dst) as local:
            rio.shutil.copy(s, local, **_cog_options(resampling, s.dtypes[0]))
    finally:
        if fetched:
            os.path.exists(fetched) and os.remove(fetched)
    return footprint_4326(dst)


def grid_transform(
    bounds: tuple[float, float, float, float], res: float | None = None
) -> tuple[object, int, int, tuple[float, float, float, float]]:
    """Snap ``bounds`` (EPSG:4326 west, south, east, north) outward to the global BII grid and
    return ``(transform, width, height, snapped_bounds)``.

    Snapping to multiples of ``res`` keeps every staged tile pixel-aligned with the processing
    grid and with neighbouring tiles, so cog_worker can mosaic overlapping tiles cleanly.
    """
    from affine import Affine

    res = res or config.SCALE_DEG
    w, s, e, n = bounds
    w = np.floor(w / res) * res
    s = np.floor(s / res) * res
    e = np.ceil(e / res) * res
    n = np.ceil(n / res) * res
    width = max(1, int(round((e - w) / res)))
    height = max(1, int(round((n - s) / res)))
    transform = Affine.translation(w, n) * Affine.scale(res, -res)
    return transform, width, height, (w, s, e, n)


# GDAL output-type names for ``gdal_rasterize -ot``, keyed by numpy dtype string.
_GDAL_OT = {
    "uint8": "Byte", "int8": "Int8", "uint16": "UInt16", "int16": "Int16",
    "uint32": "UInt32", "int32": "Int32", "float32": "Float32", "float64": "Float64",
}


def _require_gdal_rasterize() -> None:
    if shutil.which("gdal_rasterize") is None:
        raise RuntimeError(
            "vector rasterization requires the gdal_rasterize CLI but it is not on PATH; "
            "install GDAL (the gdal-bin package)."
        )


def _raster_is_empty(path: str, fill: int) -> bool:
    """Whether every pixel of ``path`` equals ``fill``. Reads block-by-block so a huge burn
    never lands in memory at once, and short-circuits on the first non-fill pixel."""
    with rio.Env(**READ_ENV), rio.open(path) as s:
        for _, win in s.block_windows(1):
            if (s.read(1, window=win) != fill).any():
                return False
    return True


def rasterize_to_cog(
    src: str,
    dst: str,
    bounds: tuple[float, float, float, float] | None = None,
    *,
    layer: str | None = None,
    where: str | None = None,
    res: float | None = None,
    burn: int = 1,
    fill: int = 0,
    dtype: str = "uint8",
    all_touched: bool = True,
    nodata: float | None = None,
    overwrite: bool = False,
    skip_empty: bool = True,
    extra_env: dict | None = None,
) -> tuple[float, float, float, float] | None:
    """Burn an OGR vector ``src`` onto the BII grid with ``gdal_rasterize`` and write a COG at ``dst``.

    ``src`` is any OGR-readable path — a local file, a filtered ``.osm.pbf`` (``layer="lines"``),
    or a file geodatabase over ``/vsizip//vsicurl`` (``layer=<region>``). ``gdal_rasterize``
    streams features inside GDAL and burns them to a temp GeoTIFF; geometries are never read into
    Python/geopandas. The burn is then stream-converted to a COG (``nearest`` overviews — the
    output is a categorical mask).

    ``bounds`` (EPSG:4326 west, south, east, north) is snapped outward to the BII grid for the
    output extent; pass ``None`` to use the source layer's full extent (read from metadata only,
    no geometry load). ``all_touched=True`` keeps thin features (single-pixel roads) from
    dropping out. ``where`` is an OGR attribute filter applied during the burn. Returns the
    snapped footprint, or ``None`` if the burn is empty (and ``skip_empty``) — keeping the index
    lean.

    The source must already be in EPSG:4326 (the BII grid CRS) — ``gdal_rasterize`` burns vector
    coordinates onto the ``-te`` grid as-is and does not reproject. Both callers satisfy this:
    OSM is WGS84 and the SDPT GDB layers are EPSG:4326.
    """
    if not overwrite and exists(dst):
        return footprint_4326(dst)
    _require_gdal_rasterize()
    env = dict(READ_ENV, **(extra_env or {}))

    if bounds is None:
        import pyogrio

        with rio.Env(**env):
            info = pyogrio.read_info(src, layer=layer)
        if not info.get("features") or info.get("total_bounds") is None:
            return None  # empty layer -> nothing to burn
        bounds = tuple(info["total_bounds"])

    _, width, height, snapped = grid_transform(bounds, res)
    w, s, e, n = (float(x) for x in snapped)

    cmd = ["gdal_rasterize"]
    for k, v in env.items():
        cmd += ["--config", k, str(v)]
    cmd += [
        "-burn", str(burn), "-init", str(fill),
        "-ot", _GDAL_OT.get(dtype, "Byte"), "-of", "GTiff",
        "-te", repr(w), repr(s), repr(e), repr(n), "-ts", str(width), str(height),
        "-co", "TILED=YES", "-co", "BLOCKXSIZE=512", "-co", "BLOCKYSIZE=512",
    ]
    if all_touched:
        cmd.append("-at")
    if layer:
        cmd += ["-l", layer]
    if where:
        cmd += ["-where", where]
    if nodata is not None:
        cmd += ["-a_nodata", str(nodata)]

    tmpdir = tempfile.mkdtemp(prefix="bii_rasterize_")
    tmp_tif = os.path.join(tmpdir, "burn.tif")
    cmd += [src, tmp_tif]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"gdal_rasterize failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        if skip_empty and _raster_is_empty(tmp_tif, fill):
            return None
        return translate_to_cog(
            tmp_tif, dst, resampling="nearest", overwrite=overwrite, extra_env=extra_env
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
