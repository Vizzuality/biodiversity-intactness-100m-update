"""BII model

"""

from __future__ import annotations

from collections.abc import Iterator

import cv2
import edt
import numpy as np

from . import config, tile_index

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
    """Per-cell maximum that normalizes a linear predictor to 0-1: inverse link at the max-impact
    inputs (ln_accessibility at 1440 min, ln_distRoads at 10 km)."""
    return float(INVERSE_TRANSFORMS[transform](
        coefs["Intercept"]
        + coefs.get("ln_accessibility", 0) * np.log(1440)
        + coefs.get("ln_distRoads", 0) * np.log(10000)
    ))


ABUNDANCE_MAX = _transform_max(ABUNDANCE_COEFFICIENTS, ABUNDANCE_TRANSFORM)
COMMUNITY_SIMILARITY_MAX = _transform_max(COMMUNITY_SIMILARITY_COEFFICIENTS, COMMUNITY_SIMILARITY_TRANSFORM)

# forestLoss is a single cumulative ``lossyear`` raster filtered per year, so it groups static.
STATIC_ASSETS = ("forestManagement", "accessibility", "roads", "forestLoss")
ANNUAL_ASSETS = ("landcover", "population", "nightlights")


def nominal_scale(worker) -> float:
    """Pixel size in meters (``scale`` is in degrees for a geographic CRS)."""
    if worker.proj.crs.is_geographic:
        return worker.scale * config.DEG2METERS
    return worker.scale


def convolve(arr, radius, scale=1):
    """Focal mean over a square window of side ``radius`` meters (``cv2.blur``)."""
    if arr.ndim == 3 and arr.shape[0] == 1:
        arr = arr[0]
    kernel_size = round(radius / scale)
    return cv2.blur(arr.astype(np.float32), (kernel_size, kernel_size))[np.newaxis]


def fast_distance_transform(arr):
    """Euclidean distance (px) to the nearest truthy cell, via ``edt``."""
    if arr.ndim == 3 and arr.shape[0] == 1:
        arr = arr[0]
    # black_border=True treats edges as roads, avoiding a linux-specific edt bug.
    return edt.edt(np.logical_not(arr), black_border=True)[np.newaxis]


def close_landcover_seams(lc):
    """Fill 1px nodata seams where overlapping IO MGRS tiles meet (a reprojection artifact at the
    slanted UTM tile edges, not real nodata) with the nearest valid land class via a morphological
    close. Genuine water/nodata regions are wider than the kernel and stay masked."""
    data = lc.data[0]
    valid = ((data > 1) & ~np.ma.getmaskarray(lc)[0]).astype(np.uint8)
    k = np.ones((3, 3), np.uint8)
    seam = cv2.morphologyEx(valid, cv2.MORPH_CLOSE, k).astype(bool) & ~valid.astype(bool)
    filled = np.where(seam, cv2.dilate(np.where(valid.astype(bool), data, 0), k), data)
    return np.ma.MaskedArray(filled[np.newaxis], (np.ma.getmaskarray(lc)[0] & ~seam)[np.newaxis])


def expand_valid(arr, px):
    """Grow a masked array's valid-data zone by ``px`` pixels, filling new cells with a neighboring
    valid value (grayscale dilation)."""
    if arr.ndim == 3 and arr.shape[0] == 1:
        arr = arr[0]
    mask = np.ma.getmaskarray(arr)
    data = np.ma.filled(arr, 0).astype(np.float32)
    grown = cv2.dilate(data, np.ones((2 * px + 1, 2 * px + 1), np.uint8))
    return np.where(mask, grown, data)[np.newaxis]


# Asset acquisition via tile_index.lookup (footprint index) +
# worker.read, which mosaics overlapping tiles on the fly.
def _read(worker, asset: str, year: int | None = None):
    bounds = worker.lnglat_bounds()
    if not np.isfinite(bounds).all():
        return worker.read([])
    return worker.read(tile_index.lookup(asset, bounds, year))


def read_static_assets(worker) -> dict:
    return {a: _read(worker, a) for a in STATIC_ASSETS}


def read_annual_assets(worker, year: int) -> dict:
    layers = {a: _read(worker, a, year) for a in ANNUAL_ASSETS}
    layers["landcover"] = close_landcover_seams(layers["landcover"])
    return layers


def _static_predictors(layers: dict, scale: float) -> Iterator[tuple[str, object]]:
    """Year-invariant predictors. :func:`compute_all` folds these once and reuses across years — the
    costly distance transform and dilations don't change year to year."""
    distRoads = np.clip(fast_distance_transform(layers["roads"]) * scale, 0, 10000)  # m, 10 km clip
    # accessibility is ~1 km native: grow valid zone 1 native px to cover jagged nodata
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
    """Predictors that vary by year: landcover/population/nightlights focals and year-filtered
    forest loss."""
    ln_nightlights = np.log(np.ma.filled(layers["nightlights"], 0) + 1)
    ln_population = np.log(np.nan_to_num(np.ma.filled(layers["population"], 0), 0) + 1)
    crops = layers["landcover"].data == 5  # LULC: 5 crops, 7 built area
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
    """Accumulate ``coef * predictor`` into the abundance and community-similarity linear sums.
    Generator-fed so each focal output is freed before the next"""
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
    valid landcover (landcover > 1). With ``computed``, also merges in inputs and per-predictor arrays."""
    abundance = np.clip(INVERSE_TRANSFORMS[ABUNDANCE_TRANSFORM](abundance) / ABUNDANCE_MAX, 0, 1)
    community_similarity = np.clip(INVERSE_TRANSFORMS[COMMUNITY_SIMILARITY_TRANSFORM](community_similarity) / COMMUNITY_SIMILARITY_MAX, 0, 1)
    bii = abundance * community_similarity

    nodata = ~(layers["landcover"].data > 1)
    results = {
        "abundance": np.ma.MaskedArray(abundance, mask=nodata),
        "community_similarity": np.ma.MaskedArray(community_similarity, mask=nodata),
        "bii": np.ma.MaskedArray(bii, mask=nodata),
    }
    return layers | computed | results if computed is not None else results


def calc_bii(worker, layers: dict | None = None, year: int = config.START_YEAR, return_all: bool = False) -> dict:
    """Compute abundance, community similarity, and BII for one chunk/year. If ``layers`` is
    ``None``, the assets are acquired for ``worker``'s bounds."""
    if layers is None:
        layers = read_static_assets(worker) | read_annual_assets(worker, year)
    scale = nominal_scale(worker)
    computed = {} if return_all else None
    ab, cs = _fold(_static_predictors(layers, scale), 0.0, 0.0, computed)
    ab, cs = _fold(_annual_predictors(layers, scale, year), ab, cs, computed)
    return _finalize(ab, cs, layers, computed)


def compute_all(worker) -> Iterator[tuple[str, np.ndarray]]:
    """Yield ``("bii_<year>", MaskedArray)`` for every configured year. Year-invariant predictors are
    folded once into the static partial sums and reused, so only annual predictors recompute per year.

    Yeilds one year at a time."""
    static_assets = read_static_assets(worker)
    scale = nominal_scale(worker)
    ab0, cs0 = _fold(_static_predictors(static_assets, scale), 0.0, 0.0)
    for year in config.years():
        layers = static_assets | read_annual_assets(worker, year)
        ab, cs = _fold(_annual_predictors(layers, scale, year), ab0, cs0)
        yield f"bii_{year}", _finalize(ab, cs, layers)["bii"]
