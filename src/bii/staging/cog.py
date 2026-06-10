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
  never reads geometries into Python/geopandas, then stream-converts the burn to a COG. A
  remote or non-EPSG:4326 source is first staged once to a local EPSG:4326 copy with
  ``ogr2ogr`` (see :func:`_localize_layer`), since ``gdal_rasterize`` re-reads the whole layer
  and won't reproject.

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
) -> tuple[float, float, float, float]:
    """Stream-convert an existing raster ``src`` into a COG at ``dst``.

    ``src`` may be a local path, an ``http(s)://`` URL (read via ``/vsicurl``), or any GDAL
    VSI path (``/vsigzip/...``, ``/vsizip/...``). For hosts that don't support HTTP range
    requests, ``fetch`` the source to a temp file first and pass that path. Returns the COG
    footprint in EPSG:4326.
    """
    if not overwrite and exists(dst):
        return footprint_4326(dst)

    src_path = _vsi(src)
    env = dict(READ_ENV)
    if extra_env:
        env.update(extra_env)

    with rio.Env(**env), rio.open(src_path) as s, _staged_local_path(dst) as local:
        # Predictor decorrelates neighbouring samples before ZSTD: 3 (floating-point) for
        # float bands, 2 (horizontal) for ints. Predictor 2 on raw float bytes can *inflate*
        # the output, so the COG driver's dtype-blind ``YES`` is not safe here.
        predictor = 3 if s.dtypes[0].startswith(("float", "complex")) else 2
        rio.shutil.copy(
            s, local, driver="COG", compress="ZSTD", predictor=predictor,
            blocksize=512, overview_resampling=resampling,
        )
    return footprint_4326(dst)


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


# GDAL output-type names for ``gdal_rasterize -ot``, keyed by numpy dtype string.
_GDAL_OT = {
    "uint8": "Byte", "int8": "Int8", "uint16": "UInt16", "int16": "Int16",
    "uint32": "UInt32", "int32": "Int32", "float32": "Float32", "float64": "Float64",
}


def _config_flags(env: dict) -> list[str]:
    """``--config K V`` pairs for a GDAL CLI invocation, from a ``rio.Env``-style dict."""
    flags: list[str] = []
    for k, v in env.items():
        flags += ["--config", k, str(v)]
    return flags


def _is_epsg_4326(crs) -> bool:
    """Whether an OGR/pyproj CRS identifier denotes EPSG:4326.

    Unknown/``None`` is treated as already on grid — the historical assumption for sources with
    no declared CRS (GeoJSON/OSM, which are WGS84). Anything that resolves to a different EPSG
    code (many SDPT layers are EPSG:3857 or UTM) must be reprojected before burning.
    """
    if not crs:
        return True
    try:
        from pyproj import CRS

        return CRS.from_user_input(crs).to_epsg() == 4326
    except Exception:
        return "4326" in str(crs)


def _localize_layer(
    src: str,
    layer: str | None,
    where: str | None,
    spat: tuple[float, float, float, float] | None,
    env: dict,
) -> tuple[str, str, int, tuple[float, float, float, float] | None]:
    """Stream the (``where``-filtered, ``spat``-windowed) vector ``layer`` to a local EPSG:4326
    GeoPackage with ``ogr2ogr`` and return ``(path, layer_name, n_features, total_bounds_4326)``.

    One pass over ``src`` here replaces what would otherwise be two remote opens by
    :func:`rasterize_to_cog`: ``gdal_rasterize`` ignores ``-te`` as a spatial filter (it re-reads
    the whole layer) and is preceded by the emptiness pre-check, so a remote ``/vsizip//vsicurl``
    GDB would be read end to end twice. It also reprojects: ``gdal_rasterize`` burns coordinates
    onto the ``-te`` grid as-is and never reprojects, but many SDPT country layers are in
    EPSG:3857/UTM. ``spat`` is the snapped window in EPSG:4326 (``None`` for the whole layer) and
    is pushed down through the source spatial index, so an empty window extracts nothing. The
    caller owns the returned file's parent dir and removes it. Geometries never enter Python.
    """
    import pyogrio

    if shutil.which("ogr2ogr") is None:
        raise RuntimeError(
            "staging a remote/non-EPSG:4326 vector source requires the ogr2ogr CLI but it is "
            "not on PATH; install GDAL (the gdal-bin package)."
        )
    tmpdir = tempfile.mkdtemp(prefix="bii_localize_")
    out = os.path.join(tmpdir, "src.gpkg")
    cmd = ["ogr2ogr", *_config_flags(env), "-t_srs", "EPSG:4326", "-f", "GPKG", "-nln", "feat"]
    if where:
        cmd += ["-where", where]
    if spat is not None:
        cmd += ["-spat", *(repr(float(x)) for x in spat), "-spat_srs", "EPSG:4326"]
    cmd += [out, src]
    if layer:
        cmd.append(layer)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError(f"ogr2ogr failed ({proc.returncode}): {proc.stderr.strip()}")

    info = pyogrio.read_info(out, layer="feat")
    tb = info.get("total_bounds")
    return out, "feat", int(info.get("features") or 0), (tuple(tb) if tb is not None else None)


def _burn_to_cog(
    src: str,
    dst: str,
    layer: str | None,
    where: str | None,
    snapped: tuple[float, float, float, float],
    width: int,
    height: int,
    env: dict,
    *,
    burn: int,
    fill: int,
    dtype: str,
    all_touched: bool,
    nodata: float | None,
    overwrite: bool,
    extra_env: dict | None,
) -> tuple[float, float, float, float]:
    """``gdal_rasterize`` ``src`` onto the snapped ``-te``/``-ts`` grid, then stream the burn to a
    COG at ``dst`` (``nearest`` overviews — categorical mask). Returns the EPSG:4326 footprint."""
    if shutil.which("gdal_rasterize") is None:
        raise RuntimeError(
            "vector rasterization requires the gdal_rasterize CLI but it is not on PATH; "
            "install GDAL (the gdal-bin package)."
        )
    w, s, e, n = (float(x) for x in snapped)
    cmd = ["gdal_rasterize", *_config_flags(env)]
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
        return translate_to_cog(
            tmp_tif, dst, resampling="nearest", overwrite=overwrite, extra_env=extra_env
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


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
    or a file geodatabase over ``/vsizip//vsicurl`` (``layer=<region>``). Geometries are never
    read into Python/geopandas; the burn is stream-converted to a COG (``nearest`` overviews —
    the output is a categorical mask).

    ``bounds`` (EPSG:4326 west, south, east, north) is snapped outward to the BII grid for the
    output extent; pass ``None`` to use the source layer's full extent. ``all_touched=True`` keeps
    thin features (single-pixel roads) from dropping out. ``where`` is an OGR attribute filter.
    With ``skip_empty`` a window/layer with no matching feature returns ``None`` (no COG written),
    avoiding a burn for the many empty tiles and keeping the index lean.

    A **remote** source (read over ``/vsicurl``) or one **not in EPSG:4326** is first staged once
    to a local EPSG:4326 GeoPackage via :func:`_localize_layer` — ``gdal_rasterize`` re-reads the
    whole layer (it doesn't use ``-te`` as a spatial filter) and never reprojects, so streaming it
    twice from the remote GDB, or burning EPSG:3857/UTM coordinates onto a degree grid, is avoided.
    A local source already on the grid (OSM roads) is read in place.
    """
    if not overwrite and exists(dst):
        return footprint_4326(dst)
    env = dict(READ_ENV, **(extra_env or {}))

    import pyogrio

    # Snap the requested window once so the local extract and the burn share one grid.
    width = height = None
    snapped: tuple[float, float, float, float] | None = None
    if bounds is not None:
        width, height, snapped = snap_grid(bounds, res)

    # A remote source would be opened twice (pre-check + burn), each a full network pass; a
    # non-EPSG:4326 source can't be burned onto the degree grid as-is. Either way, stage a single
    # local EPSG:4326 copy first. (Probe the CRS only for local sources — remote is always staged.)
    needs_local = "/vsicurl" in src
    if not needs_local:
        with rio.Env(**env):
            info = pyogrio.read_info(src, layer=layer)
        needs_local = not _is_epsg_4326(info.get("crs"))

    if needs_local:
        local, llayer, n_features, lbounds = _localize_layer(src, layer, where, snapped, env)
        try:
            # The exact feature count from the (filtered, windowed) extract is the emptiness signal.
            if n_features == 0 and (skip_empty or bounds is None):
                return None
            if bounds is None:
                if lbounds is None:
                    return None
                width, height, snapped = snap_grid(lbounds, res)
            # ``where`` was already applied during the extract.
            return _burn_to_cog(
                local, dst, llayer, None, snapped, width, height, env,
                burn=burn, fill=fill, dtype=dtype, all_touched=all_touched,
                nodata=nodata, overwrite=overwrite, extra_env=extra_env,
            )
        finally:
            shutil.rmtree(os.path.dirname(local), ignore_errors=True)

    # Local source already on the BII grid: read it in place (opening twice is cheap on disk).
    if bounds is None:
        if not info.get("features") or info.get("total_bounds") is None:
            return None  # empty layer -> nothing to burn
        width, height, snapped = snap_grid(tuple(info["total_bounds"]), res)

    # No matching feature in the window -> the burn would be all-fill, so skip it. ``max_features=1``
    # stops after the first hit (reads at most one feature). ``read_geometry`` stays on so GDAL's
    # GeoJSON driver applies the bbox spatial filter for attribute-less features.
    if skip_empty:
        w, s, e, n = (float(x) for x in snapped)
        with rio.Env(**env):
            present = pyogrio.read_dataframe(
                src, layer=layer, where=where, bbox=(w, s, e, n), max_features=1,
            )
        if len(present) == 0:
            return None

    return _burn_to_cog(
        src, dst, layer, where, snapped, width, height, env,
        burn=burn, fill=fill, dtype=dtype, all_touched=all_touched,
        nodata=nodata, overwrite=overwrite, extra_env=extra_env,
    )
