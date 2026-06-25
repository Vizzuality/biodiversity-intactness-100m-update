import shutil
from pathlib import Path

import pytest

from bii import config

# Repo-relative staged dir for integration tests, so staged COGs + indexes are easy to
# inspect across runs (gitignored via data/).
_DATA_STAGED = Path(__file__).resolve().parent.parent / "data" / "test_staged"


@pytest.fixture
def local_staged(tmp_path, monkeypatch):
    """Redirect the staged root to a local tmp dir so staging writes COGs + index to disk.

    Used by the unit tests (no network); pytest auto-cleans ``tmp_path``.
    """
    root = str(tmp_path / "staged")
    monkeypatch.setattr(config, "STAGED_ROOT", root)
    return root


@pytest.fixture
def local_roots(tmp_path, monkeypatch):
    """Redirect both the staged and output roots to local tmp dirs; return ``(staged, out)``."""
    staged, out = str(tmp_path / "staged"), str(tmp_path / "out")
    monkeypatch.setattr(config, "STAGED_ROOT", staged)
    monkeypatch.setattr(config, "OUT_ROOT", out)
    return staged, out


@pytest.fixture
def data_staged(request, monkeypatch):
    """Redirect the staged root to ``data/test_staged/<test_name>/`` for integration tests.

    Unlike ``local_staged`` (tmp_path), this writes under the repo's gitignored ``data/`` so the
    staged outputs persist and can be opened/inspected after a run. The per-test subdir is wiped
    at setup so each run starts fresh (and the skip-if-exists path stays exercised within a test).
    """
    root = _DATA_STAGED / request.node.name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "STAGED_ROOT", str(root))
    return str(root)
