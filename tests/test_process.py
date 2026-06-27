"""Unit tests for processing — the per-chunk worker and the run driver — no network, no AWS.

``compute_all`` (the only part reading remote sources) is stubbed with synthetic layers; the real
``cog_worker.Worker`` and COG write run against a local output root. The Batch client and ``docker
run`` are fakes recording their inputs.
"""

from types import SimpleNamespace

import numpy as np
import pytest
import rasterio as rio
from cog_worker import Manager, Worker
from rio_cogeo.cogeo import cog_validate

from bii import config, orchestration, process, io, tile_index

# Coarse scale, no buffer -> synthetic layers are 10x10.
_CHUNK = {"proj": "EPSG:4326", "scale": 0.1, "buffer": 0, "proj_bounds": (-86.0, 9.0, -85.0, 10.0)}

# Costa Rica-ish land region + coarse scale -> a Manager yields a handful of chunks.
_BOUNDS = (-86.0, 9.0, -84.0, 11.0)
_SCALE = 0.5


@pytest.fixture
def local_out(tmp_path, monkeypatch):
    root = str(tmp_path / "out")
    monkeypatch.setattr(config, "OUT_ROOT", root)
    return root


@pytest.fixture
def one_year(monkeypatch):
    """Restrict the year range to 2020 so a run produces just one layer."""
    monkeypatch.setattr(config, "START_YEAR", 2020)
    monkeypatch.setattr(config, "END_YEAR", 2020)


@pytest.fixture
def batch_env(monkeypatch):
    monkeypatch.setenv("BII_BATCH_QUEUE", "q")
    monkeypatch.setenv("BII_BATCH_JOB_DEF", "jd")


def _manager():
    return Manager(bounds=_BOUNDS, scale=_SCALE, proj="EPSG:4326", buffer=0)


def _stub_compute_all(worker):
    shape = (1, worker.height, worker.width)
    return [
        (key, np.ma.MaskedArray(np.ones(shape, np.float32), mask=np.zeros(shape, bool)))
        for key in process.output_layers()
    ]


def _write_outputs(chunks, run_id):
    """Materialize every output COG for ``chunks``, standing in for a completed run."""
    for chunk in chunks:
        worker = Worker(**chunk)
        arr = np.ma.MaskedArray(
            np.ones((1, worker.height, worker.width), np.float32),
            mask=np.zeros((1, worker.height, worker.width), bool),
        )
        for layer in process.output_layers():
            with io.staged_local_path(process.output_uri(run_id, layer, worker)) as out:
                worker.write(arr, out, driver="COG", overview_resampling="average")


class _FakeBatch:
    """Records submit_job kwargs; fails the given array-child indices on the first job only."""

    def __init__(self, fail_first=()):
        self.submissions = []
        self.fail_first = list(fail_first)

    def submit_job(self, **kwargs):
        self.submissions.append(kwargs)
        return {"jobId": f"job-{len(self.submissions)}"}

    def list_jobs(self, **kwargs):  # FAILED children, round 0 only
        idxs = self.fail_first if len(self.submissions) == 1 else []
        return {"jobSummaryList": [
            {"arrayProperties": {"index": i}, "status": "FAILED", "container": {"exitCode": 1}}
            for i in idxs]}


def _env(submission):
    return {e["name"]: e["value"] for e in submission["containerOverrides"]["environment"]}


# --------------------------------------------------------------------------------------
# Worker — output layout, persist, compute
# --------------------------------------------------------------------------------------
def test_output_layers_are_bii_year_keys():
    layers = process.output_layers()
    assert layers == [f"bii_{y}" for y in config.years()]
    assert "bii_2024" in layers and "abundance_2017" not in layers  # only bii persisted


def test_output_uri_is_deterministic_and_north_west_named(local_out):
    worker = Worker(**_CHUNK)
    uri = process.output_uri("v1", "bii_2020", worker)
    assert uri == f"{local_out}/v1/bii_2020/bii_2020_10.000000_-86.000000.tif"
    assert process.output_uri("v1", "bii_2020", worker) == uri


def test_process_writes_all_layers_unconditionally(local_out, one_year, monkeypatch):
    monkeypatch.setattr(process.model, "compute_all", _stub_compute_all)
    process.process(_CHUNK, run_id="v1")
    worker = Worker(**_CHUNK)
    uris = [process.output_uri("v1", layer, worker) for layer in process.output_layers()]
    assert len(uris) == 1
    for uri in uris:
        valid, errors, _ = cog_validate(uri)
        assert valid, errors
        with rio.open(uri) as src:
            assert src.dtypes[0] == "float32" and src.crs.to_epsg() == 4326

    # The skip lives in run(), not the worker: a rerun recomputes.
    calls = []
    monkeypatch.setattr(process.model, "compute_all", lambda w: calls.append(1) or _stub_compute_all(w))
    process.process(_CHUNK, run_id="v1")
    assert calls == [1]


# --------------------------------------------------------------------------------------
# Driver — manifest build, skip-done, run/retry loop
# --------------------------------------------------------------------------------------
def test_chunk_manifest_keeps_all_chunks_without_coverage_index(local_roots):
    chunks = process.chunk_manifest(_manager(), chunksize=2)
    full = process.chunk_manifest(_manager(), chunksize=2, coverage_assets=())
    assert chunks == full and len(chunks) > 1
    assert all(isinstance(c["proj_bounds"], list) for c in chunks)  # JSON round-trip


def test_chunk_manifest_drops_ocean_chunks_via_coverage(local_roots, one_year):
    mgr = _manager()
    all_chunks = process.chunk_manifest(mgr, chunksize=2, coverage_assets=())
    assert len(all_chunks) >= 2

    # Footprint must be strictly interior to the first chunk: an edge-touching one would also
    # intersect the neighbors sharing that edge.
    covered = all_chunks[0]
    w, s, e, n = mgr.proj.transform_bounds(*covered["proj_bounds"], direction="inverse")
    inset = (e - w) * 0.2
    footprint = (w + inset, s + inset, e - inset, n - inset)
    tile_index.build_index("landcover", [("s3://x/lc.tif", footprint)], year=2020)

    kept = process.chunk_manifest(mgr, chunksize=2, coverage_assets=("landcover",), coverage_year=2020)
    kept_bounds = {tuple(c["proj_bounds"]) for c in kept}
    assert kept_bounds == {tuple(covered["proj_bounds"])}
    assert len(kept) < len(all_chunks)


def test_run_converges_when_no_failures(local_roots, one_year, batch_env):
    fake = _FakeBatch()
    result = process.run(_manager(), run_id="v1", chunksize=2, coverage_assets=(),
                         client=fake, wait_fn=lambda *a, **k: "SUCCEEDED")
    assert result["complete"] and result["failed"] == 0
    assert len(fake.submissions) == 1
    env = _env(fake.submissions[0])
    assert env["BII_RUN_ID"] == "v1" and "BII_MANIFEST" in env
    assert fake.submissions[0]["containerOverrides"]["command"] == ["bii-process"]


def test_run_reports_failed_chunk_without_resubmitting(local_roots, one_year, batch_env):
    fake = _FakeBatch(fail_first=[0])  # FAILED after Batch's own retries
    result = process.run(_manager(), run_id="v1", chunksize=2, coverage_assets=(),
                         client=fake, wait_fn=lambda *a, **k: "FAILED")
    assert not result["complete"] and result["failed"] == 1
    assert len(fake.submissions) == 1


def test_run_skips_chunks_already_written_unless_overwrite(local_roots, one_year, batch_env):
    chunks = process.chunk_manifest(_manager(), chunksize=2, coverage_assets=())
    _write_outputs(chunks[:1], "v1")

    fake = _FakeBatch()
    process.run(_manager(), run_id="v1", chunksize=2, coverage_assets=(),
                client=fake, wait_fn=lambda *a, **k: "SUCCEEDED")
    submitted = orchestration.read_manifest(_env(fake.submissions[0])["BII_MANIFEST"])
    assert chunks[0] not in submitted and len(submitted) == len(chunks) - 1

    fake2 = _FakeBatch()
    process.run(_manager(), run_id="v1", chunksize=2, coverage_assets=(), overwrite=True,
                client=fake2, wait_fn=lambda *a, **k: "SUCCEEDED")
    assert len(orchestration.read_manifest(_env(fake2.submissions[0])["BII_MANIFEST"])) == len(chunks)


def test_run_docker_executor_runs_one_container_per_chunk(local_roots, one_year, monkeypatch):
    calls = []
    monkeypatch.setattr(orchestration.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0, stdout=""))
    chunks = process.chunk_manifest(_manager(), chunksize=2, coverage_assets=())
    result = process.run(_manager(), run_id="v1", chunksize=2, coverage_assets=(), executor="docker")
    # process.py's contribution is wiring run() to the docker executor; the per-container argv is
    # orchestration.run_docker's job (covered in test_orchestration.py).
    assert result["complete"] and len(calls) == len(chunks)


def test_run_no_submit_writes_manifest_only(local_roots, one_year):
    fake = _FakeBatch()
    result = process.run(_manager(), run_id="v1", chunksize=2, coverage_assets=(),
                         submit=False, client=fake)
    assert not result["submitted"] and fake.submissions == []
    assert orchestration.read_manifest(process.manifest_uri("v1")) == \
        process.chunk_manifest(_manager(), chunksize=2, coverage_assets=())


def test_run_empty_manifest_short_circuits(local_roots, one_year):
    # Coverage index overlapping nothing -> empty manifest.
    tile_index.build_index("landcover", [("s3://x/lc.tif", (100.0, 80.0, 101.0, 81.0))], year=2020)
    fake = _FakeBatch()
    result = process.run(_manager(), run_id="v1", chunksize=2, coverage_assets=("landcover",),
                         coverage_year=2020, client=fake, wait_fn=lambda *a, **k: None)
    assert result["n_chunks"] == 0 and result["complete"] and fake.submissions == []
