"""Shared executors: run a JSONL manifest as one container per line, locally or on AWS Batch.

The staging and processing fan-outs both reduce to the same contract — a manifest where array index
N is line N, dispatched to a worker command — so the executors live here and the two drivers
(:mod:`bii.stage`, :mod:`bii.process`) stay dataset/metric specific. Two executors run the same
per-line work over the same manifest, so a local docker run exercises exactly what Batch will:

* ``run_docker`` — one ``docker run`` per line (test the image locally).
* ``run_batch``  — submit the manifest as an AWS Batch array job.

:func:`run_manifest` is the one entry both drivers call: write the manifest, dispatch it to the
chosen executor, return the failed lines. :func:`manifest_line` is the other side — a worker
container reads line ``AWS_BATCH_JOB_ARRAY_INDEX`` of the manifest at ``BII_MANIFEST``.

Batch infra is deployment-specific: queue/definition come from ``BII_BATCH_QUEUE`` /
``BII_BATCH_JOB_DEF`` (or explicit args). The boto3 Batch client and the wait step are injectable
for testing. Both executors report failures as ``{"index", "error"}`` so the driver can map a failed
line back to its source item.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from . import s3io

_POLL_SECONDS = 30.0
# Env var naming the manifest URI inside a worker container; the array index selects the line.
MANIFEST_ENV = "BII_MANIFEST"


# --------------------------------------------------------------------------------------
# Manifest JSONL I/O (reuses s3io's S3/local byte helpers)
# --------------------------------------------------------------------------------------
def write_manifest(items: list[dict], uri: str) -> str:
    """Write ``items`` as JSONL (one dict per line) to ``uri`` (S3 or local)."""
    s3io.put_bytes("".join(json.dumps(it) + "\n" for it in items).encode(), uri)
    return uri


def read_manifest(uri: str) -> list[dict]:
    return [json.loads(ln) for ln in s3io.read_text(uri).splitlines() if ln.strip()]


# --------------------------------------------------------------------------------------
# Local docker executor
# --------------------------------------------------------------------------------------
def _aws_creds() -> dict:
    """AWS_* env to hand a container: the active session's frozen credentials plus region. boto3
    resolves whatever the host uses (an assume-role profile, SSO, env keys, instance profile), so a
    profile that assumes a role yields temporary keys the container can use — unlike a bare
    AWS_PROFILE, which is inert without ~/.aws or the source credentials inside the container."""
    import boto3  # lazy: unit tests / docker-less paths don't need credentials

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
    """One ``docker run --rm`` mirroring a Batch job: forward the host's AWS credentials (``creds``,
    defaulting to :func:`_aws_creds`) and set ``env`` (the job-definition environment). ``store`` is
    the local stand-in for the S3 store — bind-mounted at the same absolute path and pointed at by
    ``BII_STAGED_ROOT`` / ``BII_OUT_ROOT``, so the container reads the manifest and writes its COGs to it."""
    args = ["docker", "run", "--rm"]
    if store:
        args += ["-v", f"{store}:{store}"]
        env = {"BII_STAGED_ROOT": store, "BII_OUT_ROOT": store, **(env or {})}
    creds = _aws_creds() if creds is None else creds
    args += [a for k in creds for a in ("-e", k)]  # -e NAME (not NAME=value): keeps secrets off argv/ps
    args += [a for k, v in (env or {}).items() for a in ("-e", f"{k}={v}")]
    args += [image, *command]
    # Capture combined output so a failing unit's traceback survives in the run report; echo it
    # through once the unit finishes (the container output never streamed live anyway).
    proc = subprocess.run(args, env={**os.environ, **creds}, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)
    sys.stdout.write(proc.stdout)
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, args, output=proc.stdout)


def run_docker(items: list[dict], command: list[str], *, manifest_uri: str, env: dict | None = None,
               store: str | None = None, image: str | None = None, label=None) -> list[dict]:
    """Run ``command`` in one ``docker run`` per manifest line — the local mirror of the Batch array.
    The image is ``BII_STAGE_IMAGE`` (default ``bii``); each container reads line
    ``AWS_BATCH_JOB_ARRAY_INDEX`` of the manifest at ``manifest_uri`` (``BII_MANIFEST``). ``env`` is
    extra container environment (e.g. the processing run id). Continues past a container that exits
    non-zero; returns ``{"index", "error"}`` per failure."""
    image = image or os.environ.get("BII_STAGE_IMAGE", "bii")
    creds = _aws_creds()  # resolve the host session once, not per container
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


# --------------------------------------------------------------------------------------
# AWS Batch executor
# --------------------------------------------------------------------------------------
def batch_client(client=None):
    if client is not None:
        return client
    import boto3  # lazy so unit tests don't need credentials (mirrors s3io._client)

    return boto3.client("batch")


def submit_array(*, size: int, job_name: str, command: list[str] | None = None,
                 environment: dict | None = None, job_queue: str | None = None,
                 job_definition: str | None = None, attempts: int = 3, client=None) -> str:
    """Submit a manifest as a Batch array job; return the job id. Index N runs line N
    (``size == 1`` submits a plain non-array job, since arrays need >= 2). ``command`` overrides the
    job definition's default; ``environment`` is the container env. ``attempts`` drives the
    Spot-friendly ``retryStrategy``. Queue/definition fall back to ``BII_BATCH_QUEUE`` /
    ``BII_BATCH_JOB_DEF``."""
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


def wait_for_array(job_id: str, *, client=None) -> str:
    """Poll Batch until ``job_id`` is SUCCEEDED or FAILED; return that state."""
    client = batch_client(client)
    while True:
        status = client.describe_jobs(jobs=[job_id])["jobs"][0].get("status", "")
        if status in ("SUCCEEDED", "FAILED"):
            return status
        time.sleep(_POLL_SECONDS)


def failed_children(job_id: str, client=None) -> dict[int, str]:
    """``{array-child index: failure detail}`` for the children of ``job_id`` that ended FAILED after
    retries (paginated). Detail is the status / container exit code / reason from the summary."""
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
    """Submit the manifest as one Batch array job running ``command``, wait for it, and return the
    lines whose child ended FAILED after retries (``{"index", "error"}``) — parity with
    ``run_docker``'s non-zero-exit failures. ``env`` is extra container environment. A unit that
    produces nothing exits 0, not failed."""
    wait_fn = wait_fn or wait_for_array
    job_id = submit_array(size=len(items), job_name=job_name, command=command,
                          environment={MANIFEST_ENV: manifest_uri, **(env or {})}, client=client)
    if wait_fn(job_id, client=client) == "FAILED":  # a non-array job (size 1) is just index 0
        detail = {0: "batch job failed"} if len(items) == 1 else failed_children(job_id, client)
        return [{"index": i, "error": detail[i]} for i in sorted(detail)]
    return []


# --------------------------------------------------------------------------------------
# Dispatch + container entrypoint — the two halves both drivers share
# --------------------------------------------------------------------------------------
def run_manifest(items: list[dict], command: list[str], *, executor: str, manifest_uri: str,
                 job_name: str, env: dict | None = None, store: str | None = None, label=None,
                 client=None, wait_fn=None) -> list[dict]:
    """Write ``items`` to ``manifest_uri`` and run ``command`` over them via ``executor`` (``docker``
    locally / ``batch`` on AWS); return the failed lines as ``{"index", "error"}``. ``store`` and
    ``label`` apply to the docker executor only (ignored on batch)."""
    write_manifest(items, manifest_uri)
    if executor == "docker":
        return run_docker(items, command, manifest_uri=manifest_uri, env=env, store=store, label=label)
    if executor == "batch":
        return run_batch(items, command, manifest_uri=manifest_uri, env=env, job_name=job_name,
                         client=client, wait_fn=wait_fn)
    raise SystemExit(f"unknown executor {executor!r} (docker | batch)")


def manifest_line() -> dict:
    """The manifest line this container handles — line ``AWS_BATCH_JOB_ARRAY_INDEX`` of the manifest
    at ``BII_MANIFEST``. The worker entrypoints (``bii-process`` / ``bii-stage``) read it."""
    manifest = os.environ.get(MANIFEST_ENV)
    if not manifest:
        raise SystemExit(f"{MANIFEST_ENV} must point at the manifest")
    index = int(os.environ.get("AWS_BATCH_JOB_ARRAY_INDEX", "0"))
    return read_manifest(manifest)[index]
