"""Tests for explicit read-only WLED validation without network access."""

from __future__ import annotations

import json

import pytest

from aurora_core.config import AuroraConfigurationError, load_settings
from aurora_core.hardware.errors import WLEDTimeoutError
from aurora_core.hardware.transport import _info_url
from aurora_core.hardware.wled import expected_led_count, parse_wled_info, validate_wled
from aurora_core.runtime.models import ComponentHealthState


class FakeTransport:
    def __init__(self, result: bytes | Exception) -> None:
        self.result, self.calls = result, 0

    def fetch_info(self, *, host: str, port: int, timeout_seconds: float) -> bytes:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def settings(**overrides: object):
    return load_settings(
        environment={},
        cli_overrides={"wled": {"enabled": True, "host": "device.local"}, **overrides},
    )


def test_timeout_default_environment_and_validation() -> None:
    assert load_settings(environment={}).wled.validation_timeout_seconds == 2.0
    assert (
        load_settings(
            environment={"AURORA_WLED__VALIDATION_TIMEOUT_SECONDS": "3.5"}
        ).wled.validation_timeout_seconds
        == 3.5
    )
    for value in (0.0, -1.0, 10.1, True):
        with pytest.raises(AuroraConfigurationError):
            load_settings(
                environment={},
                cli_overrides={"wled": {"validation_timeout_seconds": value}},
            )
    with pytest.raises(AuroraConfigurationError, match="Invalid float"):
        load_settings(environment={"AURORA_WLED__VALIDATION_TIMEOUT_SECONDS": "nope"})


@pytest.mark.parametrize(
    "host", ["192.0.2.1", "2001:db8::1", "example.invalid", "device.local"]
)
def test_safe_hosts_are_accepted(host: str) -> None:
    assert settings(wled={"enabled": True, "host": host}).wled.host == host


@pytest.mark.parametrize("host", ["http://bad", "a/b", "a?b", "a#b", "user@bad"])
def test_url_hosts_are_rejected(host: str) -> None:
    with pytest.raises(AuroraConfigurationError):
        settings(wled={"enabled": True, "host": host})


def test_expected_count_is_complete_only() -> None:
    configured = settings(
        lighting_zones=[
            {"name": "a", "enabled": True, "led_count": 3},
            {"name": "b", "enabled": True, "led_count": 4},
            {"name": "off", "enabled": False, "led_count": 9},
        ]
    )
    assert expected_led_count(configured.lighting_zones) == 7
    partial = settings(
        lighting_zones=[
            {"name": "a", "enabled": True, "led_count": 3},
            {"name": "b", "enabled": True},
        ]
    )
    assert expected_led_count(partial.lighting_zones) is None


def test_parse_is_sanitized() -> None:
    info = parse_wled_info(
        json.dumps(
            {"ver": "0.15.0", "leds": {"count": 12}, "ip": "192.0.2.1", "mac": "secret"}
        ).encode()
    )
    assert info.firmware_version == "0.15.0" and info.led_count == 12
    assert "192.0.2.1" not in repr(info)
    for body in (
        b"[]",
        b"{",
        b'{"ver":"","leds":{"count":1}}',
        b'{"ver":"x","leds":{"count":true}}',
    ):
        with pytest.raises(ValueError):
            parse_wled_info(body)


def test_validation_states_and_safety() -> None:
    disabled = validate_wled(load_settings(environment={}), FakeTransport(b"{}"))
    assert disabled.state is ComponentHealthState.DISABLED
    good = validate_wled(
        settings(lighting_zones=[{"name": "a", "enabled": True, "led_count": 12}]),
        FakeTransport(b'{"ver":"0.15.0","leds":{"count":12}}'),
    )
    assert good.state is ComponentHealthState.HEALTHY
    degraded = validate_wled(
        settings(lighting_zones=[{"name": "a", "enabled": True, "led_count": 3}]),
        FakeTransport(b'{"ver":"0.15.0","leds":{"count":12}}'),
    )
    assert degraded.state is ComponentHealthState.DEGRADED
    failure = validate_wled(
        settings(), FakeTransport(WLEDTimeoutError("do-not-print-this device.local:80"))
    )
    assert failure.reason_code == "timeout" and "do-not-print-this" not in repr(failure)


def test_fixed_url_uses_default_and_ipv6_brackets() -> None:
    assert _info_url("device.local", 80) == "http://device.local:80/json/info"
    assert _info_url("2001:db8::1", 81) == "http://[2001:db8::1]:81/json/info"


def test_timeout_cli_precedence() -> None:
    configured = load_settings(
        environment={"AURORA_WLED__VALIDATION_TIMEOUT_SECONDS": "3.0"},
        cli_overrides={"wled": {"validation_timeout_seconds": 4.0}},
    )
    assert configured.wled.validation_timeout_seconds == 4.0


def test_disabled_transport_is_not_called() -> None:
    transport = FakeTransport(b"{}")
    validate_wled(load_settings(environment={}), transport)
    assert transport.calls == 0


def test_production_transport_is_fixed_get_and_closes_response(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from aurora_core.hardware.transport import UrllibWLEDInfoTransport

    captured: dict[str, object] = {}

    class Response:
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            self.closed = True

        def getcode(self) -> int:
            return 200

        def read(self, amount: int) -> bytes:
            captured["amount"] = amount
            return b'{"ver":"0.15.0","leds":{"count":12}}'

    response = Response()

    class Opener:
        def open(self, request: object, timeout: float) -> Response:
            captured["request"], captured["timeout"] = request, timeout
            return response

    monkeypatch.setattr(
        "aurora_core.hardware.transport.build_opener", lambda handler: Opener()
    )
    body = UrllibWLEDInfoTransport().fetch_info(
        host="2001:db8::1", port=81, timeout_seconds=1.5
    )
    request = captured["request"]
    assert body.startswith(b"{") and response.closed
    assert captured["timeout"] == 1.5
    assert request.get_method() == "GET"  # type: ignore[union-attr]
    assert request.full_url == "http://[2001:db8::1]:81/json/info"  # type: ignore[union-attr]
    assert request.data is None  # type: ignore[union-attr]
