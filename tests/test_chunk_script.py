"""Unit tests for ``scripts/test_chunk.py`` — no network.

The remote reads (``model.compute_all``) are stubbed with synthetic layers; the real chunk
selection, ``cog_worker`` COG write, and the script's output validation run end to end against a
local output root. Mirrors ``test_process.py``'s offline stubbing.
"""

import importlib.util
from pathlib import Path

import numpy as np
import pytest
import rasterio as rio
from cog_worker import Worker

from bii import config, process

# scripts/ is not a package — load the script module by path.
_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "test_chunk.py"
_spec = importlib.util.spec_from_file_location("test_chunk_script", _SCRIPT)
test_chunk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(test_chunk)


@pytest.fixture
def local_out(tmp_path, monkeypatch):
    root = str(tmp_path / "out")
    monkeypatch.setattr(config, "OUT_ROOT", root)
    return root


@pytest.fixture
def one_year(monkeypatch):
    monkeypatch.setattr(config, "START_YEAR", 2020)
    monkeypatch.setattr(config, "END_YEAR", 2020)


def _stub_compute_all(worker):
    shape = (1, worker.height, worker.width)
    return {
        key: np.ma.MaskedArray(np.ones(shape, np.float32), mask=np.zeros(shape, bool))
        for key in process.output_layers()
    }


def test_first_chunk_is_json_serializable_and_indexable():
    from cog_worker import Manager

    manager = Manager(bounds=(-86.0, 9.0, -84.0, 11.0), scale=0.5, proj="EPSG:4326", buffer=0)
    chunk = test_chunk.first_chunk(manager, chunksize=4096)
    # proj_bounds is a plain list (JSON round-tripped), exactly as the Batch manifest stores it.
    assert isinstance(chunk["proj_bounds"], list)
    assert Worker(**chunk).bounds  # rebuildable

    with pytest.raises(SystemExit):
        test_chunk.first_chunk(manager, chunksize=4096, index=99)


def test_validate_output_accepts_valid_cog_and_rejects_bad(local_out, tmp_path):
    chunk = {"proj": "EPSG:4326", "scale": 0.1, "buffer": 0, "proj_bounds": (-86.0, 9.0, -85.0, 10.0)}
    worker = Worker(**chunk)
    arr = np.ma.MaskedArray(np.ones((1, 10, 10), np.float32), mask=np.zeros((1, 10, 10), bool))
    uri = process.output_uri("v1", "bii_2020", worker)
    process.persist_cog(worker, arr, uri)
    test_chunk.validate_output(uri, worker)  # valid -> no raise

    # Wrong expected shape -> assertion failure.
    other = Worker(proj="EPSG:4326", scale=0.1, buffer=0, proj_bounds=(-86.0, 9.0, -84.0, 11.0))
    with pytest.raises(AssertionError):
        test_chunk.validate_output(uri, other)


def test_run_processes_and_validates_first_chunk(local_out, one_year, monkeypatch):
    monkeypatch.setattr(process.model, "compute_all", _stub_compute_all)

    result = test_chunk.run(bounds=(-86.0, 9.0, -84.0, 11.0), chunksize=4096, run_id="v1", index=0)

    assert result["complete"]
    assert result["validated"] == len(process.output_layers()) == 3
    for uri in result["written"]:
        with rio.open(uri) as src:
            assert src.crs.to_epsg() == 4326 and src.dtypes[0] == "float32"


def test_main_redirects_output_root_to_local(tmp_path, one_year, monkeypatch):
    monkeypatch.setattr(process.model, "compute_all", _stub_compute_all)
    out = str(tmp_path / "chunk_out")

    result = test_chunk.main(["--bounds", "-86", "9", "-84", "11", "--out", out, "--run-id", "v1"])

    assert config.OUT_ROOT == out
    assert result["validated"] == 3
    assert all(uri.startswith(out) for uri in result["written"])
