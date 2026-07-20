"""Tests for the minimal Aurora package scaffold."""

from aurora_core import __version__
from aurora_core.__main__ import APPLICATION_NAME, main


def test_package_has_version() -> None:
    assert __version__ == "0.1.0"


def test_startup_check(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("sys.argv", ["aurora", "--check"])

    assert main() == 0
    assert (
        f"{APPLICATION_NAME} {__version__}: package startup check passed"
        in capsys.readouterr().out
    )
