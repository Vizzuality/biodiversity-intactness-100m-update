"""Unit tests for the BII model math — no network, synthetic layers."""

import numpy as np
import pytest

from bii import config, model


class _FakeCRS:
    is_geographic = True


class _FakeProj:
    crs = _FakeCRS()


class _FakeWorker:
    """Minimal stand-in: calc_bii's pure math only touches ``proj.crs`` and ``scale``."""

    proj = _FakeProj()
    scale = config.SCALE_DEG


def _synthetic_layers(n=24):
    rng = np.random.default_rng(0)  # seed-free determinism: np default_rng is fine offline
    shape = (1, n, n)

    def masked(arr):
        return np.ma.MaskedArray(arr, mask=np.zeros(shape, bool))

    landcover = np.full(shape, 4, np.int16)  # valid (>1) by default
    landcover[0, :4, :] = 5  # crops
    landcover[0, 4:8, :] = 7  # built area
    landcover[0, -2:, :] = 0  # nodata (<=1) -> masked in output

    roads = np.zeros(shape, np.uint8)
    roads[0, n // 2, n // 2] = 1  # a single road cell so distance transform has a target

    return {
        # forestManagement arrives pre-normalized to a 0/1 mask (read_static_assets does this).
        "forestManagement": (rng.random(shape) > 0.7).astype(float),
        "accessibility": masked(rng.random(shape) * 2000),
        "roads": roads,
        "forestLoss": masked(rng.integers(0, 24, shape).astype(np.int16)),
        "landcover": masked(landcover),
        "population": masked(rng.random(shape) * 100),
        "nightlights": masked(rng.random(shape) * 10),
    }


def test_nominal_scale_geographic():
    assert model.nominal_scale(_FakeWorker()) == pytest.approx(config.SCALE_DEG * config.DEG2METERS)
    assert model.nominal_scale(_FakeWorker()) == pytest.approx(config.SCALE_METERS)


def test_convolve_preserves_band_shape():
    arr = np.ones((1, 16, 16))
    out = model.convolve(arr, 200, scale=100)
    assert out.shape == (1, 16, 16)
    assert out == pytest.approx(1.0)  # focal mean of a constant field is the constant


def test_managed_forest_mask_decodes_fml_codes():
    # FML managed-forest is codes >30 & <55; everything else is unmanaged.
    codes = np.array([[[11, 20, 31, 32, 40, 53, 55, 0]]], np.int16)
    mask = model._managed_forest_mask(np.ma.MaskedArray(codes, mask=False))
    assert mask.tolist() == [[[0, 0, 1, 1, 1, 1, 0, 0]]]


def test_calc_bii_product_form_and_masking():
    worker = _FakeWorker()
    layers = _synthetic_layers()
    out = model.calc_bii(worker, layers, year=2020)

    assert set(out) == {"abundance", "community_similarity", "bii"}
    bii, ab, cs = out["bii"], out["abundance"], out["community_similarity"]

    # BII is the product of the two components (notebook 2 form), not the sum.
    np.testing.assert_allclose(np.ma.filled(bii, 0), np.ma.filled(ab * cs, 0))

    # Nodata landcover (<=1) is masked in every output layer; valid cells are finite.
    expected_mask = ~(layers["landcover"].data > 1)
    np.testing.assert_array_equal(np.ma.getmaskarray(bii), expected_mask)
    assert np.isfinite(bii.compressed()).all()
    assert (bii.compressed() >= 0).all()


def test_calc_bii_return_all_includes_predictors():
    out = model.calc_bii(_FakeWorker(), _synthetic_layers(), year=2020, return_all=True)
    # return_all merges inputs + predictors + results.
    assert {"bii", "Intercept", "forestManagement_100m", "landcover"} <= set(out)
