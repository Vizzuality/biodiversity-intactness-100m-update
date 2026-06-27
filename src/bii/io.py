"""Local-or-S3 I/O via fsspec. ``staged_local_path`` is the exception: GDAL/rasterio need a real
local file, so s3 destinations stage through a temp file and upload."""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager

import fsspec


def is_s3(uri: str) -> bool:
    return str(uri).startswith("s3://")


def exists(uri: str) -> bool:
    fs, path = fsspec.core.url_to_fs(uri)
    return fs.exists(path)


def upload(local_path: str, uri: str) -> None:
    fs, path = fsspec.core.url_to_fs(uri, **({} if is_s3(uri) else {"auto_mkdir": True}))
    fs.put_file(local_path, path)


def put_bytes(data: bytes, uri: str) -> None:
    fs, path = fsspec.core.url_to_fs(uri, **({} if is_s3(uri) else {"auto_mkdir": True}))
    fs.pipe_file(path, data)


def read_text(uri: str) -> str:
    with fsspec.open(uri, "rt") as f:
        return f.read()


def list_uris(prefix_uri: str) -> list[str]:
    """List object URIs under a prefix, recursively."""
    fs, path = fsspec.core.url_to_fs(prefix_uri)
    files = fs.find(path)
    return [f"s3://{f}" for f in files] if is_s3(prefix_uri) else files


@contextmanager
def staged_local_path(dst: str):
    """Yield a local path to write to, then publish it to ``dst``. Local dsts go through a
    sibling ``.tmp`` and atomic rename to avoid incomplete/orphaned files."""
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
