import shutil
from pathlib import Path

import pytest

from bii import config

_DATA_STAGED = Path(__file__).resolve().parent.parent / "data" / "test_staged"


@pytest.fixture
def local_staged(tmp_path, monkeypatch):
    root = str(tmp_path / "staged")
    monkeypatch.setattr(config, "STAGED_ROOT", root)
    return root


@pytest.fixture
def local_roots(tmp_path, monkeypatch):
    staged, out = str(tmp_path / "staged"), str(tmp_path / "out")
    monkeypatch.setattr(config, "STAGED_ROOT", staged)
    monkeypatch.setattr(config, "OUT_ROOT", out)
    return staged, out


@pytest.fixture
def data_staged(request, monkeypatch):
    """Staged root -> gitignored ``data/test_staged/<test_name>/`` so outputs persist for inspection,
    wiped at setup."""
    root = _DATA_STAGED / request.node.name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "STAGED_ROOT", str(root))
    return str(root)
