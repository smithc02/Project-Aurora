"""Bounded, operator-only DDP output probe with injectable network seams."""

from __future__ import annotations

import ipaddress
import socket
import struct
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Literal, Protocol, cast

from aurora_core.hardware.models import DDPOutputProbeResult

DDP_DESTINATION_ID = 1
DDP_RGB24_DATATYPE = 0x0B
DDP_HEADER_LENGTH = 10
DDP_MAX_DATA_PAYLOAD = 1400
DDP_MAX_LEDS = 512
DDP_MAX_FRAME_PAYLOAD = 1536
DDP_MAX_DATAGRAMS_PER_FRAME = 2
DDP_MAX_SEND_ATTEMPTS = 4
DDP_DEADLINE_SECONDS = 2.0
DDP_TEST_RGB = b"\x00\x00\x10"
DDP_BLACKOUT_RGB = b"\x00\x00\x00"
DDP_TEST_SEQUENCE = 1
DDP_BLACKOUT_SEQUENCE = 2

_DDP_VERSION = 0x40
_DDP_PUSH = 0x01
_DDP_HEADER = struct.Struct("!BBBBIH")

SocketAddress = tuple[str, int] | tuple[str, int, int, int]
DDPAddressInfo = tuple[int, int, int, str, tuple[object, ...]]


class DDPResolver(Protocol):
    """Resolve one configured endpoint using only UDP-compatible queries."""

    def resolve(self, *, host: str, port: int) -> Sequence[DDPAddressInfo]: ...


class DDPSocket(Protocol):
    def settimeout(self, value: float | None) -> None: ...

    def sendto(self, data: bytes, address: SocketAddress) -> int: ...

    def close(self) -> None: ...


class DDPSocketCleanup(Protocol):
    def __call__(
        self, udp_socket: DDPSocket, exception: BaseException | None
    ) -> _SocketCleanupOutcome: ...


class DDPSocketLease(Protocol):
    """Own bounded cleanup and the sole close for one acquired UDP socket."""

    @property
    def socket_was_acquired(self) -> bool: ...

    @property
    def socket_was_closed(self) -> bool: ...

    def register_cleanup(self, cleanup: DDPSocketCleanup) -> None: ...

    def __enter__(self) -> DDPSocket: ...

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool: ...


class DDPSocketFactory(Protocol):
    def acquire(
        self, *, family: int, socket_type: int, protocol: int
    ) -> DDPSocketLease: ...


class StandardLibraryDDPResolver:
    """Perform one blocking getaddrinfo call with no wall-clock cap claim."""

    def resolve(self, *, host: str, port: int) -> Sequence[DDPAddressInfo]:
        results = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_DGRAM,
            proto=socket.IPPROTO_UDP,
        )
        return cast(Sequence[DDPAddressInfo], results)


@dataclass(slots=True, repr=False)
class _StandardLibraryDDPSocketLease:
    family: int
    socket_type: int
    protocol: int
    _socket: DDPSocket | None = None
    _cleanup: DDPSocketCleanup | None = None
    _cleanup_started: bool = False
    _close_attempted: bool = False
    _socket_was_closed: bool = False

    @property
    def socket_was_acquired(self) -> bool:
        return self._socket is not None

    @property
    def socket_was_closed(self) -> bool:
        return self._socket_was_closed

    def register_cleanup(self, cleanup: DDPSocketCleanup) -> None:
        if self._socket is not None or self._cleanup is not None:
            raise RuntimeError("DDP socket cleanup is already registered")
        self._cleanup = cleanup

    def __enter__(self) -> DDPSocket:
        if self._cleanup is None:
            raise RuntimeError("DDP socket cleanup is not registered")
        try:
            self._socket = socket.socket(self.family, self.socket_type, self.protocol)
            return self._socket
        except BaseException as error:
            # If construction returned before an asynchronous interruption, the
            # registered cleanup already owns blackout and the sole close attempt.
            if self._socket is not None:
                self._finish(error)
            raise

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exception_type, traceback
        return self._finish(exception)

    def _finish(self, exception: BaseException | None) -> bool:
        active_interrupt = (
            exception if isinstance(exception, KeyboardInterrupt) else None
        )
        suppress_exception = False
        try:
            if not self._cleanup_started:
                self._cleanup_started = True
                assert self._cleanup is not None and self._socket is not None
                outcome = self._cleanup(self._socket, exception)
                active_interrupt = outcome.interruption or active_interrupt
                suppress_exception = outcome.suppress_exception
        except KeyboardInterrupt as error:
            active_interrupt = active_interrupt or error
        except Exception:
            suppress_exception = isinstance(exception, Exception)
        finally:
            try:
                self._close_once()
            except KeyboardInterrupt as error:
                active_interrupt = active_interrupt or error
            except Exception:
                pass
        if active_interrupt is not None and active_interrupt is not exception:
            raise active_interrupt
        return suppress_exception

    def _close_once(self) -> None:
        if self._socket is None or self._close_attempted:
            return
        self._close_attempted = True
        self._socket.close()
        self._socket_was_closed = True


class StandardLibraryDDPSocketFactory:
    """Acquire the one UDP socket through an interruption-safe ownership lease."""

    def acquire(
        self, *, family: int, socket_type: int, protocol: int
    ) -> DDPSocketLease:
        return _StandardLibraryDDPSocketLease(family, socket_type, protocol)


@dataclass(frozen=True, slots=True, repr=False)
class _ResolvedDestination:
    family: int
    address: SocketAddress


_SendStatus = Literal[
    "sent", "deadline", "budget", "failed", "partial", "unexpected", "interrupted"
]


@dataclass(frozen=True, slots=True, repr=False)
class _SendOutcome:
    status: _SendStatus
    attempts: int
    interrupted: bool = False
    interruption: KeyboardInterrupt | None = None


@dataclass(slots=True)
class _SocketUseState:
    attempts: int = 0
    test_sent: int = 0
    blackout_sent: int = 0
    test_reason: str | None = None
    blackout_reason: str | None = None
    blackout_attempted: bool = False
    socket_closed: bool = False
    deferred_interrupt: KeyboardInterrupt | None = None


@dataclass(frozen=True, slots=True, repr=False)
class _SocketCleanupOutcome:
    suppress_exception: bool = False
    interruption: KeyboardInterrupt | None = None


@dataclass(slots=True, repr=False)
class _DDPBlackoutCleanup:
    """Run the one blackout plan before socket ownership attempts close."""

    state: _SocketUseState
    destination: SocketAddress
    deadline: float
    blackout_packets: tuple[bytes, ...]
    run_blackout: Callable[
        [_SocketUseState, DDPSocket, SocketAddress, float, tuple[bytes, ...]], None
    ]
    prepare_close: Callable[[_SocketUseState], None]

    def __call__(
        self, udp_socket: DDPSocket, exception: BaseException | None
    ) -> _SocketCleanupOutcome:
        if isinstance(exception, KeyboardInterrupt):
            _defer_interrupt(self.state, exception)
        elif isinstance(exception, Exception) and self.state.test_reason is None:
            self.state.test_reason = "unexpected_ddp_output_failure"

        try:
            try:
                self.run_blackout(
                    self.state,
                    udp_socket,
                    self.destination,
                    self.deadline,
                    self.blackout_packets,
                )
            except KeyboardInterrupt as error:
                _defer_interrupt(self.state, error)
            except Exception:
                if self.state.blackout_reason is None:
                    self.state.blackout_reason = "unexpected_ddp_output_failure"
        finally:
            try:
                self.prepare_close(self.state)
            except KeyboardInterrupt as error:
                _defer_interrupt(self.state, error)
            except Exception:
                if self.state.blackout_reason is None:
                    self.state.blackout_reason = "unexpected_ddp_output_failure"
        return _SocketCleanupOutcome(
            suppress_exception=isinstance(exception, Exception),
            interruption=self.state.deferred_interrupt,
        )


def build_ddp_packets(payload: bytes, *, sequence: int) -> tuple[bytes, ...]:
    """Build one bounded version-1 DDP RGB24 frame without a timecode."""
    if type(sequence) is not int or sequence not in {
        DDP_TEST_SEQUENCE,
        DDP_BLACKOUT_SEQUENCE,
    }:
        raise ValueError("unsupported DDP sequence")
    if (
        type(payload) is not bytes
        or not payload
        or len(payload) > DDP_MAX_FRAME_PAYLOAD
    ):
        raise ValueError("DDP frame payload is outside the bounded limit")
    if _DDP_HEADER.size != DDP_HEADER_LENGTH:
        raise ValueError("DDP header size is invalid")
    chunks = tuple(
        payload[offset : offset + DDP_MAX_DATA_PAYLOAD]
        for offset in range(0, len(payload), DDP_MAX_DATA_PAYLOAD)
    )
    if not 1 <= len(chunks) <= DDP_MAX_DATAGRAMS_PER_FRAME:
        raise ValueError("DDP frame packet count is outside the bounded limit")
    packets = tuple(
        _DDP_HEADER.pack(
            _DDP_VERSION | (_DDP_PUSH if index == len(chunks) - 1 else 0),
            sequence,
            DDP_RGB24_DATATYPE,
            DDP_DESTINATION_ID,
            index * DDP_MAX_DATA_PAYLOAD,
            len(chunk),
        )
        + chunk
        for index, chunk in enumerate(chunks)
    )
    if not _packet_plan_is_valid(packets, payload, sequence):
        raise ValueError("constructed DDP packet plan is invalid")
    return packets


def _packet_plan_is_valid(
    packets: tuple[bytes, ...], payload: bytes, sequence: int
) -> bool:
    if not 1 <= len(packets) <= DDP_MAX_DATAGRAMS_PER_FRAME:
        return False
    rebuilt = bytearray()
    for index, packet in enumerate(packets):
        if len(packet) < DDP_HEADER_LENGTH:
            return False
        flags, actual_sequence, datatype, destination, offset, length = (
            _DDP_HEADER.unpack(packet[:DDP_HEADER_LENGTH])
        )
        chunk = packet[DDP_HEADER_LENGTH:]
        expected_flags = _DDP_VERSION | (_DDP_PUSH if index == len(packets) - 1 else 0)
        if (
            flags != expected_flags
            or actual_sequence != sequence
            or datatype != DDP_RGB24_DATATYPE
            or destination != DDP_DESTINATION_ID
            or offset != len(rebuilt)
            or length != len(chunk)
            or length > DDP_MAX_DATA_PAYLOAD
        ):
            return False
        rebuilt.extend(chunk)
    return bytes(rebuilt) == payload


class BoundedDDPOutputProbe:
    """Submit at most one static test frame followed by one blackout frame."""

    def __init__(
        self,
        *,
        resolver: DDPResolver | None = None,
        socket_factory: DDPSocketFactory | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        control_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._resolver = resolver or StandardLibraryDDPResolver()
        self._socket_factory = socket_factory or StandardLibraryDDPSocketFactory()
        self._monotonic = monotonic
        self._control_hook = control_hook

    def probe(self, *, host: str, port: int, led_count: int) -> DDPOutputProbeResult:
        """Perform the fixed bounded operation and return sanitized metadata only."""
        try:
            if type(led_count) is not int or not 1 <= led_count <= DDP_MAX_LEDS:
                return DDPOutputProbeResult("unexpected_ddp_output_failure")
            test_payload = DDP_TEST_RGB * led_count
            blackout_payload = DDP_BLACKOUT_RGB * led_count
            test_packets = build_ddp_packets(test_payload, sequence=DDP_TEST_SEQUENCE)
            blackout_packets = build_ddp_packets(
                blackout_payload, sequence=DDP_BLACKOUT_SEQUENCE
            )
            if (
                len(test_payload) != led_count * 3
                or len(test_payload) != len(blackout_payload)
                or len(test_packets) != len(blackout_packets)
            ):
                return DDPOutputProbeResult("unexpected_ddp_output_failure")
            # This caps send eligibility, not blocking resolution or command time.
            # Establish it here so resolution and socket creation consume its budget.
            deadline = self._monotonic() + DDP_DEADLINE_SECONDS
        except Exception:
            return DDPOutputProbeResult("unexpected_ddp_output_failure")

        try:
            destination, resolution_reason = self._resolve(host=host, port=port)
        except Exception:
            destination, resolution_reason = None, "destination_resolution_failed"
        if resolution_reason is not None or destination is None:
            return _pre_socket_result(
                resolution_reason or "destination_resolution_failed",
                led_count,
                len(test_payload),
                len(test_packets),
            )

        try:
            socket_lease = self._socket_factory.acquire(
                family=destination.family,
                socket_type=socket.SOCK_DGRAM,
                protocol=socket.IPPROTO_UDP,
            )
        except OSError:
            return _pre_socket_result(
                "socket_creation_failed",
                led_count,
                len(test_payload),
                len(test_packets),
            )
        except Exception:
            return _pre_socket_result(
                "unexpected_ddp_output_failure",
                led_count,
                len(test_payload),
                len(test_packets),
            )

        state = _SocketUseState()
        blackout_cleanup = _DDPBlackoutCleanup(
            state=state,
            destination=destination.address,
            deadline=deadline,
            blackout_packets=blackout_packets,
            run_blackout=self._run_blackout_phase,
            prepare_close=self._prepare_close,
        )
        try:
            socket_lease.register_cleanup(blackout_cleanup)
        except Exception:
            return _pre_socket_result(
                "unexpected_ddp_output_failure",
                led_count,
                len(test_payload),
                len(test_packets),
            )
        try:
            with socket_lease as udp_socket:
                self._control_point("after_socket_session_entered")
                self._use_socket(
                    state, udp_socket, destination.address, deadline, test_packets
                )
                if state.deferred_interrupt is not None:
                    raise state.deferred_interrupt
        except KeyboardInterrupt:
            raise
        except OSError:
            if not socket_lease.socket_was_acquired:
                return _pre_socket_result(
                    "socket_creation_failed",
                    led_count,
                    len(test_payload),
                    len(test_packets),
                )
            state.test_reason = "unexpected_ddp_output_failure"
        except Exception:
            if not socket_lease.socket_was_acquired:
                return _pre_socket_result(
                    "unexpected_ddp_output_failure",
                    led_count,
                    len(test_payload),
                    len(test_packets),
                )
            state.test_reason = "unexpected_ddp_output_failure"
        state.socket_closed = socket_lease.socket_was_closed
        return _socket_result(state, led_count, len(test_packets))

    def _resolve(
        self, *, host: str, port: int
    ) -> tuple[_ResolvedDestination | None, str | None]:
        results = self._resolver.resolve(host=host, port=port)
        unique: dict[tuple[int, SocketAddress], _ResolvedDestination] = {}
        unsafe_destination = False
        for result in results:
            normalized, unsafe = _normalize_destination(result, port)
            unsafe_destination = unsafe_destination or unsafe
            if normalized is not None:
                unique[(normalized.family, normalized.address)] = normalized
        if unsafe_destination:
            return None, "destination_not_unicast"
        if not unique:
            return None, "destination_resolution_failed"
        if len(unique) != 1:
            return None, "destination_resolution_ambiguous"
        return next(iter(unique.values())), None

    def _use_socket(
        self,
        state: _SocketUseState,
        udp_socket: DDPSocket,
        destination: SocketAddress,
        deadline: float,
        test_packets: tuple[bytes, ...],
    ) -> None:
        try:
            self._control_point("after_socket_acquired")
        except KeyboardInterrupt as error:
            _defer_interrupt(state, error)
        except Exception:
            state.test_reason = "unexpected_ddp_output_failure"
        else:
            try:
                self._run_test_phase(
                    state, udp_socket, destination, deadline, test_packets
                )
            except KeyboardInterrupt as error:
                _defer_interrupt(state, error)
            except Exception:
                state.test_reason = "unexpected_ddp_output_failure"

    def _run_test_phase(
        self,
        state: _SocketUseState,
        udp_socket: DDPSocket,
        destination: SocketAddress,
        deadline: float,
        packets: tuple[bytes, ...],
    ) -> None:
        for packet in packets:
            outcome = self._send_once(
                udp_socket, destination, packet, deadline, state.attempts
            )
            state.attempts = outcome.attempts
            if outcome.interrupted:
                assert outcome.interruption is not None
                _defer_interrupt(state, outcome.interruption)
                break
            if outcome.status == "sent":
                try:
                    self._control_point("after_test_send")
                except KeyboardInterrupt as error:
                    _defer_interrupt(state, error)
                    break
                except Exception:
                    state.test_reason = "unexpected_ddp_output_failure"
                    break
                state.test_sent += 1
                continue
            state.test_reason = _phase_reason("test_frame", outcome.status)
            break

    def _run_blackout_phase(
        self,
        state: _SocketUseState,
        udp_socket: DDPSocket,
        destination: SocketAddress,
        deadline: float,
        packets: tuple[bytes, ...],
    ) -> None:
        state.blackout_attempted = True
        try:
            self._control_point("before_blackout")
        except KeyboardInterrupt as error:
            _defer_interrupt(state, error)
        except Exception:
            state.blackout_reason = "unexpected_ddp_output_failure"
        for index, packet in enumerate(packets):
            outcome = self._send_once(
                udp_socket, destination, packet, deadline, state.attempts
            )
            state.attempts = outcome.attempts
            if outcome.interrupted:
                assert outcome.interruption is not None
                _defer_interrupt(state, outcome.interruption)
            elif outcome.status == "sent":
                state.blackout_sent += 1
            elif state.blackout_reason is None:
                state.blackout_reason = _phase_reason("blackout", outcome.status)
            if outcome.status in {"deadline", "budget"}:
                break
            if index < len(packets) - 1:
                try:
                    self._control_point("between_blackout_packets")
                except KeyboardInterrupt as error:
                    _defer_interrupt(state, error)
                except Exception:
                    if state.blackout_reason is None:
                        state.blackout_reason = "unexpected_ddp_output_failure"

    def _prepare_close(self, state: _SocketUseState) -> None:
        try:
            self._control_point("before_close")
        except KeyboardInterrupt as error:
            _defer_interrupt(state, error)
        except Exception:
            if state.blackout_reason is None:
                state.blackout_reason = "unexpected_ddp_output_failure"

    def _control_point(self, name: str) -> None:
        if self._control_hook is not None:
            self._control_hook(name)

    def _send_once(
        self,
        udp_socket: DDPSocket,
        destination: SocketAddress,
        packet: bytes,
        deadline: float,
        attempts: int,
    ) -> _SendOutcome:
        try:
            remaining = deadline - self._monotonic()
        except KeyboardInterrupt as error:
            return _SendOutcome("interrupted", attempts, True, error)
        except Exception:
            return _SendOutcome("unexpected", attempts)
        if remaining <= 0:
            return _SendOutcome("deadline", attempts)
        if attempts >= DDP_MAX_SEND_ATTEMPTS:
            return _SendOutcome("budget", attempts)
        try:
            udp_socket.settimeout(remaining)
        except KeyboardInterrupt as error:
            return _SendOutcome("interrupted", attempts, True, error)
        except OSError:
            return _SendOutcome("failed", attempts)
        except Exception:
            return _SendOutcome("unexpected", attempts)
        attempts += 1
        try:
            sent = udp_socket.sendto(packet, destination)
        except KeyboardInterrupt as error:
            return _SendOutcome("interrupted", attempts, True, error)
        except OSError:
            return _SendOutcome("failed", attempts)
        except Exception:
            return _SendOutcome("unexpected", attempts)
        if type(sent) is not int or sent != len(packet):
            return _SendOutcome("partial", attempts)
        return _SendOutcome("sent", attempts)


def _defer_interrupt(state: _SocketUseState, error: KeyboardInterrupt) -> None:
    if state.deferred_interrupt is None:
        state.deferred_interrupt = error


def _phase_reason(phase: str, outcome: _SendStatus) -> str:
    if outcome == "unexpected":
        return "unexpected_ddp_output_failure"
    suffix = {
        "deadline": "deadline_exceeded",
        "failed": "send_failed",
        "partial": "partial_send",
    }.get(outcome)
    if suffix is None:
        return "unexpected_ddp_output_failure"
    return f"{phase}_{suffix}"


def _pre_socket_result(
    reason_code: str, led_count: int, frame_payload_bytes: int, packets_planned: int
) -> DDPOutputProbeResult:
    return DDPOutputProbeResult(
        reason_code=reason_code,
        led_count=led_count,
        frame_payload_bytes=frame_payload_bytes,
        test_packets_planned=packets_planned,
        blackout_packets_planned=packets_planned,
    )


def _socket_result(
    state: _SocketUseState, led_count: int, packets_planned: int
) -> DDPOutputProbeResult:
    test_completed = state.test_sent == packets_planned
    blackout_completed = state.blackout_sent == packets_planned
    if state.blackout_reason is not None:
        reason = state.blackout_reason
    elif test_completed and blackout_completed and not state.socket_closed:
        reason = "ddp_output_cleanup_unconfirmed"
    elif state.test_reason is not None and blackout_completed:
        reason = state.test_reason
    elif test_completed and blackout_completed and state.socket_closed:
        reason = "validated"
    else:
        reason = "unexpected_ddp_output_failure"
    return DDPOutputProbeResult(
        reason_code=reason,
        socket_was_created=True,
        socket_was_closed=state.socket_closed,
        led_count=led_count,
        frame_payload_bytes=led_count * 3,
        test_packets_planned=packets_planned,
        test_packets_sent=state.test_sent,
        blackout_packets_planned=packets_planned,
        blackout_packets_sent=state.blackout_sent,
        test_frame_completed=test_completed,
        blackout_attempted=state.blackout_attempted,
        blackout_completed=blackout_completed,
        cleanup_completed=state.socket_closed,
    )


def _normalize_destination(
    result: DDPAddressInfo, expected_port: int
) -> tuple[_ResolvedDestination | None, bool]:
    try:
        family, socket_type, protocol, _canonical_name, raw_address = result
        if (
            isinstance(family, bool)
            or isinstance(socket_type, bool)
            or isinstance(protocol, bool)
            or family not in {socket.AF_INET, socket.AF_INET6}
            or socket_type != socket.SOCK_DGRAM
            or protocol not in {0, socket.IPPROTO_UDP}
        ):
            return None, False
        if family == socket.AF_INET:
            if len(raw_address) != 2:
                return None, False
            host, port = raw_address
            if not isinstance(host, str) or type(port) is not int:
                return None, False
            parsed_address: ipaddress.IPv4Address | ipaddress.IPv6Address = (
                ipaddress.IPv4Address(host)
            )
            normalized: SocketAddress = (str(parsed_address), port)
        else:
            if len(raw_address) != 4:
                return None, False
            host, port, flowinfo, scope_id = raw_address
            if (
                not isinstance(host, str)
                or type(port) is not int
                or type(flowinfo) is not int
                or type(scope_id) is not int
            ):
                return None, False
            parsed_address = ipaddress.IPv6Address(host)
            normalized = (str(parsed_address), port, flowinfo, scope_id)
        if port != expected_port:
            return None, False
        unsafe = parsed_address.is_multicast or parsed_address.is_unspecified
        if isinstance(parsed_address, ipaddress.IPv4Address):
            unsafe = unsafe or parsed_address == ipaddress.IPv4Address(
                "255.255.255.255"
            )
        elif parsed_address.ipv4_mapped is not None:
            mapped = parsed_address.ipv4_mapped
            unsafe = (
                unsafe
                or mapped.is_multicast
                or mapped.is_unspecified
                or mapped == ipaddress.IPv4Address("255.255.255.255")
            )
        if unsafe:
            return None, True
        return _ResolvedDestination(family, normalized), False
    except (TypeError, ValueError):
        return None, False
