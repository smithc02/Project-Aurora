"""Synthetic safety tests for bounded operator-only DDP output validation."""

from __future__ import annotations

import socket
import struct
from collections.abc import Callable
from dataclasses import replace
from types import TracebackType
from typing import Any

import pytest

from aurora_core.__main__ import _print_ddp_output_report, build_parser, main
from aurora_core.config import AuroraConfigurationError, load_settings
from aurora_core.config.models import (
    AuroraSettings,
    DDPSettings,
    LightingZoneSettings,
)
from aurora_core.hardware import ddp_output_probe as probe_module
from aurora_core.hardware.ddp_output import validate_ddp_output
from aurora_core.hardware.ddp_output_probe import (
    DDP_BLACKOUT_RGB,
    DDP_BLACKOUT_SEQUENCE,
    DDP_DEADLINE_SECONDS,
    DDP_DESTINATION_ID,
    DDP_HEADER_LENGTH,
    DDP_MAX_DATA_PAYLOAD,
    DDP_MAX_DATAGRAMS_PER_FRAME,
    DDP_MAX_FRAME_PAYLOAD,
    DDP_MAX_LEDS,
    DDP_MAX_SEND_ATTEMPTS,
    DDP_RGB24_DATATYPE,
    DDP_TEST_RGB,
    DDP_TEST_SEQUENCE,
    BoundedDDPOutputProbe,
    DDPAddressInfo,
    DDPOutputProbeResult,
    DDPSocketLease,
    SocketAddress,
    StandardLibraryDDPResolver,
    StandardLibraryDDPSocketFactory,
    build_ddp_packets,
)
from aurora_core.hardware.models import DDPOutputValidationReport
from aurora_core.runtime.models import ComponentHealthState, ComponentId

_HOST = "private-host-canary.invalid"
_IPV4 = "192.0.2.44"
_PORT = 4048
_SAFETY_STATEMENT = (
    "At most one low-intensity DDP test frame and one blackout frame were "
    "attempted. No animation loop, discovery, broadcast, multicast, HTTP mutation, "
    "MQTT, HyperHDR, capture, or runtime-controller operation was performed."
)


@pytest.fixture(autouse=True)
def _forbid_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("real network access is forbidden in DDP tests")

    monkeypatch.setattr(probe_module.socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(probe_module.socket, "socket", forbidden)


def _address(
    host: str = _IPV4,
    *,
    family: int = socket.AF_INET,
    port: int = _PORT,
) -> DDPAddressInfo:
    sockaddr: tuple[object, ...]
    if family == socket.AF_INET6:
        sockaddr = (host, port, 0, 0)
    else:
        sockaddr = (host, port)
    return (family, socket.SOCK_DGRAM, socket.IPPROTO_UDP, "", sockaddr)


class _Resolver:
    def __init__(
        self,
        results: list[DDPAddressInfo] | None = None,
        failure: Exception | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.results = results if results is not None else [_address()]
        self.failure = failure
        self.calls: list[tuple[str, int]] = []
        self.events = events

    def resolve(self, *, host: str, port: int) -> list[DDPAddressInfo]:
        self.calls.append((host, port))
        if self.events is not None:
            self.events.append("resolve")
        if self.failure is not None:
            raise self.failure
        return self.results


_FULL = object()


class _Socket:
    def __init__(
        self,
        outcomes: list[object] | None = None,
        close_failure: BaseException | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.outcomes = list(outcomes or [])
        self.close_failure = close_failure
        self.events = events
        self.timeouts: list[float] = []
        self.sends: list[tuple[bytes, SocketAddress]] = []
        self.close_calls = 0
        self.receive_calls = 0

    def settimeout(self, value: float | None) -> None:
        assert value is not None and value > 0
        self.timeouts.append(value)
        if self.events is not None:
            self.events.append("settimeout")
        if self.outcomes and isinstance(self.outcomes[0], _TimeoutFailure):
            failure = self.outcomes.pop(0)
            assert isinstance(failure, _TimeoutFailure)
            raise failure.error

    def sendto(self, data: bytes, address: SocketAddress) -> int:
        self.sends.append((data, address))
        if self.events is not None:
            self.events.append("send")
        outcome = self.outcomes.pop(0) if self.outcomes else _FULL
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is _FULL:
            return len(data)
        assert isinstance(outcome, int)
        return outcome

    def recvfrom(self, _size: int) -> bytes:
        self.receive_calls += 1
        raise AssertionError("receive is forbidden")

    def close(self) -> None:
        self.close_calls += 1
        if self.events is not None:
            self.events.append("close")
        if self.close_failure is not None:
            raise self.close_failure


class _TimeoutFailure:
    def __init__(self, error: Exception) -> None:
        self.error = error


class _SocketFactory:
    def __init__(
        self,
        udp_socket: _Socket | None = None,
        failure: Exception | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.socket = udp_socket or _Socket()
        self.failure = failure
        self.calls: list[tuple[int, int, int]] = []
        self.events = events

    def acquire(self, *, family: int, socket_type: int, protocol: int) -> _SocketLease:
        self.calls.append((family, socket_type, protocol))
        if self.events is not None:
            self.events.append("socket")
        return _SocketLease(self.socket, self.failure)


class _SocketLease:
    def __init__(self, udp_socket: _Socket, failure: Exception | None) -> None:
        self.socket = udp_socket
        self.failure = failure
        self.cleanup: probe_module.DDPSocketCleanup | None = None
        self.cleanup_started = False
        self.close_attempted = False
        self.socket_was_acquired = False
        self.socket_was_closed = False

    def register_cleanup(self, cleanup: probe_module.DDPSocketCleanup) -> None:
        assert self.cleanup is None and not self.socket_was_acquired
        self.cleanup = cleanup

    def __enter__(self) -> _Socket:
        assert self.cleanup is not None
        if self.failure is not None:
            raise self.failure
        self.socket_was_acquired = True
        return self.socket

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exception_type, traceback
        active_interrupt = (
            exception if isinstance(exception, KeyboardInterrupt) else None
        )
        suppress_exception = False
        try:
            if not self.cleanup_started:
                self.cleanup_started = True
                assert self.cleanup is not None
                outcome = self.cleanup(self.socket, exception)
                active_interrupt = outcome.interruption or active_interrupt
                suppress_exception = outcome.suppress_exception
        except KeyboardInterrupt as error:
            active_interrupt = active_interrupt or error
        except Exception:
            suppress_exception = isinstance(exception, Exception)
        finally:
            if not self.close_attempted:
                self.close_attempted = True
                try:
                    self.socket.close()
                except KeyboardInterrupt as error:
                    active_interrupt = active_interrupt or error
                except Exception:
                    pass
                else:
                    self.socket_was_closed = True
        if active_interrupt is not None and active_interrupt is not exception:
            raise active_interrupt
        return suppress_exception


def _register_noop_cleanup(lease: DDPSocketLease) -> None:
    lease.register_cleanup(
        lambda _socket, _exception: probe_module._SocketCleanupOutcome()
    )


class _Clock:
    def __init__(self, *values: float, events: list[str] | None = None) -> None:
        self.values = list(values)
        self.last = values[-1] if values else 0.0
        self.calls = 0
        self.events = events

    def __call__(self) -> float:
        self.calls += 1
        if self.events is not None:
            self.events.append("clock")
        if self.values:
            self.last = self.values.pop(0)
        return self.last


class _ControlHook:
    def __init__(
        self, interruptions: dict[str, list[BaseException]] | None = None
    ) -> None:
        self.interruptions = {
            name: list(errors) for name, errors in (interruptions or {}).items()
        }
        self.calls: list[str] = []

    def __call__(self, name: str) -> None:
        self.calls.append(name)
        pending = self.interruptions.get(name, [])
        if pending:
            raise pending.pop(0)


def _probe(
    *,
    resolver: _Resolver | None = None,
    factory: _SocketFactory | None = None,
    clock: _Clock | None = None,
    control_hook: Callable[[str], None] | None = None,
) -> tuple[BoundedDDPOutputProbe, _Resolver, _SocketFactory]:
    selected_resolver = resolver or _Resolver()
    selected_factory = factory or _SocketFactory()
    return (
        BoundedDDPOutputProbe(
            resolver=selected_resolver,
            socket_factory=selected_factory,
            monotonic=clock or _Clock(0.0),
            control_hook=control_hook,
        ),
        selected_resolver,
        selected_factory,
    )


def _run_probe(
    led_count: int = 1,
    *,
    resolver: _Resolver | None = None,
    factory: _SocketFactory | None = None,
    clock: _Clock | None = None,
    control_hook: Callable[[str], None] | None = None,
) -> tuple[DDPOutputProbeResult, _Resolver, _SocketFactory]:
    probe, selected_resolver, selected_factory = _probe(
        resolver=resolver,
        factory=factory,
        clock=clock,
        control_hook=control_hook,
    )
    return (
        probe.probe(host=_HOST, port=_PORT, led_count=led_count),
        selected_resolver,
        selected_factory,
    )


def _settings(
    led_count: int | None = 1,
    *,
    enabled: bool = True,
    zones: tuple[LightingZoneSettings, ...] | None = None,
) -> AuroraSettings:
    selected_zones = zones
    if selected_zones is None:
        selected_zones = (
            LightingZoneSettings(name="rear", enabled=True, led_count=led_count),
        )
    return AuroraSettings(
        ddp=DDPSettings(enabled=enabled, host=_HOST if enabled else None),
        lighting_zones=selected_zones,
    )


def _valid_result(led_count: int = 1, **changes: Any) -> DDPOutputProbeResult:
    payload_bytes = led_count * 3
    packets = (payload_bytes + DDP_MAX_DATA_PAYLOAD - 1) // DDP_MAX_DATA_PAYLOAD
    result = DDPOutputProbeResult(
        reason_code="validated",
        socket_was_created=True,
        socket_was_closed=True,
        led_count=led_count,
        frame_payload_bytes=payload_bytes,
        test_packets_planned=packets,
        test_packets_sent=packets,
        blackout_packets_planned=packets,
        blackout_packets_sent=packets,
        test_frame_completed=True,
        blackout_attempted=True,
        blackout_completed=True,
        cleanup_completed=True,
    )
    return replace(result, **changes)


class _FakeProbe:
    def __init__(
        self,
        result: DDPOutputProbeResult | object,
        failure: BaseException | None = None,
    ) -> None:
        self.result = result
        self.failure = failure
        self.calls: list[tuple[str, int, int]] = []

    def probe(self, *, host: str, port: int, led_count: int) -> Any:
        self.calls.append((host, port, led_count))
        if self.failure is not None:
            raise self.failure
        return self.result


def _report(
    state: ComponentHealthState = ComponentHealthState.HEALTHY,
    **changes: Any,
) -> DDPOutputValidationReport:
    result = _valid_result()
    report = DDPOutputValidationReport(
        component_id=ComponentId.DDP,
        state=state,
        reason_code="validated"
        if state is ComponentHealthState.HEALTHY
        else state.value,
        message="sanitized",
        socket_was_created=result.socket_was_created,
        socket_was_closed=result.socket_was_closed,
        led_count=result.led_count,
        frame_payload_bytes=result.frame_payload_bytes,
        test_packets_planned=result.test_packets_planned,
        test_packets_sent=result.test_packets_sent,
        blackout_packets_planned=result.blackout_packets_planned,
        blackout_packets_sent=result.blackout_packets_sent,
        test_frame_completed=result.test_frame_completed,
        blackout_attempted=result.blackout_attempted,
        blackout_completed=result.blackout_completed,
        cleanup_completed=result.cleanup_completed,
    )
    return replace(report, **changes)


def test_ddp_configuration_defaults_and_enabled_host_requirement() -> None:
    settings = load_settings(environment={})
    assert settings.ddp.port == 4048 and not settings.ddp.enabled
    with pytest.raises(AuroraConfigurationError):
        load_settings(environment={}, cli_overrides={"ddp": {"enabled": True}})


def test_disabled_ddp_performs_no_probe_resolution_or_socket_work() -> None:
    fake = _FakeProbe(_valid_result())
    report = validate_ddp_output(_settings(enabled=False), fake)
    assert report.state is ComponentHealthState.DISABLED
    assert report.reason_code == "ddp_disabled" and fake.calls == []
    assert not report.socket_was_created and not report.blackout_attempted


@pytest.mark.parametrize(
    ("zones", "reason"),
    [
        ((), "enabled_zone_required"),
        (
            (LightingZoneSettings(name="off", enabled=False, led_count=1),),
            "enabled_zone_required",
        ),
        (
            (
                LightingZoneSettings(name="a", enabled=True, led_count=1),
                LightingZoneSettings(name="b", enabled=True, led_count=1),
            ),
            "multiple_enabled_zones_not_supported",
        ),
        (
            (LightingZoneSettings(name="a", enabled=True, led_count=None),),
            "led_count_required",
        ),
        (
            (LightingZoneSettings(name="a", enabled=True, led_count=513),),
            "led_count_exceeds_limit",
        ),
    ],
)
def test_zone_configuration_failures_stop_before_probe(
    zones: tuple[LightingZoneSettings, ...], reason: str
) -> None:
    fake = _FakeProbe(_valid_result())
    report = validate_ddp_output(_settings(zones=zones), fake)
    assert report.state is ComponentHealthState.UNHEALTHY
    assert report.reason_code == reason and fake.calls == []


def test_zero_led_count_is_rejected_by_configuration_and_service() -> None:
    with pytest.raises(AuroraConfigurationError):
        load_settings(
            environment={},
            cli_overrides={
                "ddp": {"enabled": True, "host": _HOST},
                "lighting_zones": [{"name": "rear", "enabled": True, "led_count": 0}],
            },
        )
    invalid_zone = LightingZoneSettings.model_construct(
        name="rear", enabled=True, led_count=0
    )
    report = validate_ddp_output(_settings(zones=(invalid_zone,)), _FakeProbe(object()))
    assert report.reason_code == "led_count_required"


@pytest.mark.parametrize("led_count", [1, 512])
def test_exactly_one_bounded_enabled_zone_is_accepted(led_count: int) -> None:
    fake = _FakeProbe(_valid_result(led_count))
    report = validate_ddp_output(_settings(led_count), fake)
    assert report.state is ComponentHealthState.HEALTHY
    assert fake.calls == [(_HOST, 4048, led_count)]


def test_packet_header_and_one_packet_frame_are_exact() -> None:
    payload = DDP_TEST_RGB
    packet = build_ddp_packets(payload, sequence=DDP_TEST_SEQUENCE)[0]
    assert DDP_HEADER_LENGTH == 10
    assert (
        packet
        == struct.pack(
            "!BBBBIH",
            0x41,
            1,
            DDP_RGB24_DATATYPE,
            DDP_DESTINATION_ID,
            0,
            3,
        )
        + b"\x00\x00\x10"
    )
    assert len(packet) == DDP_HEADER_LENGTH + len(payload)


def test_packet_boundaries_flags_offsets_lengths_and_sequences() -> None:
    one_packet = build_ddp_packets(b"x" * 1400, sequence=DDP_TEST_SEQUENCE)
    two_packets = build_ddp_packets(b"y" * 1401, sequence=DDP_BLACKOUT_SEQUENCE)
    assert len(one_packet) == 1 and one_packet[0][0] == 0x41
    assert len(two_packets) == 2
    first = struct.unpack("!BBBBIH", two_packets[0][:10])
    second = struct.unpack("!BBBBIH", two_packets[1][:10])
    assert first == (0x40, 2, 0x0B, 1, 0, 1400)
    assert second == (0x41, 2, 0x0B, 1, 1400, 1)
    assert len(two_packets[0][10:]) == 1400 and len(two_packets[1][10:]) == 1


def test_512_led_frames_segment_into_1400_and_136_bytes_without_timecode() -> None:
    result, _, factory = _run_probe(512)
    assert result.reason_code == "validated"
    packets = [data for data, _address_used in factory.socket.sends]
    assert [packet[1] for packet in packets] == [1, 1, 2, 2]
    assert [len(packet) - 10 for packet in packets] == [1400, 136, 1400, 136]
    assert [int.from_bytes(packet[4:8], "big") for packet in packets] == [
        0,
        1400,
        0,
        1400,
    ]
    assert packets[0][10:] == (DDP_TEST_RGB * 512)[:1400]
    assert packets[1][10:] == (DDP_TEST_RGB * 512)[1400:]
    assert packets[2][10:] == (DDP_BLACKOUT_RGB * 512)[:1400]
    assert packets[3][10:] == (DDP_BLACKOUT_RGB * 512)[1400:]
    assert all(len(packet[:10]) == DDP_HEADER_LENGTH for packet in packets)


def test_packet_builder_never_allows_more_than_two_packets() -> None:
    assert DDP_MAX_LEDS == 512
    assert DDP_MAX_FRAME_PAYLOAD == 1536
    assert DDP_MAX_DATAGRAMS_PER_FRAME == 2
    assert DDP_MAX_DATA_PAYLOAD == 1400
    with pytest.raises(ValueError):
        build_ddp_packets(b"x" * 1537, sequence=DDP_TEST_SEQUENCE)
    with pytest.raises(ValueError):
        build_ddp_packets(b"x", sequence=3)
    with pytest.raises(ValueError):
        build_ddp_packets(b"x", sequence=True)


def test_standard_resolver_requests_only_udp_address_families_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    def synthetic_getaddrinfo(*args: object, **kwargs: object) -> list[DDPAddressInfo]:
        calls.append((*args, kwargs))
        return [_address()]

    monkeypatch.setattr(probe_module.socket, "getaddrinfo", synthetic_getaddrinfo)
    assert list(StandardLibraryDDPResolver().resolve(host=_HOST, port=_PORT)) == [
        _address()
    ]
    assert calls == [
        (
            _HOST,
            _PORT,
            {
                "family": socket.AF_UNSPEC,
                "type": socket.SOCK_DGRAM,
                "proto": socket.IPPROTO_UDP,
            },
        )
    ]


def test_duplicate_resolver_results_are_normalized() -> None:
    resolver = _Resolver([_address(), _address(), _address()])
    result, resolver, factory = _run_probe(resolver=resolver)
    assert result.reason_code == "validated"
    assert resolver.calls == [(_HOST, _PORT)] and len(factory.calls) == 1


def test_equivalent_ipv6_results_normalize_to_one_exact_destination() -> None:
    resolver = _Resolver(
        [
            _address("2001:0db8:0000:0000:0000:0000:0000:0044", family=socket.AF_INET6),
            _address("2001:db8::44", family=socket.AF_INET6),
        ]
    )
    result, _, factory = _run_probe(resolver=resolver)
    assert result.reason_code == "validated"
    assert factory.calls == [(socket.AF_INET6, socket.SOCK_DGRAM, socket.IPPROTO_UDP)]
    assert {address for _packet, address in factory.socket.sends} == {
        ("2001:db8::44", _PORT, 0, 0)
    }


def test_ambiguous_resolution_is_rejected_without_socket_or_fallback() -> None:
    resolver = _Resolver([_address(), _address("192.0.2.45")])
    result, resolver, factory = _run_probe(resolver=resolver)
    assert result.reason_code == "destination_resolution_ambiguous"
    assert resolver.calls == [(_HOST, _PORT)] and factory.calls == []


@pytest.mark.parametrize(
    "address",
    [
        _address("0.0.0.0"),
        _address("255.255.255.255"),
        _address("239.1.2.3"),
        _address("::", family=socket.AF_INET6),
        _address("ff02::1", family=socket.AF_INET6),
        _address("::ffff:239.1.2.3", family=socket.AF_INET6),
        _address("::ffff:255.255.255.255", family=socket.AF_INET6),
    ],
)
def test_non_unicast_destinations_are_rejected(address: DDPAddressInfo) -> None:
    result, _, factory = _run_probe(resolver=_Resolver([address]))
    assert result.reason_code == "destination_not_unicast"
    assert factory.calls == [] and not result.socket_was_created


@pytest.mark.parametrize(
    ("resolver", "reason"),
    [
        (_Resolver([]), "destination_resolution_failed"),
        (
            _Resolver(failure=OSError("private resolver canary")),
            "destination_resolution_failed",
        ),
        (
            _Resolver(failure=ValueError("private resolver canary")),
            "destination_resolution_failed",
        ),
    ],
)
def test_resolution_failures_are_sanitized(resolver: _Resolver, reason: str) -> None:
    result, _, factory = _run_probe(resolver=resolver)
    assert result.reason_code == reason and factory.calls == []
    assert _HOST not in repr(result) and _IPV4 not in repr(result)
    assert "private resolver" not in repr(result)


def test_exactly_one_socket_uses_exact_udp_parameters_and_destination() -> None:
    hook = _ControlHook()
    result, resolver, factory = _run_probe(512, control_hook=hook)
    assert result.reason_code == "validated"
    assert resolver.calls == [(_HOST, _PORT)]
    assert factory.calls == [(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)]
    assert len(factory.socket.sends) == 4
    assert {address for _packet, address in factory.socket.sends} == {(_IPV4, _PORT)}
    assert factory.socket.receive_calls == 0
    assert factory.socket.close_calls == 1
    assert hook.calls.count("after_socket_acquired") == 1


def test_standard_socket_factory_uses_exact_constructor_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int, int]] = []
    sentinel = _Socket()

    def synthetic_socket(family: int, kind: int, protocol: int) -> _Socket:
        calls.append((family, kind, protocol))
        return sentinel

    monkeypatch.setattr(probe_module.socket, "socket", synthetic_socket)
    lease = StandardLibraryDDPSocketFactory().acquire(
        family=socket.AF_INET6,
        socket_type=socket.SOCK_DGRAM,
        protocol=socket.IPPROTO_UDP,
    )
    _register_noop_cleanup(lease)
    assert calls == []
    with lease as created:
        assert created is sentinel
    assert calls == [(socket.AF_INET6, socket.SOCK_DGRAM, socket.IPPROTO_UDP)]
    assert sentinel.close_calls == 1 and lease.socket_was_closed


def test_standard_socket_lease_creation_failure_does_not_attempt_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = OSError("synthetic creation failure")

    def synthetic_socket(_family: int, _kind: int, _protocol: int) -> _Socket:
        raise failure

    monkeypatch.setattr(probe_module.socket, "socket", synthetic_socket)
    lease = StandardLibraryDDPSocketFactory().acquire(
        family=socket.AF_INET,
        socket_type=socket.SOCK_DGRAM,
        protocol=socket.IPPROTO_UDP,
    )
    _register_noop_cleanup(lease)
    with pytest.raises(OSError) as caught, lease:
        raise AssertionError("the failed lease must not expose a socket")
    assert caught.value is failure and not lease.socket_was_closed


def test_standard_socket_lease_close_failure_is_unconfirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = _Socket(close_failure=OSError("synthetic close failure"))
    monkeypatch.setattr(probe_module.socket, "socket", lambda *_args: sentinel)
    lease = StandardLibraryDDPSocketFactory().acquire(
        family=socket.AF_INET,
        socket_type=socket.SOCK_DGRAM,
        protocol=socket.IPPROTO_UDP,
    )
    _register_noop_cleanup(lease)
    with lease:
        pass
    assert sentinel.close_calls == 1 and not lease.socket_was_closed


def test_standard_socket_lease_preserves_active_interrupt_during_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = KeyboardInterrupt("original")
    second = KeyboardInterrupt("close")
    sentinel = _Socket(close_failure=second)
    monkeypatch.setattr(probe_module.socket, "socket", lambda *_args: sentinel)
    lease = StandardLibraryDDPSocketFactory().acquire(
        family=socket.AF_INET,
        socket_type=socket.SOCK_DGRAM,
        protocol=socket.IPPROTO_UDP,
    )
    _register_noop_cleanup(lease)
    with pytest.raises(KeyboardInterrupt) as caught, lease:
        raise original
    assert caught.value is original and sentinel.close_calls == 1


def test_standard_socket_lease_propagates_close_interrupt_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupt = KeyboardInterrupt("close")
    sentinel = _Socket(close_failure=interrupt)
    monkeypatch.setattr(probe_module.socket, "socket", lambda *_args: sentinel)
    lease = StandardLibraryDDPSocketFactory().acquire(
        family=socket.AF_INET,
        socket_type=socket.SOCK_DGRAM,
        protocol=socket.IPPROTO_UDP,
    )
    _register_noop_cleanup(lease)
    with pytest.raises(KeyboardInterrupt) as caught, lease:
        pass
    assert caught.value is interrupt and sentinel.close_calls == 1
    assert lease.__exit__(None, None, None) is False
    assert sentinel.close_calls == 1


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (OSError("private socket canary"), "socket_creation_failed"),
        (ValueError("private socket canary"), "unexpected_ddp_output_failure"),
    ],
)
def test_socket_creation_failure_has_no_send_or_close(
    failure: Exception, reason: str
) -> None:
    factory = _SocketFactory(failure=failure)
    hook = _ControlHook()
    result, _, factory = _run_probe(factory=factory, control_hook=hook)
    assert result.reason_code == reason and len(factory.calls) == 1
    assert factory.socket.sends == [] and factory.socket.close_calls == 0
    assert not result.blackout_attempted and "private" not in repr(result)
    assert hook.calls == []


def test_deadline_is_checked_and_exact_remaining_timeout_precedes_every_send() -> None:
    events: list[str] = []
    clock = _Clock(10.0, 10.25, 10.5, 10.75, 11.0, events=events)
    udp_socket = _Socket(events=events)
    factory = _SocketFactory(udp_socket, events=events)
    resolver = _Resolver(events=events)
    result, _, _ = _run_probe(512, resolver=resolver, factory=factory, clock=clock)
    assert result.reason_code == "validated"
    assert udp_socket.timeouts == [1.75, 1.5, 1.25, 1.0]
    assert clock.calls == 5
    operation_events = [
        event for event in events if event in {"clock", "settimeout", "send"}
    ]
    assert operation_events == ["clock"] + ["clock", "settimeout", "send"] * 4


def test_no_retry_including_eintr_and_maximum_attempts_are_enforced() -> None:
    udp_socket = _Socket([InterruptedError("private EINTR"), _FULL, _FULL])
    result, _, factory = _run_probe(512, factory=_SocketFactory(udp_socket))
    assert result.reason_code == "test_frame_send_failed"
    assert len(factory.socket.sends) == 3
    assert [packet[1] for packet, _ in factory.socket.sends] == [1, 2, 2]
    assert len(factory.socket.sends) <= DDP_MAX_SEND_ATTEMPTS == 4
    assert not result.retry_was_used


def test_blackout_follows_successful_test_frame() -> None:
    result, _, factory = _run_probe(1)
    assert result.reason_code == "validated"
    assert [packet[1] for packet, _ in factory.socket.sends] == [1, 2]
    assert result.test_frame_completed and result.blackout_completed


@pytest.mark.parametrize(
    ("test_outcome", "reason"),
    [
        (OSError("private"), "test_frame_send_failed"),
        (1, "test_frame_partial_send"),
        (ValueError("private"), "unexpected_ddp_output_failure"),
    ],
)
def test_blackout_follows_first_test_failure_and_preserves_reason(
    test_outcome: object, reason: str
) -> None:
    udp_socket = _Socket([test_outcome, _FULL, _FULL])
    result, _, _ = _run_probe(512, factory=_SocketFactory(udp_socket))
    assert result.reason_code == reason
    assert not result.test_frame_completed
    assert result.blackout_attempted and result.blackout_completed
    assert [packet[1] for packet, _ in udp_socket.sends] == [1, 2, 2]


def test_test_deadline_after_socket_creation_still_enters_blackout() -> None:
    result, _, factory = _run_probe(512, clock=_Clock(0.0, 2.0, 2.0, 2.0))
    assert result.reason_code == "blackout_deadline_exceeded"
    assert result.blackout_attempted and not result.blackout_completed
    assert factory.socket.sends == [] and factory.socket.close_calls == 1


def test_elapsed_resolution_time_consumes_first_send_deadline() -> None:
    clock = _Clock(10.0, 12.1, 12.1)
    result, resolver, factory = _run_probe(512, clock=clock)
    assert resolver.calls == [(_HOST, _PORT)]
    assert factory.calls == [(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)]
    assert result.socket_was_created
    assert clock.calls == 3
    assert factory.socket.timeouts == [] and factory.socket.sends == []
    assert result.blackout_attempted
    assert result.reason_code == "blackout_deadline_exceeded"
    assert factory.socket.close_calls == 1 and result.socket_was_closed
    assert not result.retry_was_used


def test_each_blackout_segment_is_attempted_once_after_blackout_failure() -> None:
    udp_socket = _Socket([_FULL, _FULL, OSError("private"), _FULL])
    result, _, _ = _run_probe(512, factory=_SocketFactory(udp_socket))
    assert result.reason_code == "blackout_send_failed"
    assert [packet[1] for packet, _ in udp_socket.sends] == [1, 1, 2, 2]
    assert result.blackout_packets_sent == 1 and not result.blackout_completed


def test_blackout_failure_overrides_test_and_close_failures() -> None:
    udp_socket = _Socket(
        [OSError("test"), OSError("blackout"), _FULL],
        close_failure=OSError("close"),
    )
    result, _, _ = _run_probe(512, factory=_SocketFactory(udp_socket))
    assert result.reason_code == "blackout_send_failed"
    assert not result.socket_was_closed and udp_socket.close_calls == 1


def test_blackout_partial_send_has_precedence() -> None:
    udp_socket = _Socket([OSError("test"), 1, _FULL])
    result, _, _ = _run_probe(512, factory=_SocketFactory(udp_socket))
    assert result.reason_code == "blackout_partial_send"


def test_successful_blackout_preserves_earlier_test_failure_and_close_result() -> None:
    udp_socket = _Socket(
        [OSError("test"), _FULL, _FULL], close_failure=OSError("close")
    )
    result, _, _ = _run_probe(512, factory=_SocketFactory(udp_socket))
    assert result.reason_code == "test_frame_send_failed"
    assert result.blackout_completed and not result.socket_was_closed


def test_successful_frames_with_close_failure_is_cleanup_unconfirmed() -> None:
    udp_socket = _Socket(close_failure=OSError("private close canary"))
    result, _, _ = _run_probe(factory=_SocketFactory(udp_socket))
    assert result.reason_code == "ddp_output_cleanup_unconfirmed"
    assert result.test_frame_completed and result.blackout_completed
    assert not result.socket_was_closed and not result.cleanup_completed
    assert [packet[1] for packet, _ in udp_socket.sends] == [1, 2]
    assert udp_socket.close_calls == 1
    report = validate_ddp_output(_settings(), _FakeProbe(result))
    assert report.state is ComponentHealthState.DEGRADED


def test_close_occurs_once_after_blackout_and_result_is_built_after_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    real_result = DDPOutputProbeResult

    def tracked_result(*args: object, **kwargs: object) -> DDPOutputProbeResult:
        events.append("result")
        return real_result(*args, **kwargs)

    monkeypatch.setattr(probe_module, "DDPOutputProbeResult", tracked_result)
    udp_socket = _Socket(events=events)
    result, _, _ = _run_probe(
        factory=_SocketFactory(udp_socket, events=events),
        resolver=_Resolver(events=events),
    )
    assert result.reason_code == "validated" and udp_socket.close_calls == 1
    assert events == [
        "resolve",
        "socket",
        "settimeout",
        "send",
        "settimeout",
        "send",
        "close",
        "result",
    ]


def test_timeout_setting_failure_is_not_retried_and_blackout_still_runs() -> None:
    udp_socket = _Socket([_TimeoutFailure(OSError("private")), _FULL])
    result, _, _ = _run_probe(factory=_SocketFactory(udp_socket))
    assert result.reason_code == "test_frame_send_failed"
    assert len(udp_socket.sends) == 1 and udp_socket.sends[0][0][1] == 2
    assert len(udp_socket.timeouts) == 2 and not result.retry_was_used


def test_keyboard_interrupt_during_first_test_send_defers_until_cleanup() -> None:
    interrupt = KeyboardInterrupt("operator interrupt")
    udp_socket = _Socket([interrupt, _FULL, _FULL])
    factory = _SocketFactory(udp_socket)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run_probe(512, factory=factory)
    assert caught.value is interrupt
    assert [packet[1] for packet, _ in udp_socket.sends] == [1, 2, 2]
    assert len(udp_socket.sends) == 3 <= DDP_MAX_SEND_ATTEMPTS
    assert udp_socket.close_calls == 1


def test_keyboard_interrupt_after_test_datagram_acceptance_still_cleans_up() -> None:
    interrupt = KeyboardInterrupt("operator interrupt")
    udp_socket = _Socket([_FULL, interrupt, _FULL, _FULL])
    factory = _SocketFactory(udp_socket)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run_probe(512, factory=factory)
    assert caught.value is interrupt
    sequences = [packet[1] for packet, _ in udp_socket.sends]
    assert sequences == [1, 1, 2, 2]
    assert len(sequences) == DDP_MAX_SEND_ATTEMPTS
    assert udp_socket.close_calls == 1


def test_keyboard_interrupt_during_blackout_does_not_retry_segment() -> None:
    interrupt = KeyboardInterrupt("operator interrupt")
    udp_socket = _Socket([_FULL, _FULL, interrupt, _FULL])
    factory = _SocketFactory(udp_socket)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run_probe(512, factory=factory)
    assert caught.value is interrupt
    blackout_packets = [
        packet for packet, _ in udp_socket.sends if packet[1] == DDP_BLACKOUT_SEQUENCE
    ]
    assert [int.from_bytes(packet[4:8], "big") for packet in blackout_packets] == [
        0,
        1400,
    ]
    assert len(udp_socket.sends) == DDP_MAX_SEND_ATTEMPTS
    assert udp_socket.close_calls == 1


def test_second_keyboard_interrupt_during_cleanup_preserves_original() -> None:
    original = KeyboardInterrupt("original operator interrupt")
    second = KeyboardInterrupt("second operator interrupt")
    udp_socket = _Socket([original, second, _FULL])
    factory = _SocketFactory(udp_socket)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run_probe(512, factory=factory)
    assert caught.value is original
    assert [packet[1] for packet, _ in udp_socket.sends] == [1, 2, 2]
    blackout_packets = [
        packet for packet, _ in udp_socket.sends if packet[1] == DDP_BLACKOUT_SEQUENCE
    ]
    assert [int.from_bytes(packet[4:8], "big") for packet in blackout_packets] == [
        0,
        1400,
    ]
    assert udp_socket.close_calls == 1


def test_second_keyboard_interrupt_during_close_preserves_original() -> None:
    original = KeyboardInterrupt("original operator interrupt")
    second = KeyboardInterrupt("close interrupt")
    events: list[str] = []
    udp_socket = _Socket([original, _FULL, _FULL], close_failure=second, events=events)
    factory = _SocketFactory(udp_socket, events=events)
    resolver = _Resolver(events=events)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run_probe(512, resolver=resolver, factory=factory)
    assert caught.value is original
    assert [packet[1] for packet, _ in udp_socket.sends] == [1, 2, 2]
    assert udp_socket.close_calls == 1 and events[-1] == "close"


def test_keyboard_interrupt_during_close_propagates_after_one_close() -> None:
    interrupt = KeyboardInterrupt("close interrupt")
    events: list[str] = []
    udp_socket = _Socket(close_failure=interrupt, events=events)
    factory = _SocketFactory(udp_socket, events=events)
    resolver = _Resolver(events=events)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run_probe(1, resolver=resolver, factory=factory)
    assert caught.value is interrupt
    assert udp_socket.close_calls == 1
    assert events[-1] == "close"
    assert [packet[1] for packet, _ in udp_socket.sends] == [1, 2]


@pytest.mark.parametrize(
    "outcomes",
    [
        [KeyboardInterrupt(), _FULL, _FULL],
        [_FULL, KeyboardInterrupt(), _FULL, _FULL],
        [_FULL, _FULL, KeyboardInterrupt(), _FULL],
        [_FULL, _FULL, _FULL, KeyboardInterrupt()],
    ],
)
def test_keyboard_interrupt_paths_never_exceed_global_send_budget(
    outcomes: list[object],
) -> None:
    udp_socket = _Socket(outcomes)
    factory = _SocketFactory(udp_socket)
    with pytest.raises(KeyboardInterrupt):
        _run_probe(512, factory=factory)
    assert len(udp_socket.sends) <= DDP_MAX_SEND_ATTEMPTS
    assert udp_socket.close_calls == 1


def test_service_does_not_sanitize_keyboard_interrupt() -> None:
    interrupt = KeyboardInterrupt("operator interrupt")
    fake = _FakeProbe(_valid_result(), interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        validate_ddp_output(_settings(), fake)
    assert caught.value is interrupt


def test_boundary_interrupt_after_test_outcome_runs_blackout_and_close() -> None:
    interrupt = KeyboardInterrupt("after test outcome")
    hook = _ControlHook({"after_test_send": [interrupt]})
    udp_socket = _Socket()
    factory = _SocketFactory(udp_socket)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run_probe(512, factory=factory, control_hook=hook)
    assert caught.value is interrupt
    assert [packet[1] for packet, _ in udp_socket.sends] == [1, 2, 2]
    assert len(udp_socket.sends) == 3 <= DDP_MAX_SEND_ATTEMPTS
    assert udp_socket.close_calls == 1


def test_boundary_interrupt_during_blackout_transition_runs_cleanup() -> None:
    interrupt = KeyboardInterrupt("blackout transition")
    hook = _ControlHook({"before_blackout": [interrupt]})
    udp_socket = _Socket()
    factory = _SocketFactory(udp_socket)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run_probe(512, factory=factory, control_hook=hook)
    assert caught.value is interrupt
    assert [packet[1] for packet, _ in udp_socket.sends] == [1, 1, 2, 2]
    assert len(udp_socket.sends) == DDP_MAX_SEND_ATTEMPTS
    assert udp_socket.close_calls == 1


def test_boundary_interrupt_between_blackout_segments_never_retries() -> None:
    interrupt = KeyboardInterrupt("between blackout segments")
    hook = _ControlHook({"between_blackout_packets": [interrupt]})
    udp_socket = _Socket()
    factory = _SocketFactory(udp_socket)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run_probe(512, factory=factory, control_hook=hook)
    assert caught.value is interrupt
    blackout_packets = [
        packet for packet, _ in udp_socket.sends if packet[1] == DDP_BLACKOUT_SEQUENCE
    ]
    assert [int.from_bytes(packet[4:8], "big") for packet in blackout_packets] == [
        0,
        1400,
    ]
    assert len(udp_socket.sends) == DDP_MAX_SEND_ATTEMPTS
    assert udp_socket.close_calls == 1


def test_boundary_interrupt_before_close_still_closes_once() -> None:
    interrupt = KeyboardInterrupt("before close")
    hook = _ControlHook({"before_close": [interrupt]})
    events: list[str] = []
    udp_socket = _Socket(events=events)
    factory = _SocketFactory(udp_socket, events=events)
    resolver = _Resolver(events=events)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run_probe(
            512,
            resolver=resolver,
            factory=factory,
            control_hook=hook,
        )
    assert caught.value is interrupt
    assert len(udp_socket.sends) == DDP_MAX_SEND_ATTEMPTS
    assert udp_socket.close_calls == 1 and events[-1] == "close"


def test_boundary_interrupt_remains_preferred_during_cleanup_interrupt() -> None:
    original = KeyboardInterrupt("boundary interrupt")
    second = KeyboardInterrupt("blackout send interrupt")
    hook = _ControlHook({"after_test_send": [original]})
    udp_socket = _Socket([_FULL, second, _FULL])
    factory = _SocketFactory(udp_socket)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run_probe(512, factory=factory, control_hook=hook)
    assert caught.value is original
    assert [packet[1] for packet, _ in udp_socket.sends] == [1, 2, 2]
    assert len(udp_socket.sends) == 3 <= DDP_MAX_SEND_ATTEMPTS
    assert udp_socket.close_calls == 1


def test_immediate_with_body_interrupt_runs_owned_blackout_before_close() -> None:
    interrupt = KeyboardInterrupt("immediate with-body interrupt")
    hook = _ControlHook({"after_socket_session_entered": [interrupt]})
    udp_socket = _Socket()
    factory = _SocketFactory(udp_socket)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run_probe(512, factory=factory, control_hook=hook)
    assert caught.value is interrupt
    assert factory.calls == [(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)]
    assert "after_socket_acquired" not in hook.calls
    assert [packet[1] for packet, _ in udp_socket.sends] == [2, 2]
    assert [int.from_bytes(packet[4:8], "big") for packet, _ in udp_socket.sends] == [
        0,
        DDP_MAX_DATA_PAYLOAD,
    ]
    assert len(udp_socket.sends) == 2 <= DDP_MAX_SEND_ATTEMPTS
    assert udp_socket.close_calls == 1


def test_immediate_with_body_ordinary_failure_is_sanitized_after_cleanup() -> None:
    hook = _ControlHook(
        {"after_socket_session_entered": [ValueError("private body failure")]}
    )
    udp_socket = _Socket()
    result, _, _ = _run_probe(
        512,
        factory=_SocketFactory(udp_socket),
        control_hook=hook,
    )
    assert result.reason_code == "unexpected_ddp_output_failure"
    assert [packet[1] for packet, _ in udp_socket.sends] == [2, 2]
    assert result.blackout_completed and result.socket_was_closed
    assert udp_socket.close_calls == 1
    assert "private" not in repr(result)


def test_immediate_with_body_interrupt_preserves_original_during_blackout() -> None:
    original = KeyboardInterrupt("immediate with-body interrupt")
    second = KeyboardInterrupt("blackout interrupt")
    hook = _ControlHook({"after_socket_session_entered": [original]})
    udp_socket = _Socket([second, _FULL])
    factory = _SocketFactory(udp_socket)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run_probe(512, factory=factory, control_hook=hook)
    assert caught.value is original
    assert [packet[1] for packet, _ in udp_socket.sends] == [2, 2]
    assert [int.from_bytes(packet[4:8], "big") for packet, _ in udp_socket.sends] == [
        0,
        DDP_MAX_DATA_PAYLOAD,
    ]
    assert len(udp_socket.sends) == 2 <= DDP_MAX_SEND_ATTEMPTS
    assert udp_socket.close_calls == 1


def test_immediate_with_body_interrupt_preserves_original_during_close() -> None:
    original = KeyboardInterrupt("immediate with-body interrupt")
    second = KeyboardInterrupt("close interrupt")
    events: list[str] = []
    hook = _ControlHook({"after_socket_session_entered": [original]})
    udp_socket = _Socket(close_failure=second, events=events)
    factory = _SocketFactory(udp_socket, events=events)
    resolver = _Resolver(events=events)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run_probe(
            512,
            resolver=resolver,
            factory=factory,
            control_hook=hook,
        )
    assert caught.value is original
    assert [packet[1] for packet, _ in udp_socket.sends] == [2, 2]
    assert udp_socket.close_calls == 1 and events[-1] == "close"


def test_immediate_with_body_interrupt_observes_expired_deadline() -> None:
    original = KeyboardInterrupt("immediate with-body interrupt")
    hook = _ControlHook({"after_socket_session_entered": [original]})
    clock = _Clock(10.0, 12.1)
    udp_socket = _Socket()
    factory = _SocketFactory(udp_socket)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run_probe(512, factory=factory, clock=clock, control_hook=hook)
    assert caught.value is original
    assert clock.calls == 2
    assert udp_socket.timeouts == [] and udp_socket.sends == []
    assert udp_socket.close_calls == 1


def test_interrupt_after_socket_acquired_runs_only_bounded_blackout_and_close() -> None:
    interrupt = KeyboardInterrupt("after socket acquired")
    hook = _ControlHook({"after_socket_acquired": [interrupt]})
    udp_socket = _Socket()
    factory = _SocketFactory(udp_socket)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run_probe(512, factory=factory, control_hook=hook)
    assert caught.value is interrupt
    assert [packet[1] for packet, _ in udp_socket.sends] == [2, 2]
    assert [int.from_bytes(packet[4:8], "big") for packet, _ in udp_socket.sends] == [
        0,
        DDP_MAX_DATA_PAYLOAD,
    ]
    assert len(udp_socket.sends) == 2 <= DDP_MAX_SEND_ATTEMPTS
    assert udp_socket.close_calls == 1


def test_interrupt_after_socket_acquired_preserves_original_during_blackout() -> None:
    original = KeyboardInterrupt("after socket acquired")
    second = KeyboardInterrupt("blackout interrupt")
    hook = _ControlHook({"after_socket_acquired": [original]})
    udp_socket = _Socket([second, _FULL])
    factory = _SocketFactory(udp_socket)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run_probe(512, factory=factory, control_hook=hook)
    assert caught.value is original
    assert [packet[1] for packet, _ in udp_socket.sends] == [2, 2]
    assert [int.from_bytes(packet[4:8], "big") for packet, _ in udp_socket.sends] == [
        0,
        DDP_MAX_DATA_PAYLOAD,
    ]
    assert len(udp_socket.sends) == 2 <= DDP_MAX_SEND_ATTEMPTS
    assert udp_socket.close_calls == 1


def test_interrupt_after_socket_acquired_preserves_original_during_close() -> None:
    original = KeyboardInterrupt("after socket acquired")
    second = KeyboardInterrupt("close interrupt")
    events: list[str] = []
    hook = _ControlHook({"after_socket_acquired": [original]})
    udp_socket = _Socket(close_failure=second, events=events)
    factory = _SocketFactory(udp_socket, events=events)
    resolver = _Resolver(events=events)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run_probe(
            512,
            resolver=resolver,
            factory=factory,
            control_hook=hook,
        )
    assert caught.value is original
    assert [packet[1] for packet, _ in udp_socket.sends] == [2, 2]
    assert udp_socket.close_calls == 1 and events[-1] == "close"


def test_interrupt_after_socket_acquired_observes_expired_deadline() -> None:
    original = KeyboardInterrupt("after socket acquired")
    hook = _ControlHook({"after_socket_acquired": [original]})
    clock = _Clock(10.0, 12.1)
    udp_socket = _Socket()
    factory = _SocketFactory(udp_socket)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run_probe(512, factory=factory, clock=clock, control_hook=hook)
    assert caught.value is original
    assert clock.calls == 2
    assert udp_socket.timeouts == [] and udp_socket.sends == []
    assert udp_socket.close_calls == 1


def test_only_exact_valid_success_becomes_healthy() -> None:
    report = validate_ddp_output(_settings(), _FakeProbe(_valid_result()))
    assert report.state is ComponentHealthState.HEALTHY
    assert report.reason_code == "validated"


def test_only_exact_cleanup_unconfirmed_result_becomes_degraded() -> None:
    degraded = _valid_result(
        reason_code="ddp_output_cleanup_unconfirmed",
        socket_was_closed=False,
        cleanup_completed=False,
    )
    report = validate_ddp_output(_settings(), _FakeProbe(degraded))
    assert report.state is ComponentHealthState.DEGRADED
    invalid = replace(degraded, blackout_completed=False)
    assert (
        validate_ddp_output(_settings(), _FakeProbe(invalid)).state
        is ComponentHealthState.UNHEALTHY
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"led_count": 2},
        {"frame_payload_bytes": 4},
        {"test_packets_planned": 0},
        {"test_packets_planned": 2},
        {"test_packets_sent": 0},
        {"blackout_packets_sent": 0},
        {"test_frame_completed": False},
        {"blackout_attempted": False},
        {"blackout_completed": False},
        {"socket_was_created": False},
        {"socket_was_closed": False},
        {"cleanup_completed": False},
        {"socket_was_created": 1},
        {"reason_code": 1},
        {"reason_code": "private-invalid-reason"},
    ],
)
def test_inconsistent_injected_results_become_unexpected(
    changes: dict[str, object],
) -> None:
    report = validate_ddp_output(
        _settings(), _FakeProbe(replace(_valid_result(), **changes))
    )
    assert report.state is ComponentHealthState.UNHEALTHY
    assert report.reason_code == "unexpected_ddp_output_failure"


@pytest.mark.parametrize(
    "flag",
    [
        "broadcast_was_used",
        "multicast_was_used",
        "discovery_was_used",
        "retry_was_used",
    ],
)
def test_unsafe_flags_remain_visible_in_unhealthy_public_report(flag: str) -> None:
    result = replace(_valid_result(), **{flag: True})
    report = validate_ddp_output(_settings(), _FakeProbe(result))
    assert report.state is ComponentHealthState.UNHEALTHY
    assert report.reason_code == "unexpected_ddp_output_failure"
    assert getattr(report, flag) is True


def test_probe_exception_and_wrong_type_are_sanitized() -> None:
    for fake in (
        _FakeProbe(_valid_result(), ValueError("private exception")),
        _FakeProbe(object()),
    ):
        report = validate_ddp_output(_settings(), fake)
        assert report.state is ComponentHealthState.UNHEALTHY
        assert report.reason_code == "unexpected_ddp_output_failure"
        assert "private" not in repr(report) and _HOST not in repr(report)


@pytest.mark.parametrize(
    ("state", "exit_code"),
    [
        (ComponentHealthState.HEALTHY, 0),
        (ComponentHealthState.DEGRADED, 1),
        (ComponentHealthState.UNHEALTHY, 1),
        (ComponentHealthState.DISABLED, 1),
    ],
)
def test_cli_command_wiring_and_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: ComponentHealthState,
    exit_code: int,
) -> None:
    report = _report(state, reason_code=state.value)
    monkeypatch.setattr("aurora_core.__main__.validate_ddp_output", lambda _: report)
    monkeypatch.setattr("sys.argv", ["aurora", "hardware", "validate", "ddp-output"])
    assert main() == exit_code
    output = capsys.readouterr().out
    assert f"DDP output validation: {state.value}" in output
    assert f"state: {state.value}" in output and f"reason: {state.value}" in output
    assert _SAFETY_STATEMENT in output


def test_cli_accepts_only_normal_config_and_log_level_options() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "hardware",
            "validate",
            "ddp-output",
            "--config",
            "safe.yaml",
            "--log-level",
            "DEBUG",
        ]
    )
    assert args.config.name == "safe.yaml" and args.log_level == "DEBUG"
    for forbidden in (
        "--host",
        "--port",
        "--led-count",
        "--color",
        "--destination-id",
        "--packet-size",
        "--timeout",
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(
                ["hardware", "validate", "ddp-output", forbidden, "private"]
            )


def test_cli_printer_is_sanitized_and_states_udp_limitations(
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = replace(
        _report(),
        message=(
            f"host={_HOST} ip={_IPV4} port={_PORT} sockaddr fd=41 "
            "packet=40010b01 rgb=000010 errno=5 secret=credential 0x1234"
        ),
    )
    _print_ddp_output_report(report)
    output = capsys.readouterr().out
    assert _SAFETY_STATEMENT in output
    assert (
        "UDP submission does not verify WLED receipt, LED output, or the complete "
        "lighting path."
    ) in output
    for forbidden in (
        _HOST,
        _IPV4,
        str(_PORT),
        "sockaddr",
        "fd=41",
        "40010b01",
        "000010",
        "errno=5",
        "credential",
        "0x1234",
    ):
        assert forbidden not in output


def test_cli_configuration_error_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        f"ddp:\n  enabled: true\n  host: {_HOST}\n  port: 0\n"
        "mqtt:\n  password: private-secret-canary\n"
    )
    monkeypatch.setattr(
        "sys.argv",
        ["aurora", "hardware", "validate", "ddp-output", "--config", str(path)],
    )
    assert main() == 1
    output = capsys.readouterr()
    assert "DDP output validation configuration failed." in output.err
    assert _SAFETY_STATEMENT in output.out
    assert _HOST not in output.err and "private-secret-canary" not in output.err


def test_fixed_safety_constants() -> None:
    assert DDP_DESTINATION_ID == 1
    assert DDP_RGB24_DATATYPE == 0x0B
    assert DDP_HEADER_LENGTH == 10
    assert DDP_MAX_DATA_PAYLOAD == 1400
    assert DDP_MAX_LEDS == 512
    assert DDP_MAX_FRAME_PAYLOAD == 1536
    assert DDP_MAX_DATAGRAMS_PER_FRAME == 2
    assert DDP_MAX_SEND_ATTEMPTS == 4
    assert DDP_DEADLINE_SECONDS == 2.0
    assert DDP_TEST_RGB == bytes((0, 0, 16))
    assert DDP_TEST_SEQUENCE == 1 and DDP_BLACKOUT_SEQUENCE == 2
