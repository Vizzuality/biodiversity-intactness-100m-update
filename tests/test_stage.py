"""Unit tests for the staging driver — no network, no AWS, no docker.

The Batch client is a fake recording ``submit_job``; ``docker run`` is a fake recording argv; the
worker dispatch is exercised against a stub dataset module injected into ``MODULES``.
"""

from types import SimpleNamespace

import pytest

from bii import config, orchestration, io, stage


class _StubModule:
    """A dataset module: two units, stage_unit records the id and reports it produced output."""

    ASSET = "fake"

    def __init__(self):
        self.staged = []

    def list_units(self):
        return [{"id": "u1", "dst": "s3://b/fake/u1.tif"},
                {"id": "u2", "dst": "s3://b/fake/u2.tif"}]

    def stage_unit(self, unit):
        self.staged.append(unit["id"])
        return True


@pytest.fixture
def stub(monkeypatch):
    mod = _StubModule()
    monkeypatch.setitem(stage.MODULES, "fake", mod)
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


def test_stage_dispatches_to_module_by_manifest_line(stub):
    stage.stage(stage.manifest_items("fake")[1])  # second unit
    assert stub.staged == ["u2"]


def test_stage_writes_empty_marker_when_unit_produces_nothing(local_roots, monkeypatch):
    dst = config.staged_uri("fake", "u1.tif")
    monkeypatch.setitem(stage.MODULES, "fake",
                        SimpleNamespace(ASSET="fake", stage_unit=lambda unit: False))
    stage.stage({"dataset": "fake", "unit": {"id": "u1", "dst": dst}, "asset": "fake", "year": None})
    assert io.exists(dst + stage.EMPTY_MARKER) and not io.exists(dst)


def test_pending_skips_unit_whose_empty_marker_exists(local_roots):
    dst1, dst2 = config.staged_uri("fake", "u1.tif"), config.staged_uri("fake", "u2.tif")
    io.put_bytes(b"", dst1 + stage.EMPTY_MARKER)  # u1 staged nothing last run -> marker only
    pending = stage._pending(_items(("fake", "u1", dst1, "fake"), ("fake", "u2", dst2, "fake")))
    assert [it["unit"]["id"] for it in pending] == ["u2"]


def test_stage_reads_the_indexed_unit_from_env(stub, local_roots, monkeypatch):
    uri = orchestration.write_manifest(stage.manifest_items("fake"), stage._manifest_uri())
    monkeypatch.setenv("BII_MANIFEST", uri)
    monkeypatch.setenv("AWS_BATCH_JOB_ARRAY_INDEX", "0")
    stage.stage()  # no arg -> reads line 0 from the manifest
    assert stub.staged == ["u1"]


# --------------------------------------------------------------------------------------
# docker executor
# --------------------------------------------------------------------------------------
def test_run_docker_one_container_per_unit(stub, local_roots, monkeypatch):
    monkeypatch.setenv("BII_STAGE_IMAGE", "img")
    calls = []
    monkeypatch.setattr(orchestration.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0, stdout=""))
    items = _items(("fake", "u1", "s3://b/fake/u1.tif", "fake"),
                   ("roads", "r1", "s3://b/roads/r1.tif", "roads"))
    monkeypatch.setattr(stage, "_pending", lambda its: items)  # force both pending

    result = stage.run("fake", executor="docker")

    assert result["failed"] == []
    # one image for all units; the array index is the manifest line (mirrors Batch).
    assert "img" in calls[0] and "img" in calls[1]
    assert "AWS_BATCH_JOB_ARRAY_INDEX=0" in calls[0] and "AWS_BATCH_JOB_ARRAY_INDEX=1" in calls[1]
    assert calls[0][-1] == "bii-stage"
    # two staging containers, then one bii-index container per consolidated (asset, year).
    assert [c[-1] for c in calls] == ["bii-stage", "bii-stage", "bii-index", "bii-index"]
    assert result["indexes"] == [("fake", None), ("roads", None)]


def test_run_docker_continues_past_failure_and_reports_exception(stub, local_roots, monkeypatch):
    monkeypatch.setattr(orchestration.subprocess, "run",
                        lambda cmd, **kw: SimpleNamespace(returncode=2, stdout=""))
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
def test_run_batch_submits_one_array_for_all_units(stub, local_roots, monkeypatch):
    monkeypatch.setenv("BII_BATCH_QUEUE", "q")
    monkeypatch.setenv("BII_BATCH_JOB_DEF", "jd")
    fake = _FakeBatch()
    items = _items(("fake", "u1", "s3://b/fake/u1.tif", "fake"),
                   ("fake", "u2", "s3://b/fake/u2.tif", "fake"),
                   ("roads", "r1", "s3://b/roads/r1.tif", "roads"))
    monkeypatch.setattr(stage, "_pending", lambda its: items)

    result = stage.run("fake", executor="batch", client=fake, wait_fn=lambda *a, **k: "SUCCEEDED")

    # one staging array for every unit (roads included), then one index array for the assets.
    cmds = [kw["containerOverrides"]["command"] for kw in fake.submissions]
    assert cmds == [["bii-stage"], ["bii-index"]]
    assert all(kw["jobDefinition"] == "jd" for kw in fake.submissions)
    # landcover would be index-in-place; here both assets consolidate.
    assert result["indexes"] == [("fake", None), ("roads", None)]


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
    assert result["indexes"] == []
    assert len(fake.submissions) == 1  # only the staging array; no index array dispatched


def test_reindex_dispatches_index_only_without_staging(stub, local_roots, monkeypatch):
    calls = []
    monkeypatch.setattr(orchestration.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0, stdout=""))

    result = stage.reindex("fake", executor="docker")

    # no bii-stage containers; one bii-index container per (asset, year), no staging.
    assert [c[-1] for c in calls] == ["bii-index"]
    assert result["indexes"] == [("fake", None)] and result["failed"] == []


def test_run_skips_when_nothing_pending(stub, local_roots, monkeypatch):
    monkeypatch.setattr(stage, "_pending", lambda its: [])
    result = stage.run("fake", executor="batch")
    assert result["pending"] == 0 and result["indexes"] == []
