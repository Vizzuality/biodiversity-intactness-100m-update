"""BII model — coefficients, transforms, and the per-chunk BII computation.

Ported from ``notebooks/2. biodiversity-impact.ipynb`` (the validated working implementation).
The math (PREDICTS abundance + community-similarity regressions, focal/distance predictors)
is reproduced verbatim; only the I/O boundary changed:

* **Asset acquisition** goes through :func:`bii.tile_index.lookup` (footprint index / live LULC
  STAC) + ``worker.read`` instead of the original private STAC ``read_stac`` helper.
* **``forestManagement``** is staged as raw FML categorical codes and decoded to a managed-forest
  mask (``>30 & <55``) inside :func:`_static_predictors`. The planned ``sources.py`` provider switch
  (FML vs an already-0/1 SDPT planted mask) will own this decode.
* **BII is the product** ``abundance * community_similarity`` (notebook 2 / standard PREDICTS
  BII), not notebook 3's sum form.

The grid (``EPSG:4326``, ~100 m, 100 px buffer, ``DEG2METERS``), the ``distRoads`` 10 km clip,
and the landcover nodata masking are all carried over unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator

import cv2
import edt
import numpy as np

from . import config, tile_index

# --------------------------------------------------------------------------------------
# Regression coefficients + link functions (verbatim from notebook 2)
# --------------------------------------------------------------------------------------
ABUNDANCE_COEFFICIENTS = {
    "Intercept": 3.70170553703471,
    "ln_distRoads": -0.00981977902849513,
    "ln_pD2006_1000m": -0.096305338569171,
    "lcCrops_100m": -0.072689104315292,
    "lcBuiltArea_1000m": 0.254091185638773,
    "lcBuiltArea_100m": -0.176460730121838,
    "forestLoss2006_100m": -0.155530295244675,
}
ABUNDANCE_TRANSFORM = "log"

COMMUNITY_SIMILARITY_COEFFICIENTS = {
    "Intercept": 0.218130679408732,
    "ln_distRoads": 0.0181332392937601,
    "ln_accessibility": 0.137135301580816,
    "ln_pD2006_1000m": 0.252785443092897,
    "ln_nL2012_1000m": -0.313937017511882,
    "lcCrops_1000m": -0.935394995786293,
    "lcCrops_100m": -0.248198099726017,
    "lcBuiltArea_1000m": -0.745895005503182,
    "forestManagement_100m": -0.769972275297042,
    "forestLoss2006_100m": 0.339447252726624,
}
COMMUNITY_SIMILARITY_TRANSFORM = "logit"

INVERSE_TRANSFORMS = {
    "sqrt": lambda x: x**2,
    "logit": lambda x: 1 / (1 + np.exp(-x)),
    "log": lambda x: np.exp(x),
}


def _transform_max(coefs: dict, transform: str) -> float:
    """Per-cell maximum that normalizes a linear predictor to 0–1: the inverse link evaluated at
    the predictors' max-impact inputs (ln_accessibility at 1440, ln_distRoads at 10 km)."""
    return float(INVERSE_TRANSFORMS[transform](
        coefs["Intercept"]
        + coefs.get("ln_accessibility", 0) * np.log(1440)
        + coefs.get("ln_distRoads", 0) * np.log(10000)
    ))


ABUNDANCE_MAX = _transform_max(ABUNDANCE_COEFFICIENTS, ABUNDANCE_TRANSFORM)
COMMUNITY_SIMILARITY_MAX = _transform_max(COMMUNITY_SIMILARITY_COEFFICIENTS, COMMUNITY_SIMILARITY_TRANSFORM)

# --------------------------------------------------------------------------------------
# Asset groupings. forestLoss is single-epoch (a cumulative ``lossyear`` raster filtered per
# year inside calc_bii), so it lives with the static assets — matching the notebook.
# --------------------------------------------------------------------------------------
STATIC_ASSETS = ("forestManagement", "accessibility", "roads", "forestLoss")
ANNUAL_ASSETS = ("landcover", "population", "nightlights")


# --------------------------------------------------------------------------------------
# Raster helpers (verbatim from notebook 2)
# --------------------------------------------------------------------------------------
def nominal_scale(worker) -> float:
    """Pixel size in meters (``scale`` is in degrees for a geographic CRS)."""
    if worker.proj.crs.is_geographic:
        return worker.scale * config.DEG2METERS
    return worker.scale


def convolve(arr, radius, scale=1):
    """Focal mean over a square window of side ``radius`` meters (``cv2.blur``).

    float32 (not float64) output: the focal predictors dominate the model's memory, and the BII
    is written as float32 anyway, so the extra precision is discarded — the result moves by at
    most ~1e-6, well within tolerance."""
    if arr.ndim == 3 and arr.shape[0] == 1:
        arr = arr[0]
    kernel_size = round(radius / scale)
    return cv2.blur(arr.astype(np.float32), (kernel_size, kernel_size))[np.newaxis]


def fast_distance_transform(arr):
    """Squared Euclidean distance (px^2) to the nearest truthy cell, via ``edt``."""
    if arr.ndim == 3 and arr.shape[0] == 1:
        arr = arr[0]
    return edt.edtsq(np.logical_not(arr))[np.newaxis]


def expand_valid(arr, px):
    """Grow a masked array's valid-data zone by ``px`` pixels, filling new cells with a neighboring
    valid value (grayscale dilation)."""
    if arr.ndim == 3 and arr.shape[0] == 1:
        arr = arr[0]
    mask = np.ma.getmaskarray(arr)
    data = np.ma.filled(arr, 0).astype(np.float32)
    grown = cv2.dilate(data, np.ones((2 * px + 1, 2 * px + 1), np.uint8))
    return np.where(mask, grown, data)[np.newaxis]


# --------------------------------------------------------------------------------------
# Asset acquisition — replaces the notebook's read_stac. Routes through tile_index.lookup
# (footprint index for staged assets; live LULC STAC for landcover) + worker.read, which
# mosaics overlapping tiles on the fly.
# --------------------------------------------------------------------------------------
def _read(worker, asset: str, year: int | None = None):
    bounds = worker.lnglat_bounds()
    if not np.isfinite(bounds).all():
        return worker.read([])
    return worker.read(tile_index.lookup(asset, bounds, year))


def read_static_assets(worker) -> dict:
    return {a: _read(worker, a) for a in STATIC_ASSETS}


def read_annual_assets(worker, year: int) -> dict:
    return {a: _read(worker, a, year) for a in ANNUAL_ASSETS}


# --------------------------------------------------------------------------------------
# BII computation
# --------------------------------------------------------------------------------------
def _static_predictors(layers: dict, scale: float) -> Iterator[tuple[str, object]]:
    """Year-invariant predictors (derived from the roads/accessibility/forestManagement assets, plus
    the Intercept). :func:`compute_all` folds these once per chunk and reuses the partial sums across
    every year — the costly distance transform and dilations don't change year to year."""
    distRoads = np.clip(np.sqrt(fast_distance_transform(layers["roads"])) * scale, 0, 10000)
    # accessibility is ~1 km native: grow its valid zone 1 native px to cover the jagged nodata
    accessibility = expand_valid(layers["accessibility"], max(1, round(1000 / scale)))
    accessibility = np.clip(accessibility, 0, 1440)
    # FML managed-forest classes (31 replanted, 32 woody plantation, 40 oil palm, 53 agroforestry)
    fml = layers["forestManagement"]
    forestManagement = np.ma.filled((fml > 30) & (fml < 55), False)
    yield "ln_distRoads", np.log(distRoads + 1)
    yield "ln_accessibility", np.log(accessibility + 1)
    yield "forestManagement_100m", convolve(forestManagement, 200, scale)
    yield "Intercept", 1


def _annual_predictors(layers: dict, scale: float, year: int) -> Iterator[tuple[str, object]]:
    """Predictors that vary by year: the landcover/population/nightlights focals and the
    year-filtered forest loss."""
    ln_nightlights = np.log(layers["nightlights"] + 1).data
    ln_population = np.log(np.nan_to_num(np.ma.filled(layers["population"], 0), 0) + 1)
    crops = layers["landcover"].data == 5
    builtArea = layers["landcover"].data == 7
    forestLoss = (layers["forestLoss"].data <= year - 2000) & (layers["forestLoss"].data > 0)
    yield "ln_nL2012_1000m", convolve(ln_nightlights, 2000, scale)
    yield "ln_pD2006_1000m", convolve(ln_population, 2000, scale)
    yield "lcCrops_1000m", convolve(crops, 2000, scale)
    yield "lcCrops_100m", convolve(crops, 200, scale)
    yield "lcBuiltArea_1000m", convolve(builtArea, 2000, scale)
    yield "lcBuiltArea_100m", convolve(builtArea, 200, scale)
    yield "forestLoss2006_100m", convolve(forestLoss, 200, scale)


def _fold(predictors, abundance, community_similarity, computed=None):
    """Accumulate ``coef * predictor`` into the abundance and community-similarity linear sums. A
    generator-fed fold so each focal output streams one at a time and is freed before the next —
    the peak holds one rather than all coexisting (the ceiling at the 8192 px chunk size)."""
    for name, p in predictors:
        if name in ABUNDANCE_COEFFICIENTS:
            abundance = abundance + p * ABUNDANCE_COEFFICIENTS[name]
        if name in COMMUNITY_SIMILARITY_COEFFICIENTS:
            community_similarity = community_similarity + p * COMMUNITY_SIMILARITY_COEFFICIENTS[name]
        if computed is not None:
            computed[name] = p
        del p
    return abundance, community_similarity


def _finalize(abundance, community_similarity, layers: dict, computed: dict | None = None) -> dict:
    """Inverse-link and normalize the linear sums into abundance/community_similarity/bii, masked to
    valid landcover. With ``computed``, also merges in the inputs and per-predictor arrays."""
    abundance = INVERSE_TRANSFORMS[ABUNDANCE_TRANSFORM](abundance) / ABUNDANCE_MAX
    community_similarity = INVERSE_TRANSFORMS[COMMUNITY_SIMILARITY_TRANSFORM](community_similarity) / COMMUNITY_SIMILARITY_MAX
    bii = abundance * community_similarity

    nodata = ~(layers["landcover"].data > 1)
    results = {
        "abundance": np.ma.MaskedArray(abundance, mask=nodata),
        "community_similarity": np.ma.MaskedArray(community_similarity, mask=nodata),
        "bii": np.ma.MaskedArray(bii, mask=nodata),
    }
    return layers | computed | results if computed is not None else results


def calc_bii(worker, layers: dict | None = None, year: int = config.START_YEAR, return_all: bool = False) -> dict:
    """Compute abundance, community similarity, and BII for one chunk/year.

    ``layers`` is a dict of read rasters keyed by asset name. If ``None``, the assets are acquired
    for ``worker``'s bounds. BII is the product of abundance and community similarity, masked to
    valid landcover.
    """
    if layers is None:
        layers = read_static_assets(worker) | read_annual_assets(worker, year)
    scale = nominal_scale(worker)
    computed = {} if return_all else None
    ab, cs = _fold(_static_predictors(layers, scale), 0.0, 0.0, computed)
    ab, cs = _fold(_annual_predictors(layers, scale, year), ab, cs, computed)
    return _finalize(ab, cs, layers, computed)


def compute_all(worker) -> Iterator[tuple[str, np.ndarray]]:
    """Yield ``("bii_<year>", MaskedArray)`` for every configured year. The year-invariant predictors
    (distance transform, accessibility dilation, forest-management focal) are folded once into the
    static partial sums and reused across years, so only the annual predictors recompute per year.

    A generator, not a dict: the entrypoint :mod:`bii.process` persists and releases each year's layer
    as it arrives, so the peak holds one year rather than all of them — the memory ceiling at the
    larger chunk size.
    """
    static_assets = read_static_assets(worker)
    scale = nominal_scale(worker)
    ab0, cs0 = _fold(_static_predictors(static_assets, scale), 0.0, 0.0)
    for year in config.years():
        layers = static_assets | read_annual_assets(worker, year)
        ab, cs = _fold(_annual_predictors(layers, scale, year), ab0, cs0)
        yield f"bii_{year}", _finalize(ab, cs, layers)["bii"]
