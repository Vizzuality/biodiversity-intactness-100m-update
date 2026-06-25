"""Unit tests for the processing-run driver — no network, no AWS.

The Batch boto3 client is replaced with a fake that records ``submit_job`` calls, and the
verify/retry loop is driven with a synchronous ``wait_fn`` that materializes outputs on disk.
Manifest build (ocean drop), the output diff, and the retry-until-empty loop are exercised end
to end against a local staged + output root.
"""

import numpy as np
import pytest
from cog_worker import Manager, Worker
from shapely.geometry import box

from bii import config, orchestrate, orchestration, process, tile_index

# A small land region (Costa Rica-ish) and a coarse scale so a Manager yields a handful of chunks.
_BOUNDS = (-86.0, 9.0, -84.0, 11.0)
_SCALE = 0.5  # ~0.5 deg pixels -> few-pixel chunks at chunksize below


@pytest.fixture
def one_year(monkeypatch):
    monkeypatch.setattr(config, "START_YEAR", 2020)
    monkeypatch.setattr(config, "END_YEAR", 2020)


@pytest.fixture
def batch_env(monkeypatch):
    monkeypatch.setenv("BII_BATCH_QUEUE", "q")
    monkeypatch.setenv("BII_BATCH_JOB_DEF", "jd")


def _manager():
    return Manager(bounds=_BOUNDS, scale=_SCALE, proj="EPSG:4326", buffer=0)


class _FakeBatch:
    """Records submit_job kwargs and hands back incrementing job ids."""

    def __init__(self):
        self.submissions = []

    def submit_job(self, **kwargs):
        self.submissions.append(kwargs)
        return {"jobId": f"job-{len(self.submissions)}"}


def _write_outputs(chunks, run_id):
    """Materialize every expected output COG for ``chunks`` (stands in for a Batch run)."""
    for chunk in chunks:
        worker = Worker(**chunk)
        arr = np.ma.MaskedArray(
            np.ones((1, worker.height, worker.width), np.float32),
            mask=np.zeros((1, worker.height, worker.width), bool),
        )
        for layer in process.output_layers():
            process.persist_cog(worker, arr, process.output_uri(run_id, layer, worker))


# --------------------------------------------------------------------------------------
# Manifest naming
# --------------------------------------------------------------------------------------
def test_manifest_uri_names_retry_rounds(local_roots):
    assert orchestrate.manifest_uri("v1", 0).endswith("/v1/chunks.jsonl")
    assert orchestrate.manifest_uri("v1", 2).endswith("/v1/chunks_retry2.jsonl")


# --------------------------------------------------------------------------------------
# Manifest build — ocean drop
# --------------------------------------------------------------------------------------
def test_chunk_manifest_keeps_all_chunks_without_coverage_index(local_roots):
    # No staged coverage index exists -> keep every finite chunk (don't drop the globe).
    chunks = orchestrate.chunk_manifest(_manager(), chunksize=2)
    full = orchestrate.chunk_manifest(_manager(), chunksize=2, coverage_assets=())
    assert chunks == full and len(chunks) > 1
    # proj_bounds round-trips as a JSON list.
    assert all(isinstance(c["proj_bounds"], list) for c in chunks)


def test_chunk_manifest_drops_ocean_chunks_via_coverage(local_roots, one_year):
    mgr = _manager()
    all_chunks = orchestrate.chunk_manifest(mgr, chunksize=2, coverage_assets=())
    assert len(all_chunks) >= 2

    # Coverage footprint strictly *interior* to the first chunk, so it doesn't touch the edges
    # adjacent chunks share (an edge-touching footprint would intersect every neighbor too).
    covered = all_chunks[0]
    w, s, e, n = mgr.proj.transform_bounds(*covered["proj_bounds"], direction="inverse")
    inset = (e - w) * 0.2
    footprint = (w + inset, s + inset, e - inset, n - inset)
    tile_index.build_index("landcover", [("s3://x/lc.tif", footprint)], year=2020)

    kept = orchestrate.chunk_manifest(
        mgr, chunksize=2, coverage_assets=("landcover",), coverage_year=2020
    )
    kept_bounds = {tuple(c["proj_bounds"]) for c in kept}
    assert kept_bounds == {tuple(covered["proj_bounds"])}  # only the covered chunk survives
    assert len(kept) < len(all_chunks)


# --------------------------------------------------------------------------------------
# Verify diff
# --------------------------------------------------------------------------------------
def test_missing_chunks_detects_partial_and_full_completion(local_roots, one_year):
    chunks = orchestrate.chunk_manifest(_manager(), chunksize=2, coverage_assets=())
    assert orchestrate.missing_chunks(chunks, "v1") == chunks  # nothing written yet

    _write_outputs(chunks[:1], "v1")  # complete only the first chunk
    missing = orchestrate.missing_chunks(chunks, "v1")
    assert chunks[0] not in missing and chunks[1] in missing

    _write_outputs(chunks, "v1")
    assert orchestrate.missing_chunks(chunks, "v1") == []


def test_missing_when_one_layer_absent(local_roots, one_year):
    chunk = orchestrate.chunk_manifest(_manager(), chunksize=2, coverage_assets=())[0]
    worker = Worker(**chunk)
    arr = np.ma.MaskedArray(
        np.ones((1, worker.height, worker.width), np.float32),
        mask=np.zeros((1, worker.height, worker.width), bool),
    )
    layers = process.output_layers()
    for layer in layers[:-1]:  # write all but one layer
        process.persist_cog(worker, arr, process.output_uri("v1", layer, worker))
    assert orchestrate.missing_chunks([chunk], "v1") == [chunk]


# --------------------------------------------------------------------------------------
# Driver loop
# --------------------------------------------------------------------------------------
def test_run_converges_when_outputs_appear(local_roots, one_year, batch_env):
    fake = _FakeBatch()

    def wait_fn(job_id, *, client=None):
        _write_outputs(read_for(fake), "v1")

    def read_for(_fake):
        # The round's manifest is the most recent submission's BII_CHUNKS_URI.
        env = {e["name"]: e["value"] for e in _fake.submissions[-1]["containerOverrides"]["environment"]}
        return orchestration.read_manifest(env["BII_CHUNKS_URI"])

    result = orchestrate.run(
        _manager(), run_id="v1", chunksize=2, coverage_assets=(), client=fake, wait_fn=wait_fn
    )
    assert result["complete"] and result["missing"] == 0
    assert len(result["rounds"]) == 1  # one round suffices since wait writes everything
    assert len(fake.submissions) == 1


def test_run_retries_then_converges(local_roots, one_year, batch_env):
    fake = _FakeBatch()
    state = {"round": 0}

    def wait_fn(job_id, *, client=None):
        env = {e["name"]: e["value"] for e in fake.submissions[-1]["containerOverrides"]["environment"]}
        manifest = orchestration.read_manifest(env["BII_CHUNKS_URI"])
        # Round 0: only finish all-but-one chunk, forcing exactly one retry round.
        finish = manifest if state["round"] else manifest[:-1]
        _write_outputs(finish, "v1")
        state["round"] += 1

    result = orchestrate.run(
        _manager(), run_id="v1", chunksize=2, coverage_assets=(), client=fake, wait_fn=wait_fn,
        max_rounds=5,
    )
    assert result["complete"] and result["missing"] == 0
    assert len(result["rounds"]) == 2
    assert result["rounds"][0]["missing"] == 1
    # The retry round resubmitted only the single missing chunk (size 1 -> non-array job).
    assert "arrayProperties" not in fake.submissions[1]
    retry_env = {e["name"]: e["value"] for e in fake.submissions[1]["containerOverrides"]["environment"]}
    full_manifest = orchestration.read_manifest(orchestrate.manifest_uri("v1", 0))
    assert orchestration.read_manifest(retry_env["BII_CHUNKS_URI"]) == full_manifest[-1:]


def test_run_empty_manifest_short_circuits(local_roots, one_year):
    # Coverage index that overlaps nothing -> empty manifest -> no submission.
    tile_index.build_index("landcover", [("s3://x/lc.tif", (100.0, 80.0, 101.0, 81.0))], year=2020)
    fake = _FakeBatch()
    result = orchestrate.run(
        _manager(), run_id="v1", chunksize=2, coverage_assets=("landcover",),
        coverage_year=2020, client=fake, wait_fn=lambda *a, **k: None,
    )
    assert result["n_chunks"] == 0 and result["complete"] and fake.submissions == []
