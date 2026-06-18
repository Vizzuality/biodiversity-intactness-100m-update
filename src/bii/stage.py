"""Staging orchestrator + per-unit worker.

Enumerates every staging unit (one COG/tile/year per dataset), stages the ones whose output is
missing (or all, with ``overwrite``), and consolidates each fully-staged asset's footprint index
from its parts. Two executors run the same per-unit work over the same per-image manifests +
``AWS_BATCH_JOB_ARRAY_INDEX`` contract, so a local docker run exercises exactly what Batch will:

* ``docker`` — ``docker run`` one container per unit (default; test the images locally; roads uses
  the osmctools image, everything else the raster image).
* ``batch``  — submit the manifest as an AWS Batch array job (one job per image group).

Both dispatch the ``bii-stage-worker`` entrypoint in :mod:`bii.stage_worker`. A thin driver over
:mod:`bii.staging`: each module exposes ``list_units`` / ``stage_unit`` and carries ``dst`` +
``ASSET``, so this module stays dataset-agnostic. The skip-if-exists decision lives here; the
per-unit staging lives in the worker. A unit that legitimately produces nothing (an ocean Hansen
tile 404s) exits 0 and is not a failure; one whose worker exits non-zero (docker) or whose Batch
child ends FAILED after retries is reported, and its asset's index is left unconsolidated.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import subprocess
import sys

from . import config, orchestrate, s3io, tile_index
from .staging import MODULES

# Datasets whose container needs osmctools (the roads image); everything else uses the raster image.
ROADS_DATASETS = ("roads",)
# landcover builds its index in place (no parts) -> consolidate-exempt.
INDEX_IN_PLACE = ("iolulc",)
# Host env forwarded into docker containers (S3 creds + staging tokens; -e NAME passes the value).
_FORWARD_ENV = (
    "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN", "AWS_PROFILE", "BII_EOG_COOKIE",
)


def _list_units(module, year: int | None) -> list[dict]:
    """``module.list_units`` filtered to ``year`` when the module is per-year, else its full list."""
    if year is not None and "years" in inspect.signature(module.list_units).parameters:
        return module.list_units(years=[year])
    return module.list_units()


def plan(dataset: str | None = None, year: int | None = None) -> list[tuple[str, dict]]:
    """``[(dataset_name, unit), ...]`` — every staging task for the selected datasets/year."""
    names = [dataset] if dataset else list(MODULES)
    return [(name, unit) for name in names for unit in _list_units(MODULES[name], year)]


def manifest_items(dataset: str | None = None, year: int | None = None) -> list[dict]:
    """The plan as worker-readable manifest lines: ``{dataset, unit, asset, year}``. ``asset``/``year``
    are recorded so the orchestrator can consolidate indexes after a Batch/docker run without the
    workers' return values."""
    return [{"dataset": name, "unit": unit, "asset": MODULES[name].ASSET, "year": unit.get("year")}
            for name, unit in plan(dataset, year)]


def _pending(items: list[dict]) -> list[dict]:
    """Items whose output ``dst`` does not yet exist (the skip-if-exists filter)."""
    return [it for it in items if not s3io.exists(it["unit"]["dst"])]


def print_summary(items: list[dict], pending: list[dict]) -> None:
    """Per-execution job summary on stderr (stdout stays JSON)."""
    print(f"staging {len(pending)} of {len(items)} units "
          f"({len(items) - len(pending)} skipped, already staged)", file=sys.stderr)


def _manifest_uri(group: str) -> str:
    return config.out_uri("stage", f"{group}.jsonl")


# --------------------------------------------------------------------------------------
# Executors
# --------------------------------------------------------------------------------------
def _failure(it: dict, error: str) -> dict:
    """One not-completed task for the run report (also drives the incomplete-index skip)."""
    return {"dataset": it["dataset"], "id": it["unit"]["id"],
            "asset": it["asset"], "year": it["year"], "error": error}


def _group(dataset: str) -> str:
    return "roads" if dataset in ROADS_DATASETS else "main"


def _groups(items: list[dict]) -> dict[str, list[dict]]:
    """``items`` grouped by container image (``main`` raster vs ``roads`` osmctools), preserving
    order. Both executors stage one group per image against its own manifest, so the worker's
    ``AWS_BATCH_JOB_ARRAY_INDEX`` is the line within the group — identical across docker and Batch."""
    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault(_group(it["dataset"]), []).append(it)
    return groups


def docker_run(image: str, command: list[str], *, env: dict | None = None,
               store: str | None = None) -> None:
    """One ``docker run --rm`` mirroring a Batch job: forward the host creds in ``_FORWARD_ENV``
    and set ``env`` (the job-definition environment). ``store`` is the local stand-in for the S3
    store — bind-mounted at the same absolute path and pointed at by ``BII_STAGED_ROOT`` /
    ``BII_OUT_ROOT``, so the container reads the manifest and writes COGs + index parts to it."""
    args = ["docker", "run", "--rm"]
    if store:
        args += ["-v", f"{store}:{store}"]
        env = {"BII_STAGED_ROOT": store, "BII_OUT_ROOT": store, **(env or {})}
    args += [a for k in _FORWARD_ENV if k in os.environ for a in ("-e", k)]
    args += [a for k, v in (env or {}).items() for a in ("-e", f"{k}={v}")]
    args += [image, *command]
    # Capture combined output so a failing unit's traceback survives in the run report; echo it
    # through once the unit finishes (the container output never streamed live anyway).
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    sys.stdout.write(proc.stdout)
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, args, output=proc.stdout)


def _run_docker(items: list[dict], store: str | None = None) -> list[dict]:
    """Run ``bii-stage-worker`` in one ``docker run`` per unit — the local mirror of the Batch array,
    over the same per-image manifests. The image group selects ``BII_STAGE_IMAGE`` /
    ``BII_STAGE_ROADS_IMAGE``; without ``store`` the container reads the manifest from S3 with the
    forwarded creds, with it from the bind-mounted local store. Continues past a container that
    exits non-zero; returns the failed items."""
    images = {"main": os.environ.get("BII_STAGE_IMAGE", "bii"),
              "roads": os.environ.get("BII_STAGE_ROADS_IMAGE", "bii-roads")}
    failed: list[dict] = []
    for key, group_items in _groups(items).items():
        muri = orchestrate.write_manifest(group_items, _manifest_uri(key))
        for i, it in enumerate(group_items):
            print(f"[{key} {i + 1}/{len(group_items)}] {it['dataset']} {it['unit']['id']}", file=sys.stderr)
            try:
                docker_run(images[key], ["bii-stage-worker"], store=store,
                           env={"BII_STAGE_MANIFEST": muri, "AWS_BATCH_JOB_ARRAY_INDEX": i})
            except subprocess.CalledProcessError as exc:
                tail = "\n".join((exc.output or "").strip().splitlines()[-15:]) or str(exc)
                failed.append(_failure(it, tail))
    return failed


def submit_array(manifest_uri: str, size: int, job_queue: str, job_definition: str,
                 job_name: str, client=None) -> str:
    """Submit a staging manifest as a Batch array job running ``bii-stage-worker``; return the job id.
    Index N stages line N (``size == 1`` submits a plain job, since arrays need >= 2)."""
    kwargs = dict(
        jobName=job_name, jobQueue=job_queue, jobDefinition=job_definition,
        containerOverrides={
            "command": ["bii-stage-worker"],
            "environment": [{"name": "BII_STAGE_MANIFEST", "value": manifest_uri}],
        },
        retryStrategy={"attempts": 3},
    )
    if size > 1:
        kwargs["arrayProperties"] = {"size": size}
    return orchestrate._batch_client(client).submit_job(**kwargs)["jobId"]


def _failed_children(job_id: str, client=None) -> dict[int, str]:
    """``{array-child index: failure detail}`` for the children of ``job_id`` that ended FAILED after
    retries (paginated). Detail is the status / container exit code / reason from the summary."""
    client = orchestrate._batch_client(client)
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


def _run_batch(items: list[dict], *, client=None, wait_fn=None) -> list[dict]:
    """Submit one Batch array job per image group (roads vs raster), wait for each, and return the
    units whose child ended FAILED after retries — parity with the docker executor's non-zero-exit
    failures. Queue + job defs come from ``BII_BATCH_QUEUE`` / ``BII_BATCH_JOB_DEF`` /
    ``BII_BATCH_ROADS_JOB_DEF``. Spot resilience is Batch's own ``retryStrategy``; a unit that
    produces nothing exits 0, not failed."""
    queue = os.environ.get("BII_BATCH_QUEUE")
    if not queue:
        raise SystemExit("set BII_BATCH_QUEUE")
    wait_fn = wait_fn or orchestrate.wait_for_array
    defs = {"main": os.environ.get("BII_BATCH_JOB_DEF"), "roads": os.environ.get("BII_BATCH_ROADS_JOB_DEF")}
    failed: list[dict] = []
    for key, group_items in _groups(items).items():
        if not defs[key]:
            raise SystemExit(f"set BII_BATCH_{'ROADS_' if key == 'roads' else ''}JOB_DEF for the {key} group")
        muri = orchestrate.write_manifest(group_items, _manifest_uri(key))
        job_id = submit_array(muri, len(group_items), queue, defs[key], f"bii-stage-{key}", client=client)
        if wait_fn(job_id, client=client) == "FAILED":  # a non-array job (size 1) is just index 0
            detail = {0: "batch job failed"} if len(group_items) == 1 else _failed_children(job_id, client)
            failed += [_failure(group_items[i], detail[i]) for i in sorted(detail)]
    return failed


def _consolidate(assets) -> list[str]:
    """Merge index parts for each ``(asset, year)``; skip assets that registered none (all skipped)."""
    out = []
    for asset, yr in sorted(assets, key=lambda a: (a[0], a[1] or 0)):
        try:
            out.append(tile_index.consolidate(asset, yr))
        except FileNotFoundError:
            pass
    return out


# --------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------
def run(dataset: str | None = None, year: int | None = None, *, executor: str = "docker",
        overwrite: bool = False, client=None, wait_fn=None) -> dict:
    """Stage the planned units (skipping those whose ``dst`` exists unless ``overwrite``) via the
    chosen executor (``docker`` locally / ``batch`` on AWS), then consolidate the footprint index of
    every fully-staged asset. The run continues past a failed unit; failures are reported and their
    asset's index is left unconsolidated."""
    items = manifest_items(dataset, year)
    pending = items if overwrite else _pending(items)
    print_summary(items, pending)
    if not pending:
        return {"planned": len(items), "pending": 0, "executor": executor, "failed": [],
                "incomplete_indexes": [], "indexes": []}

    if executor == "docker":
        failed = _run_docker(pending)
    elif executor == "batch":
        failed = _run_batch(pending, client=client, wait_fn=wait_fn)
    else:
        raise SystemExit(f"unknown executor {executor!r} (docker | batch)")

    # Consolidation set = the plan (every staged dataset but landcover, which builds its index in
    # place), minus assets with a failed unit: a missing footprint part would silently under-cover
    # the orchestrator's ocean-drop (real land chunks dropped), so leave that asset's prior index.
    assets = {(it["asset"], it["year"]) for it in pending if it["dataset"] not in INDEX_IN_PLACE}
    incomplete = {(f["asset"], f["year"]) for f in failed}
    return {"planned": len(items), "pending": len(pending), "executor": executor, "failed": failed,
            "incomplete_indexes": sorted(incomplete & assets, key=lambda a: (a[0], a[1] or 0)),
            "indexes": _consolidate(assets - incomplete)}


def main(argv=None) -> dict:
    """``scripts/stage.py`` / ``bii-stage`` entrypoint. Batch queue/job-defs and docker image names
    come from the environment (``BII_BATCH_*`` / ``BII_STAGE_*``), like the rest of the pipeline."""
    parser = argparse.ArgumentParser(description="Stage BII input datasets and consolidate their indexes.")
    parser.add_argument("--dataset", choices=sorted(MODULES), help="stage only this dataset (default: all)")
    parser.add_argument("--year", type=int, help="stage only this year (per-year datasets only)")
    parser.add_argument("--executor", choices=("docker", "batch"), default="docker",
                        help="where to run units (default: docker locally)")
    parser.add_argument("--overwrite", action="store_true", help="restage units whose output already exists")
    parser.add_argument("--dry-run", action="store_true", help="list the planned units + existence and exit")
    args = parser.parse_args(argv)

    if args.dry_run:
        units = [{"dataset": it["dataset"], "id": it["unit"]["id"], "dst": it["unit"]["dst"],
                  "exists": s3io.exists(it["unit"]["dst"])}
                 for it in manifest_items(args.dataset, args.year)]
        result = {"planned": len(units), "exists": sum(u["exists"] for u in units), "units": units}
    else:
        result = run(args.dataset, args.year, executor=args.executor, overwrite=args.overwrite)
    print(json.dumps(result))
    return result


# Console-script shim: main returns a dict (for tests / programmatic use), but the generated
# wrapper does ``sys.exit(fn())`` and ``sys.exit(<dict>)`` exits 1. Discard the return.
def cli() -> None:
    main()


if __name__ == "__main__":
    main()
