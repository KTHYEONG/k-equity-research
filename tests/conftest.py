"""Project-local temporary root for test runs.

Pytest fixtures and tools honoring ``TMPDIR`` are pinned to ``<project>/tmp/pytest``
so no test artifact is written to the system temp (``/tmp``).
``tmp/`` is git-ignored and purged by the sync skill.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from _pytest.config import Config

# Prevent writing compiled bytecode during test runs
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TEMP_ROOT = PROJECT_ROOT / "tmp" / "pytest"
_ACTIVE_TEMP_ROOT: Path | None = None


def pytest_configure(config: Config) -> None:
    """Pin the process temporary root to the project before any fixture runs."""
    del config
    global _ACTIVE_TEMP_ROOT
    _ACTIVE_TEMP_ROOT = _TEMP_ROOT / f"session-{os.getpid()}"
    _ACTIVE_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    os.environ["PYTEST_DEBUG_TEMPROOT"] = str(_ACTIVE_TEMP_ROOT)
    os.environ["TMPDIR"] = str(_ACTIVE_TEMP_ROOT)
    tempfile.tempdir = str(_ACTIVE_TEMP_ROOT)


def pytest_sessionfinish(session: object, exitstatus: int) -> None:
    """Clean up project-local temporary files."""
    del session
    del exitstatus
    if _ACTIVE_TEMP_ROOT is not None and _ACTIVE_TEMP_ROOT.exists():
        for item in _ACTIVE_TEMP_ROOT.iterdir():
            if item.name == ".gitignore":
                continue
            try:
                if item.is_dir() and not item.is_symlink():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
            except OSError:
                pass
