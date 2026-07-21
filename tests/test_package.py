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


def test_version_and_config_validate(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("sys.argv", ["aurora", "--version"])
    assert main() == 0
    assert f"{APPLICATION_NAME} {__version__}" in capsys.readouterr().out

    monkeypatch.setattr(
        "sys.argv",
        [
            "aurora",
            "config",
            "validate",
            "--config",
            "configs/aurora.example.yaml",
        ],
    )
    assert main() == 0
    assert "Configuration is valid" in capsys.readouterr().out


def test_config_validate_error_does_not_expose_password(
    monkeypatch, capsys, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "bad.yaml"
    path.write_text("mqtt:\n  password: not-a-real-secret\n  port: 0\n")
    monkeypatch.setattr(
        "sys.argv", ["aurora", "config", "validate", "--config", str(path)]
    )
    assert main() == 1
    assert "not-a-real-secret" not in capsys.readouterr().err


def test_runtime_plan_is_sanitized_and_uses_environment(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AURORA_MQTT__PASSWORD", "do-not-print-this")
    monkeypatch.setenv("AURORA_WLED__ENABLED", "true")
    monkeypatch.setenv("AURORA_WLED__HOST", "example.invalid")
    monkeypatch.setattr("sys.argv", ["aurora", "runtime", "plan"])
    assert main() == 0
    output = capsys.readouterr().out
    assert "wled: configured, health unknown" in output
    assert "connectivity and hardware were not tested" in output
    assert "do-not-print-this" not in output
