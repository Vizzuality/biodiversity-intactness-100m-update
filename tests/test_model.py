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
    rng = np.random.default_rng(0)
    shape = (1, n, n)

    def masked(arr):
        return np.ma.MaskedArray(arr, mask=np.zeros(shape, bool))

    landcover = np.full(shape, 4, np.int16)  # valid (>1)
    landcover[0, :4, :] = 5  # crops
    landcover[0, 4:8, :] = 7  # built area
    landcover[0, -2:, :] = 0  # nodata (<=1) -> masked

    roads = np.zeros(shape, np.uint8)
    roads[0, n // 2, n // 2] = 1  # one target cell for the distance transform

    return {
        # raw FML codes; managed forest is >30 & <55.
        "forestManagement": masked(rng.integers(0, 60, shape).astype(np.int16)),
        "accessibility": masked(rng.random(shape) * 2000),
        "roads": roads,
        "forestLoss": masked(rng.integers(0, 24, shape).astype(np.int16)),
        "landcover": masked(landcover),
        "population": masked(rng.random(shape) * 100),
        "nightlights": masked(rng.random(shape) * 10),
    }


def test_predictors_decode_managed_forest():
    # FML managed-forest is codes >30 & <55; forestManagement_100m is the focal mean of that mask.
    base = _synthetic_layers()

    def fm_predictor(code):
        layers = base | {"forestManagement": np.ma.MaskedArray(np.full((1, 24, 24), code, np.int16), mask=False)}
        return dict(model._static_predictors(layers, config.SCALE_METERS))["forestManagement_100m"]

    assert fm_predictor(40) == pytest.approx(1.0)  # managed
    assert fm_predictor(11) == pytest.approx(0.0)  # unmanaged


def test_calc_bii_product_form_and_masking():
    worker = _FakeWorker()
    layers = _synthetic_layers()
    out = model.calc_bii(worker, layers, year=2020)

    assert set(out) == {"abundance", "community_similarity", "bii"}
    bii, ab, cs = out["bii"], out["abundance"], out["community_similarity"]

    np.testing.assert_allclose(np.ma.filled(bii, 0), np.ma.filled(ab * cs, 0))

    # Nodata landcover (<=1) masked in every output.
    expected_mask = ~(layers["landcover"].data > 1)
    np.testing.assert_array_equal(np.ma.getmaskarray(bii), expected_mask)
    assert np.isfinite(bii.compressed()).all()
    assert (bii.compressed() >= 0).all()


def test_compute_all_matches_per_year_calc_bii(monkeypatch):
    # compute_all folds the static predictors once and reuses them across years (the chunk-memory
    # optimization); it must still yield exactly what an independent per-year calc_bii gives.
    worker, layers = _FakeWorker(), _synthetic_layers()
    monkeypatch.setattr(model, "read_static_assets", lambda w: layers)
    monkeypatch.setattr(model, "read_annual_assets", lambda w, year: layers)

    out = dict(model.compute_all(worker))
    assert set(out) == {f"bii_{y}" for y in config.years()}
    for year in config.years():
        expected = model.calc_bii(worker, layers, year=year)["bii"]
        np.testing.assert_allclose(np.ma.filled(out[f"bii_{year}"], 0), np.ma.filled(expected, 0))
