"""Per-unit staging worker — the docker/Batch container entrypoint.

One array index -> one manifest line. The orchestrator (:mod:`bii.stage`) builds the manifest and
dispatches the containers; this module is the ``bii-stage-worker`` command that runs inside each
one, staging a single unit. ``stage_unit`` always overwrites (the skip-if-exists decision lives in
the orchestrator); a unit that legitimately produces nothing (an ocean Hansen tile 404s) returns
``None`` and must not be treated as a failure to retry.
"""

from __future__ import annotations

import json
import os

from . import orchestrate
from .staging import MODULES


def worker(manifest_uri: str, index: int) -> dict | None:
    """Stage line ``index`` of a staging manifest; ``None`` when the unit produced nothing."""
    item = orchestrate.read_manifest(manifest_uri)[index]
    return MODULES[item["dataset"]].stage_unit(item["unit"])


def worker_main(argv=None) -> dict | None:
    """Batch array / docker entrypoint: stage this index's unit. Reads ``BII_STAGE_MANIFEST`` and
    ``AWS_BATCH_JOB_ARRAY_INDEX`` (defaults to 0 for a one-off run) from the environment."""
    manifest = os.environ.get("BII_STAGE_MANIFEST")
    if not manifest:
        raise SystemExit("BII_STAGE_MANIFEST must point at the staging units manifest")
    index = int(os.environ.get("AWS_BATCH_JOB_ARRAY_INDEX", "0"))
    result = worker(manifest, index)
    print(json.dumps(result))
    return result


# Console-script shim: worker_main returns a dict (for tests), but the generated wrapper does
# sys.exit(fn()) and sys.exit(<dict>) exits 1. Discard the return.
def worker_cli() -> None:
    worker_main()


if __name__ == "__main__":
    worker_main()
