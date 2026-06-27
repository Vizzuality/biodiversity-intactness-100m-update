"""Job orechestration in local docker or AWS Batch array job

Write/read a JSONL manifest of jobs, one container per line.
Local docker run provides parity with the Batch execution environment for testing.
Failures as ``{"index", "error"}``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from . import io

_POLL_SECONDS = 30.0
MANIFEST_ENV = "BII_MANIFEST"


def write_manifest(items: list[dict], uri: str) -> str:
    io.put_bytes("".join(json.dumps(it) + "\n" for it in items).encode(), uri)
    return uri


def read_manifest(uri: str) -> list[dict]:
    return [json.loads(ln) for ln in io.read_text(uri).splitlines() if ln.strip()]


def _aws_creds() -> dict:
    """AWS_* env to hand a container: the active session's frozen credentials plus region. 
    Resolving via boto3 yields temporary keys an assume-role/SSO profile."""
    import boto3

    session = boto3.Session()
    env = {}
    if session.region_name:
        env["AWS_REGION"] = env["AWS_DEFAULT_REGION"] = session.region_name
    creds = session.get_credentials()
    if creds:
        f = creds.get_frozen_credentials()
        env |= {"AWS_ACCESS_KEY_ID": f.access_key, "AWS_SECRET_ACCESS_KEY": f.secret_key}
        if f.token:
            env["AWS_SESSION_TOKEN"] = f.token
    return env


def docker_run(image: str, command: list[str], *, env: dict | None = None,
               store: str | None = None, creds: dict | None = None) -> None:
    """One ``docker run --rm`` mirroring a Batch job. ``store`` stands in for the S3 store —
    bind-mounted at the same absolute path and pointed at by ``BII_STAGED_ROOT`` / ``BII_OUT_ROOT``."""
    args = ["docker", "run", "--rm"]
    if store:
        args += ["-v", f"{store}:{store}"]
        env = {"BII_STAGED_ROOT": store, "BII_OUT_ROOT": store, **(env or {})}
    creds = _aws_creds() if creds is None else creds
    args += [a for k in creds for a in ("-e", k)]  # -e NAME (not NAME=value): keeps secrets off argv/ps
    args += [a for k, v in (env or {}).items() for a in ("-e", f"{k}={v}")]
    args += [image, *command]
    proc = subprocess.run(args, env={**os.environ, **creds}, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)
    sys.stdout.write(proc.stdout)
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, args, output=proc.stdout)


def run_docker(items: list[dict], command: list[str], *, manifest_uri: str, env: dict | None = None,
               store: str | None = None, image: str | None = None, label=None) -> list[dict]:
    """Run ``command`` in one ``docker run`` per manifest line — the local mirror of the Batch array.
    Image defaults to ``BII_STAGE_IMAGE``. Continues past a non-zero exit; returns
    ``{"index", "error"}`` per failure."""
    image = image or os.environ.get("BII_STAGE_IMAGE", "bii")
    creds = _aws_creds()
    n = len(items)
    failed: list[dict] = []
    for i, it in enumerate(items):
        if label:
            print(f"[{i + 1}/{n}] {label(it)}", file=sys.stderr)
        try:
            docker_run(image, command, store=store, creds=creds,
                       env={MANIFEST_ENV: manifest_uri, "AWS_BATCH_JOB_ARRAY_INDEX": i, **(env or {})})
        except subprocess.CalledProcessError as exc:
            tail = "\n".join((exc.output or "").strip().splitlines()[-15:]) or str(exc)
            failed.append({"index": i, "error": tail})
    return failed


def batch_client(client=None):
    if client is not None:
        return client
    import boto3

    return boto3.client("batch")


def submit_array(*, size: int, job_name: str, command: list[str] | None = None,
                 environment: dict | None = None, job_queue: str | None = None,
                 job_definition: str | None = None, attempts: int = 3, client=None) -> str:
    """Submit a manifest as a Batch array job; return the job id. ``size == 1`` submits a plain
    non-array job (Batch arrays need >= 2). ``attempts`` drives the Spot-friendly ``retryStrategy``.
    Queue/definition fall back to ``BII_BATCH_QUEUE`` / ``BII_BATCH_JOB_DEF``."""
    job_queue = job_queue or os.environ.get("BII_BATCH_QUEUE")
    job_definition = job_definition or os.environ.get("BII_BATCH_JOB_DEF")
    if not job_queue or not job_definition:
        raise SystemExit("set BII_BATCH_QUEUE and BII_BATCH_JOB_DEF (or pass job_queue/job_definition)")

    overrides: dict = {}
    if command:
        overrides["command"] = command
    if environment:
        overrides["environment"] = [{"name": k, "value": str(v)} for k, v in environment.items()]
    kwargs = dict(jobName=job_name, jobQueue=job_queue, jobDefinition=job_definition,
                  containerOverrides=overrides, retryStrategy={"attempts": attempts})
    if size > 1:
        kwargs["arrayProperties"] = {"size": size}
    return batch_client(client).submit_job(**kwargs)["jobId"]


# Batch array child states ordered submitted -> done, for a stable progress summary.
_ARRAY_STATES = ("SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING", "SUCCEEDED", "FAILED")


def wait_for_array(job_id: str, *, client=None) -> str:
    """Poll Batch until ``job_id`` is SUCCEEDED or FAILED; return that state."""
    client = batch_client(client)
    while True:
        job = client.describe_jobs(jobs=[job_id])["jobs"][0]
        summary = job.get("arrayProperties", {}).get("statusSummary") or {}
        if summary:
            print("  " + "  ".join(f"{s}={summary[s]}" for s in _ARRAY_STATES if summary.get(s)),
                  file=sys.stderr)
        status = job.get("status", "")
        if status in ("SUCCEEDED", "FAILED"):
            return status
        time.sleep(_POLL_SECONDS)


def terminate_job(job_id: str, *, client=None, reason: str = "orchestrator interrupted") -> None:
    """Terminate ``job_id`` so an interrupted orchestrator doesn't leave the array job spending."""
    batch_client(client).terminate_job(jobId=job_id, reason=reason)


def failed_children(job_id: str, client=None) -> dict[int, str]:
    """``{array-child index: failure detail}`` for children of ``job_id`` that ended FAILED after
    retries."""
    client = batch_client(client)
    out: dict[int, str] = {}
    token = None
    while True:
        kw = {"arrayJobId": job_id, "jobStatus": "FAILED"}
        if token:
            kw["nextToken"] = token
        resp = client.list_jobs(**kw)
        for j in resp.get("jobSummaryList", []):
            idx = j.get("arrayProperties", {}).get("index")
            if idx is None:
                continue
            c = j.get("container") or {}
            parts = [j.get("status", "FAILED")]
            if c.get("exitCode") is not None:
                parts.append(f"exit {c['exitCode']}")
            if c.get("reason") or j.get("statusReason"):
                parts.append(c.get("reason") or j.get("statusReason"))
            out[idx] = ": ".join(parts)
        token = resp.get("nextToken")
        if not token:
            return out


def run_batch(items: list[dict], command: list[str], *, manifest_uri: str, env: dict | None = None,
              job_name: str, client=None, wait_fn=None) -> list[dict]:
    """Submit the manifest as one Batch array job running ``command``, wait, and return the lines
    whose child ended FAILED after retries (``{"index", "error"}``). An interrupt while waiting
    terminates the job first."""
    wait_fn = wait_fn or wait_for_array
    job_id = submit_array(size=len(items), job_name=job_name, command=command,
                          environment={MANIFEST_ENV: manifest_uri, **(env or {})}, client=client)
    try:
        status = wait_fn(job_id, client=client)
    except BaseException:
        terminate_job(job_id, client=client)
        raise
    if status == "FAILED":
        detail = {0: "batch job failed"} if len(items) == 1 else failed_children(job_id, client)
        return [{"index": i, "error": detail[i]} for i in sorted(detail)]
    return []


def run_manifest(items: list[dict], command: list[str], *, executor: str, manifest_uri: str,
                 job_name: str, env: dict | None = None, store: str | None = None, label=None,
                 client=None, wait_fn=None) -> list[dict]:
    """Write ``items`` to ``manifest_uri`` and run ``command`` over them via ``executor``
    (``docker`` / ``batch``); return the failed lines. ``store`` and ``label`` apply to docker only."""
    write_manifest(items, manifest_uri)
    if executor == "docker":
        return run_docker(items, command, manifest_uri=manifest_uri, env=env, store=store, label=label)
    if executor == "batch":
        return run_batch(items, command, manifest_uri=manifest_uri, env=env, job_name=job_name,
                         client=client, wait_fn=wait_fn)
    raise SystemExit(f"unknown executor {executor!r} (docker | batch)")


def manifest_line() -> dict:
    """The manifest line this container handles — line ``AWS_BATCH_JOB_ARRAY_INDEX`` of the manifest."""
    manifest = os.environ.get(MANIFEST_ENV)
    if not manifest:
        raise SystemExit(f"{MANIFEST_ENV} must point at the manifest")
    index = int(os.environ.get("AWS_BATCH_JOB_ARRAY_INDEX", "0"))
    return read_manifest(manifest)[index]
