"""BII model — coefficients, transforms, and the per-chunk BII computation.

Ported from ``notebooks/2. biodiversity-impact.ipynb`` (the validated working implementation).
The math (PREDICTS abundance + community-similarity regressions, focal/distance predictors)
is reproduced verbatim; only the I/O boundary changed:

* **Asset acquisition** goes through :func:`bii.tile_index.lookup` (footprint index / live LULC
  STAC) + ``worker.read`` instead of the original private STAC ``read_stac`` helper.
* **``forestManagement``** reaches :func:`calc_bii` as an already-normalized managed-forest mask.
  The provider-specific decode (FML codes ``>30 & <55`` vs an SDPT planted mask) lives in the
  acquisition layer below — the seam the planned ``sources.py`` provider switch will own — so
  :func:`calc_bii` itself stays provider-agnostic.
* **BII is the product** ``abundance * community_similarity`` (notebook 2 / standard PREDICTS
  BII), not notebook 3's sum form.

The grid (``EPSG:4326``, ~100 m, 100 px buffer, ``DEG2METERS``), the ``distRoads`` 10 km clip,
and the landcover nodata masking are all carried over unchanged.
"""

from __future__ import annotations

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


def convolve(arr, radius, scale=1, dtype=float):
    """Focal mean over a square window of side ``radius`` meters (``cv2.blur``)."""
    if arr.ndim == 3 and arr.shape[0] == 1:
        arr = arr[0]
    kernel_size = round(radius / scale)
    return cv2.blur(arr.astype(dtype), (kernel_size, kernel_size))[np.newaxis]


def fast_distance_transform(arr):
    """Squared Euclidean distance (px^2) to the nearest truthy cell, via ``edt``."""
    if arr.ndim == 3 and arr.shape[0] == 1:
        arr = arr[0]
    return edt.edtsq(np.logical_not(arr))[np.newaxis]


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


def _managed_forest_mask(arr):
    """Decode the raw forestManagement raster into a 0/1 managed-forest mask.

    FML v3.2 is staged as raw categorical management-class codes; managed forest is codes
    ``>30 & <55`` (31 replanted, 32 woody plantation, 40 oil palm, 53 agroforestry). This is
    the provider-specific decode the planned ``sources.py`` switch will own (an SDPT planted
    mask is already 0/1 and would skip this); for now it defaults to the FML decode here so
    :func:`calc_bii` always receives a normalized mask.
    """
    return np.ma.filled((arr > 30) & (arr < 55), False)


def read_static_assets(worker) -> dict:
    layers = {a: _read(worker, a) for a in STATIC_ASSETS}
    layers["forestManagement"] = _managed_forest_mask(layers["forestManagement"])
    return layers


def read_annual_assets(worker, year: int) -> dict:
    return {a: _read(worker, a, year) for a in ANNUAL_ASSETS}


# --------------------------------------------------------------------------------------
# BII computation
# --------------------------------------------------------------------------------------
def calc_bii(worker, layers: dict | None = None, year: int = config.START_YEAR, return_all: bool = False) -> dict:
    """Compute abundance, community similarity, and BII for one chunk/year.

    ``layers`` is a dict of read rasters keyed by asset name; ``forestManagement`` is expected
    pre-normalized to a managed-forest mask (see :func:`read_static_assets`). If ``None``, the
    assets are acquired for ``worker``'s bounds. BII is the product of abundance and community
    similarity, masked to valid landcover.
    """
    if layers is None:
        layers = read_static_assets(worker) | read_annual_assets(worker, year)

    scale = nominal_scale(worker)

    forestManagement = layers["forestManagement"]
    forestLoss = (layers["forestLoss"].data <= year - 2000) & (layers["forestLoss"].data > 0)
    distRoads = np.sqrt(fast_distance_transform(layers["roads"])) * scale
    distRoads = np.clip(distRoads, 0, 10000)
    ln_distRoads = np.log(distRoads + 1)
    accessibility = np.clip(layers["accessibility"].data, 0, 1440)
    ln_accessibility = np.log(accessibility + 1)
    ln_nightlights = np.log(layers["nightlights"] + 1).data
    population = np.nan_to_num(np.ma.filled(layers["population"], 0), 0)
    ln_population = np.log(population + 1)
    crops = layers["landcover"].data == 5
    builtArea = layers["landcover"].data == 7
    nodata = ~(layers["landcover"].data > 1)

    predictors = {
        "ln_distRoads": ln_distRoads,
        "ln_accessibility": ln_accessibility,
        "ln_nL2012_1000m": convolve(ln_nightlights, 2000, scale),
        "ln_pD2006_1000m": convolve(ln_population, 2000, scale),
        "forestManagement_100m": convolve(forestManagement, 200, scale),
        "lcCrops_1000m": convolve(crops, 2000, scale),
        "lcCrops_100m": convolve(crops, 200, scale),
        "lcBuiltArea_1000m": convolve(builtArea, 2000, scale),
        "lcBuiltArea_100m": convolve(builtArea, 200, scale),
        "forestLoss2006_100m": convolve(forestLoss, 200, scale),
        "Intercept": 1,
    }

    abundance_max = INVERSE_TRANSFORMS[ABUNDANCE_TRANSFORM](
        ABUNDANCE_COEFFICIENTS["Intercept"]
        + ABUNDANCE_COEFFICIENTS.get("ln_accessibility", 0) * np.log(1440)
        + ABUNDANCE_COEFFICIENTS.get("ln_distRoads", 0) * np.log(10000)
    )
    # Element-wise weighted sum over predictors. Builtin sum() (not np.sum) reduces via ``+`` so
    # the scalar Intercept term broadcasts against the (1,H,W) predictor arrays — np.sum on the
    # mixed-shape list raises on modern numpy (the notebook relied on the old object-array path).
    abundance = sum(predictors[k] * v for k, v in ABUNDANCE_COEFFICIENTS.items())
    abundance = INVERSE_TRANSFORMS[ABUNDANCE_TRANSFORM](abundance) / abundance_max

    community_similarity_max = INVERSE_TRANSFORMS[COMMUNITY_SIMILARITY_TRANSFORM](
        COMMUNITY_SIMILARITY_COEFFICIENTS["Intercept"]
        + COMMUNITY_SIMILARITY_COEFFICIENTS.get("ln_accessibility", 0) * np.log(1440)
        + COMMUNITY_SIMILARITY_COEFFICIENTS.get("ln_distRoads", 0) * np.log(10000)
    )
    community_similarity = sum(
        predictors[k] * v for k, v in COMMUNITY_SIMILARITY_COEFFICIENTS.items()
    )
    community_similarity = (
        INVERSE_TRANSFORMS[COMMUNITY_SIMILARITY_TRANSFORM](community_similarity)
        / community_similarity_max
    )

    bii = abundance * community_similarity
    results = {
        "abundance": np.ma.MaskedArray(abundance, mask=nodata),
        "community_similarity": np.ma.MaskedArray(community_similarity, mask=nodata),
        "bii": np.ma.MaskedArray(bii, mask=nodata),
    }

    if return_all:
        return layers | predictors | results
    return results


def compute_all(worker) -> dict:
    """Run :func:`calc_bii` for every configured year, reusing the static assets.

    Returns ``{<layer>_<year>: MaskedArray}`` for abundance, community_similarity, and bii —
    the entrypoint :mod:`bii.process` persists as output COGs.
    """
    static_assets = read_static_assets(worker)

    all_results = {}
    for year in config.years():
        assets = static_assets | read_annual_assets(worker, year)
        for k, v in calc_bii(worker, assets, year).items():
            all_results[f"{k}_{year}"] = v

    return all_results
