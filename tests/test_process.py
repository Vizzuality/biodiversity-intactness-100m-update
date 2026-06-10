"""Unit tests for the per-chunk worker entrypoint — no network.

``compute_all`` (the only part that reads remote sources) is stubbed with synthetic layers; the
real ``cog_worker.Worker``, COG write, idempotency, and manifest loading are exercised end to end
against a local output root.
"""

import json

import numpy as np
import pytest
import rasterio as rio
from cog_worker import Worker
from rio_cogeo.cogeo import cog_validate

from bii import config, process

# A tiny EPSG:4326 chunk (coarse scale, no buffer) so synthetic layers are 10x10.
_CHUNK = {"proj": "EPSG:4326", "scale": 0.1, "buffer": 0, "proj_bounds": (-86.0, 9.0, -85.0, 10.0)}


@pytest.fixture
def local_out(tmp_path, monkeypatch):
    root = str(tmp_path / "out")
    monkeypatch.setattr(config, "OUT_ROOT", root)
    return root


@pytest.fixture
def one_year(monkeypatch):
    """Restrict the year range to 2020 so a run produces just 3 layers (faster, easier asserts)."""
    monkeypatch.setattr(config, "START_YEAR", 2020)
    monkeypatch.setattr(config, "END_YEAR", 2020)


def _stub_compute_all(worker):
    shape = (1, worker.height, worker.width)
    return {
        key: np.ma.MaskedArray(np.ones(shape, np.float32), mask=np.zeros(shape, bool))
        for key in process.output_layers()
    }


def test_output_layers_are_metric_year_keys():
    layers = process.output_layers()
    assert len(layers) == len(process.BII_METRICS) * len(config.years())
    assert "bii_2024" in layers and "abundance_2017" in layers


def test_output_uri_is_deterministic_and_north_west_named(local_out):
    worker = Worker(**_CHUNK)
    uri = process.output_uri("v1", "bii_2020", worker)
    # <out>/<run>/<layer>/<layer>_<north>_<west>.tif — north=10, west=-86 for this chunk.
    assert uri == f"{local_out}/v1/bii_2020/bii_2020_10.000000_-86.000000.tif"
    assert process.output_uri("v1", "bii_2020", worker) == uri  # stable across calls


def test_persist_cog_writes_valid_cog_and_skips(local_out):
    worker = Worker(**_CHUNK)
    arr = np.ma.MaskedArray(np.ones((1, 10, 10), np.float32), mask=np.zeros((1, 10, 10), bool))
    uri = process.output_uri("v1", "bii_2020", worker)

    assert process.persist_cog(worker, arr, uri) is True
    valid, errors, _ = cog_validate(uri)
    assert valid, errors
    with rio.open(uri) as src:
        assert src.dtypes[0] == "float32"
        assert src.crs.to_epsg() == 4326

    # Idempotent: a second write to an existing key is skipped.
    assert process.persist_cog(worker, arr, uri) is False


def test_process_writes_all_layers_then_skips_on_rerun(local_out, one_year, monkeypatch):
    monkeypatch.setattr(process.model, "compute_all", _stub_compute_all)

    result = process.process(_CHUNK, run_id="v1")
    assert result["complete"] and result["skipped"] == []
    assert len(result["written"]) == len(process.output_layers()) == 3
    for uri in result["written"]:
        valid, errors, _ = cog_validate(uri)
        assert valid, errors

    # Re-run: every output exists, so the precheck short-circuits before compute (which would
    # raise if called — guard that the reads truly don't happen).
    monkeypatch.setattr(process.model, "compute_all",
                        lambda w: (_ for _ in ()).throw(AssertionError("recomputed")))
    rerun = process.process(_CHUNK, run_id="v1")
    assert rerun["written"] == [] and len(rerun["skipped"]) == 3


def test_load_chunk_reads_nth_line(tmp_path):
    manifest = tmp_path / "chunks.jsonl"
    chunks = [dict(_CHUNK, proj_bounds=[w, 9.0, w + 1, 10.0]) for w in (-86.0, -85.0, -84.0)]
    manifest.write_text("\n".join(json.dumps(c) for c in chunks) + "\n")

    assert process.load_chunk(str(manifest), 0)["proj_bounds"][0] == -86.0
    assert process.load_chunk(str(manifest), 2)["proj_bounds"][0] == -84.0


def test_default_run_id_env_override(monkeypatch):
    monkeypatch.setattr(config, "RUN_ID", "cfg-default")
    monkeypatch.delenv("BII_RUN_ID", raising=False)
    assert process.default_run_id() == "cfg-default"
    monkeypatch.setenv("BII_RUN_ID", "run-42")
    assert process.default_run_id() == "run-42"
