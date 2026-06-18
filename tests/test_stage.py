"""Unit tests for the staging orchestrator — no network, no AWS, no docker.

The Batch client is a fake recording ``submit_job``; ``docker run`` is a fake recording argv; the
worker dispatch is exercised against a stub dataset module injected into ``MODULES``.
"""

import json

import pytest

from bii import config, stage, stage_worker, tile_index


@pytest.fixture
def local_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STAGED_ROOT", str(tmp_path / "staged"))
    monkeypatch.setattr(config, "OUT_ROOT", str(tmp_path / "out"))


class _StubModule:
    """A dataset module: two units, stage_unit records and returns a finalize-shaped result."""

    ASSET = "fake"

    def __init__(self):
        self.staged = []

    def list_units(self):
        return [{"id": "u1", "dst": "s3://b/fake/u1.tif"},
                {"id": "u2", "dst": "s3://b/fake/u2.tif"}]

    def stage_unit(self, unit):
        self.staged.append(unit["id"])
        return {"asset": self.ASSET, "uri": unit["dst"], "footprint": [0, 0, 1, 1],
                "year": None, "index_part": "part"}


@pytest.fixture
def stub(monkeypatch):
    mod = _StubModule()
    monkeypatch.setitem(stage.MODULES, "fake", mod)
    # consolidate would read parquet parts; stub it to just record the (asset, year) pairs.
    calls = []
    monkeypatch.setattr(tile_index, "consolidate",
                        lambda asset, year=None: calls.append((asset, year)) or f"idx:{asset}")
    mod.consolidated = calls
    return mod


class _FakeBatch:
    def __init__(self, failed_indices=()):
        self.submissions = []
        self._failed = list(failed_indices)

    def submit_job(self, **kwargs):
        self.submissions.append(kwargs)
        return {"jobId": f"job-{len(self.submissions)}"}

    def list_jobs(self, **kwargs):  # array children that ended FAILED
        return {"jobSummaryList": [
            {"arrayProperties": {"index": i}, "status": "FAILED",
             "container": {"exitCode": 1, "reason": "OutOfMemoryError"}}
            for i in self._failed]}


def _items(*specs):
    """specs: (dataset, id, dst, asset) -> manifest items with year=None."""
    return [{"dataset": d, "unit": {"id": i, "dst": dst}, "asset": a, "year": None}
            for d, i, dst, a in specs]


# --------------------------------------------------------------------------------------
# Plan / manifest / worker
# --------------------------------------------------------------------------------------
def test_manifest_items_records_asset_for_consolidation(stub):
    items = stage.manifest_items("fake")
    assert [it["dataset"] for it in items] == ["fake", "fake"]
    assert all(it["asset"] == "fake" and it["year"] is None for it in items)


def test_worker_dispatches_to_module_by_manifest_line(stub, local_roots):
    uri = stage.orchestrate.write_manifest(stage.manifest_items("fake"), stage._manifest_uri("t"))
    result = stage_worker.worker(uri, 1)  # line 1 -> second unit
    assert result["uri"] == "s3://b/fake/u2.tif" and stub.staged == ["u2"]


def test_worker_main_reads_env(stub, local_roots, monkeypatch):
    uri = stage.orchestrate.write_manifest(stage.manifest_items("fake"), stage._manifest_uri("t"))
    monkeypatch.setenv("BII_STAGE_MANIFEST", uri)
    monkeypatch.setenv("AWS_BATCH_JOB_ARRAY_INDEX", "0")
    assert stage_worker.worker_main()["uri"] == "s3://b/fake/u1.tif"


def test_worker_main_requires_manifest(monkeypatch):
    monkeypatch.delenv("BII_STAGE_MANIFEST", raising=False)
    with pytest.raises(SystemExit):
        stage_worker.worker_main()


# --------------------------------------------------------------------------------------
# docker executor
# --------------------------------------------------------------------------------------
def test_run_docker_one_container_per_unit_with_per_image_group(stub, local_roots, monkeypatch):
    monkeypatch.setenv("BII_STAGE_IMAGE", "img")
    monkeypatch.setenv("BII_STAGE_ROADS_IMAGE", "roadsimg")
    calls = []
    monkeypatch.setattr(stage.subprocess, "run", lambda cmd, check: calls.append(cmd))
    items = _items(("fake", "u1", "s3://b/fake/u1.tif", "fake"),
                   ("roads", "r1", "s3://b/roads/r1.tif", "roads"))
    monkeypatch.setattr(stage, "_pending", lambda its: items)  # force both pending

    result = stage.run("fake", executor="docker")

    assert len(calls) == 2 and result["failed"] == []
    # one image group per dataset; the array index is the line *within* the group (mirrors Batch).
    assert "img" in calls[0] and "roadsimg" in calls[1]
    assert "AWS_BATCH_JOB_ARRAY_INDEX=0" in calls[0] and "AWS_BATCH_JOB_ARRAY_INDEX=0" in calls[1]
    assert calls[0][-1] == "bii-stage-worker"


def test_run_docker_continues_past_failure_and_reports_exception(stub, local_roots, monkeypatch):
    def boom(cmd, check):
        raise stage.subprocess.CalledProcessError(2, cmd)
    monkeypatch.setattr(stage.subprocess, "run", boom)
    items = _items(("fake", "u1", "s3://b/fake/u1.tif", "fake"))
    monkeypatch.setattr(stage, "_pending", lambda its: items)

    result = stage.run("fake", executor="docker")

    assert [f["id"] for f in result["failed"]] == ["u1"]
    assert "non-zero exit status 2" in result["failed"][0]["error"]
    # failed unit -> its asset is reported incomplete, not consolidated.
    assert result["incomplete_indexes"] == [("fake", None)] and result["indexes"] == []


# --------------------------------------------------------------------------------------
# batch executor
# --------------------------------------------------------------------------------------
def test_submit_array_overrides_command_and_env(local_roots):
    fake = _FakeBatch()
    stage.submit_array("s3://b/stage/main.jsonl", 4, "q", "jd", "bii-stage-main", client=fake)
    (kw,) = fake.submissions
    assert kw["arrayProperties"] == {"size": 4} and kw["jobDefinition"] == "jd"
    assert kw["containerOverrides"]["command"] == ["bii-stage-worker"]
    env = {e["name"]: e["value"] for e in kw["containerOverrides"]["environment"]}
    assert env["BII_STAGE_MANIFEST"] == "s3://b/stage/main.jsonl"


def test_submit_array_single_is_not_array():
    fake = _FakeBatch()
    stage.submit_array("m", 1, "q", "jd", "n", client=fake)
    assert "arrayProperties" not in fake.submissions[0]


def test_run_batch_submits_one_job_per_image_group(stub, local_roots, monkeypatch):
    monkeypatch.setenv("BII_BATCH_QUEUE", "q")
    monkeypatch.setenv("BII_BATCH_JOB_DEF", "raster-jd")
    monkeypatch.setenv("BII_BATCH_ROADS_JOB_DEF", "roads-jd")
    fake = _FakeBatch()
    items = _items(("fake", "u1", "s3://b/fake/u1.tif", "fake"),
                   ("fake", "u2", "s3://b/fake/u2.tif", "fake"),
                   ("roads", "r1", "s3://b/roads/r1.tif", "roads"))
    monkeypatch.setattr(stage, "_pending", lambda its: items)

    stage.run("fake", executor="batch", client=fake, wait_fn=lambda *a, **k: "SUCCEEDED")

    defs = [kw["jobDefinition"] for kw in fake.submissions]
    assert defs == ["raster-jd", "roads-jd"]  # one array per image group: main then roads
    # landcover would be index-in-place; here both assets consolidate.
    assert ("fake", None) in stub.consolidated and ("roads", None) in stub.consolidated


def test_run_batch_reports_failed_children_and_skips_their_index(stub, local_roots, monkeypatch):
    monkeypatch.setenv("BII_BATCH_QUEUE", "q")
    monkeypatch.setenv("BII_BATCH_JOB_DEF", "jd")
    fake = _FakeBatch(failed_indices=[1])  # second unit's child ended FAILED
    items = _items(("fake", "u1", "s3://b/fake/u1.tif", "fake"),
                   ("fake", "u2", "s3://b/fake/u2.tif", "fake"))
    monkeypatch.setattr(stage, "_pending", lambda its: items)

    result = stage.run("fake", executor="batch", client=fake, wait_fn=lambda *a, **k: "FAILED")

    assert [f["id"] for f in result["failed"]] == ["u2"]
    assert result["failed"][0]["error"] == "FAILED: exit 1: OutOfMemoryError"
    # asset has an incomplete footprint -> not consolidated, reported instead.
    assert result["incomplete_indexes"] == [("fake", None)]
    assert stub.consolidated == [] and result["indexes"] == []


def test_run_batch_requires_roads_job_def_when_roads_present(stub, local_roots, monkeypatch):
    monkeypatch.setenv("BII_BATCH_QUEUE", "q")
    monkeypatch.setenv("BII_BATCH_JOB_DEF", "jd")
    monkeypatch.delenv("BII_BATCH_ROADS_JOB_DEF", raising=False)
    items = _items(("roads", "r1", "s3://b/roads/r1.tif", "roads"))
    monkeypatch.setattr(stage, "_pending", lambda its: items)
    with pytest.raises(SystemExit):
        stage.run("fake", executor="batch", client=_FakeBatch(), wait_fn=lambda *a, **k: "SUCCEEDED")


def test_run_skips_when_nothing_pending(stub, local_roots, monkeypatch):
    monkeypatch.setattr(stage, "_pending", lambda its: [])
    result = stage.run("fake", executor="batch")
    assert result["pending"] == 0 and result["indexes"] == []
