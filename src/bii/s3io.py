"""Local-or-S3 I/O for staging, indexing, and processing.

Every destination in the pipeline is either a local path or an ``s3://`` URI, and every write
goes to a local file first and is then published to the destination. This module is the single
place that knows the difference; callers pass URIs and never branch on the scheme themselves.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from urllib.parse import urlparse

import boto3
import botocore

from . import config


def is_s3(uri: str) -> bool:
    return str(uri).startswith("s3://")


def _split_s3(uri: str) -> tuple[str, str]:
    p = urlparse(uri)
    return p.netloc, p.path.lstrip("/")


def _client():
    return boto3.client("s3")


def exists(uri: str) -> bool:
    """Whether a destination already exists (the skip-if-exists idiom)."""
    if is_s3(uri):
        bucket, key = _split_s3(uri)
        try:
            _client().head_object(Bucket=bucket, Key=key)
            return True
        except botocore.exceptions.ClientError:
            return False
    return os.path.exists(uri)


def upload(local_path: str, uri: str) -> None:
    """Publish a local file to ``uri`` (s3 upload or local copy)."""
    if is_s3(uri):
        bucket, key = _split_s3(uri)
        _client().upload_file(local_path, bucket, key)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(uri)), exist_ok=True)
        if os.path.abspath(local_path) != os.path.abspath(uri):
            shutil.copyfile(local_path, uri)


@contextmanager
def staged_local_path(dst: str):
    """Yield a local path to write to, then publish it to ``dst``.

    Local destinations are written through a sibling ``.tmp`` file and atomically renamed, so a
    crash never leaves a half-written file; S3 destinations are written to a temp file and
    uploaded. Either way the caller writes a plain local path and never sees the scheme.
    """
    if is_s3(dst):
        tmp = tempfile.NamedTemporaryFile(suffix=os.path.splitext(dst)[1] or ".tmp", delete=False)
        tmp.close()
        try:
            yield tmp.name
            upload(tmp.name, dst)
        finally:
            os.path.exists(tmp.name) and os.remove(tmp.name)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
        tmp = dst + ".tmp"
        try:
            yield tmp
            os.replace(tmp, dst)
        finally:
            os.path.exists(tmp) and os.remove(tmp)


def put_bytes(data: bytes, uri: str) -> None:
    """Write in-memory ``data`` to ``uri`` (s3 put or local write)."""
    if is_s3(uri):
        bucket, key = _split_s3(uri)
        _client().put_object(Bucket=bucket, Key=key, Body=data)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(uri)), exist_ok=True)
        with open(uri, "wb") as f:
            f.write(data)


def read_text(uri: str) -> str:
    """Read ``uri`` as text (s3 get or local read)."""
    if is_s3(uri):
        bucket, key = _split_s3(uri)
        return _client().get_object(Bucket=bucket, Key=key)["Body"].read().decode()
    with open(uri) as f:
        return f.read()


def list_uris(prefix_uri: str) -> list[str]:
    """List object URIs under a prefix (recursive for s3; one level for a local directory)."""
    if is_s3(prefix_uri):
        bucket, prefix = _split_s3(prefix_uri)
        client = _client()
        out: list[str] = []
        token = None
        while True:
            kw = dict(Bucket=bucket, Prefix=prefix)
            if token:
                kw["ContinuationToken"] = token
            resp = client.list_objects_v2(**kw)
            out += [f"s3://{bucket}/{o['Key']}" for o in resp.get("Contents", [])]
            if not resp.get("IsTruncated"):
                break
            token = resp["NextContinuationToken"]
        return out
    if os.path.isdir(prefix_uri):
        return [os.path.join(prefix_uri, f) for f in os.listdir(prefix_uri)]
    return []
