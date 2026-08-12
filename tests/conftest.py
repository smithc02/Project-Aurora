"""Shared fixtures for repository-local protected-history filesystem tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

_REPOSITORY_ROOT = Path(__file__).parent.parent.absolute()


@pytest.fixture
def history_test_directory() -> Iterator[Path]:
    """Create one disposable mode-0700 directory below trusted repo ancestry."""
    with TemporaryDirectory(
        prefix=".pytest-aurora-history-",
        dir=_REPOSITORY_ROOT,
    ) as raw_directory:
        directory = Path(raw_directory)
        os.chmod(directory, 0o700)
        yield directory
