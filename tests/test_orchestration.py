"""Unit tests for the shared docker/Batch executors — no network, no AWS, no docker.

The Batch client is a fake recording ``submit_job``; ``docker run`` is a fake recording argv.
"""

from types import SimpleNamespace

import pytest

from bii import config, orchestration


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


def _items(n):
    return [{"id": i} for i in range(n)]


# --------------------------------------------------------------------------------------
# Manifest I/O
# --------------------------------------------------------------------------------------
def test_write_read_manifest_round_trips(local_roots):
    items = [{"a": 1, "proj_bounds": [1.0, 2.0]}, {"a": 2}]
    uri = orchestration.write_manifest(items, config.out_uri("m", "x.jsonl"))
    assert orchestration.read_manifest(uri) == items


# --------------------------------------------------------------------------------------
# Batch submit
# --------------------------------------------------------------------------------------
def test_submit_array_builds_array_job_with_command_env_and_retry(monkeypatch):
    monkeypatch.setenv("BII_BATCH_QUEUE", "q")
    monkeypatch.setenv("BII_BATCH_JOB_DEF", "jd")
    fake = _FakeBatch()
    job_id = orchestration.submit_array(
        size=5, job_name="n", command=["bii-stage-worker"],
        environment={"BII_STAGE_MANIFEST": "s3://b/m.jsonl"}, client=fake)
    assert job_id == "job-1"
    (kw,) = fake.submissions
    assert kw["arrayProperties"] == {"size": 5}
    assert kw["jobQueue"] == "q" and kw["jobDefinition"] == "jd"
    assert kw["retryStrategy"]["attempts"] == 3
    assert kw["containerOverrides"]["command"] == ["bii-stage-worker"]
    env = {e["name"]: e["value"] for e in kw["containerOverrides"]["environment"]}
    assert env["BII_STAGE_MANIFEST"] == "s3://b/m.jsonl"


def test_submit_array_single_index_is_not_an_array_job():
    fake = _FakeBatch()
    orchestration.submit_array(size=1, job_name="n", job_queue="q", job_definition="jd", client=fake)
    assert "arrayProperties" not in fake.submissions[0]


def test_submit_array_requires_queue_and_definition(monkeypatch):
    monkeypatch.delenv("BII_BATCH_QUEUE", raising=False)
    monkeypatch.delenv("BII_BATCH_JOB_DEF", raising=False)
    with pytest.raises(SystemExit):
        orchestration.submit_array(size=2, job_name="n", client=_FakeBatch())


# --------------------------------------------------------------------------------------
# Docker executor
# --------------------------------------------------------------------------------------
def test_run_docker_one_container_per_line(monkeypatch):
    monkeypatch.setenv("BII_STAGE_IMAGE", "img")
    calls = []
    monkeypatch.setattr(orchestration.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0, stdout=""))
    failed = orchestration.run_docker(_items(2), ["bii-stage-worker"], manifest_uri="m",
                                      manifest_env="BII_STAGE_MANIFEST")
    assert failed == []
    assert len(calls) == 2 and "img" in calls[0] and calls[0][-1] == "bii-stage-worker"
    # the array index is the manifest line (mirrors Batch).
    assert "AWS_BATCH_JOB_ARRAY_INDEX=0" in calls[0] and "AWS_BATCH_JOB_ARRAY_INDEX=1" in calls[1]
    assert "BII_STAGE_MANIFEST=m" in calls[0]


def test_run_docker_continues_past_failure_and_reports_index(monkeypatch):
    monkeypatch.setattr(orchestration.subprocess, "run",
                        lambda cmd, **kw: SimpleNamespace(returncode=2, stdout="boom"))
    failed = orchestration.run_docker(_items(2), ["bii-stage-worker"], manifest_uri="m",
                                      manifest_env="BII_STAGE_MANIFEST")
    assert [f["index"] for f in failed] == [0, 1]
    # the failure error is the tail of the container's combined output.
    assert failed[0]["error"] == "boom"


# --------------------------------------------------------------------------------------
# Batch executor
# --------------------------------------------------------------------------------------
def test_run_batch_success_returns_no_failures(monkeypatch):
    monkeypatch.setenv("BII_BATCH_QUEUE", "q")
    monkeypatch.setenv("BII_BATCH_JOB_DEF", "jd")
    fake = _FakeBatch()
    failed = orchestration.run_batch(_items(3), ["bii-stage-worker"], manifest_uri="m",
                                     manifest_env="BII_STAGE_MANIFEST", job_name="bii-stage",
                                     client=fake, wait_fn=lambda *a, **k: "SUCCEEDED")
    assert failed == []
    assert fake.submissions[0]["arrayProperties"] == {"size": 3}


def test_run_batch_reports_failed_children_by_index(monkeypatch):
    monkeypatch.setenv("BII_BATCH_QUEUE", "q")
    monkeypatch.setenv("BII_BATCH_JOB_DEF", "jd")
    fake = _FakeBatch(failed_indices=[1])
    failed = orchestration.run_batch(_items(2), ["bii-stage-worker"], manifest_uri="m",
                                     manifest_env="BII_STAGE_MANIFEST", job_name="bii-stage",
                                     client=fake, wait_fn=lambda *a, **k: "FAILED")
    assert [f["index"] for f in failed] == [1]
    assert failed[0]["error"] == "FAILED: exit 1: OutOfMemoryError"


def test_run_batch_single_failed_job_is_index_zero(monkeypatch):
    monkeypatch.setenv("BII_BATCH_QUEUE", "q")
    monkeypatch.setenv("BII_BATCH_JOB_DEF", "jd")
    failed = orchestration.run_batch(_items(1), ["bii-stage-worker"], manifest_uri="m",
                                     manifest_env="BII_STAGE_MANIFEST", job_name="bii-stage",
                                     client=_FakeBatch(), wait_fn=lambda *a, **k: "FAILED")
    assert failed == [{"index": 0, "error": "batch job failed"}]
