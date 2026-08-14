"""Hardware-free tests for Milestone 15 bounded WLED controls."""

from __future__ import annotations

import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from functools import cache
from http import HTTPStatus
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlencode

import pytest

from aurora_core.config import AuroraConfigurationError, load_settings
from aurora_core.config.models import WLEDOperation
from aurora_core.control_plane.audit import SecurityAudit
from aurora_core.control_plane.contracts import (
    IMPLEMENTED_OPERATION_ORDER,
    POWER_OFF_CONFIRMATION_VALUE,
    BrightnessInput,
    NoOperationInput,
    OperationContract,
    operation_registry,
)
from aurora_core.control_plane.cookies import SESSION_COOKIE_NAME
from aurora_core.control_plane.service import ControlPlaneService
from aurora_core.control_plane.sessions import SessionContext
from aurora_core.control_plane.wled_adapter import (
    MAX_MUTATION_RESPONSE_BYTES,
    AdapterReason,
    AdapterResult,
    UrllibWLEDMutationTransport,
    WLEDMutationAdapter,
    WLEDMutationTransportFailure,
)
from aurora_core.control_plane.wled_service import (
    WLEDControlAvailability,
    WLEDControlService,
    WLEDControlStatus,
)
from aurora_core.dashboard.assets import PORTAL_CSS_PATH
from aurora_core.dashboard.models import ComponentHealth, HealthReport, HealthStatus
from aurora_core.dashboard.server import DashboardHandler
from aurora_core.security.passwords import hash_password


def _password() -> str:
    return "bounded-test-credential"


@cache
def _password_hash() -> str:
    return hash_password(_password(), salt=bytes(range(16)))


def _settings(
    *,
    authentication_enabled: bool = True,
    controls_enabled: bool = True,
    operations: tuple[str, ...] = (
        "wled.power_on",
        "wled.power_off",
        "wled.brightness_set",
    ),
    maximum_brightness: int = 200,
    operation_limit: int = 20,
    operation_window_seconds: int = 60,
):
    authentication: dict[str, object] = {"enabled": authentication_enabled}
    if authentication_enabled:
        authentication.update(
            {
                "username": "test_operator",
                "password_hash": _password_hash(),
                "session_ttl_minutes": 5,
                "maximum_sessions": 4,
            }
        )
    return load_settings(
        environment={},
        cli_overrides={
            "dashboard": {"authentication": authentication},
            "wled": {
                "enabled": True,
                "host": "device.invalid",
                "controls": {
                    "enabled": controls_enabled,
                    "allowed_operations": list(operations),
                    "timeout_seconds": 1.5,
                    "maximum_brightness": maximum_brightness,
                    "operation_limit": operation_limit,
                    "operation_window_seconds": operation_window_seconds,
                },
            },
        },
    )


class FakeTransport:
    def __init__(self, result: bytes | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def post_state(
        self,
        *,
        host: str,
        port: int,
        timeout_seconds: float,
        payload: bytes,
    ) -> bytes:
        self.calls.append(
            {
                "host": host,
                "port": port,
                "timeout_seconds": timeout_seconds,
                "payload": payload,
            }
        )
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeAdapter:
    def __init__(
        self,
        result: AdapterResult | None = None,
    ) -> None:
        self.result = (
            AdapterResult(True, AdapterReason.VERIFIED) if result is None else result
        )
        self.calls: list[tuple[WLEDOperation, object]] = []

    def execute(
        self,
        operation: WLEDOperation,
        operation_input: NoOperationInput | BrightnessInput,
    ) -> AdapterResult:
        self.calls.append((operation, operation_input))
        return self.result


class BlockingAdapter(FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def execute(
        self,
        operation: WLEDOperation,
        operation_input: NoOperationInput | BrightnessInput,
    ) -> AdapterResult:
        self.calls.append((operation, operation_input))
        self.entered.set()
        assert self.release.wait(timeout=2)
        return self.result


class RaisingAdapter(FakeAdapter):
    def execute(
        self,
        operation: WLEDOperation,
        operation_input: NoOperationInput | BrightnessInput,
    ) -> AdapterResult:
        self.calls.append((operation, operation_input))
        raise RuntimeError("private-device-response")


class StubHealthService:
    def __init__(self) -> None:
        self.calls = 0
        self.invalidations = 0
        self.report = HealthReport(
            status=HealthStatus.HEALTHY,
            checked_at="2026-01-01T00:00:00+00:00",
            service_uptime_seconds=12.0,
            components=(
                ComponentHealth(
                    name="wled",
                    status=HealthStatus.HEALTHY,
                    message="WLED state observed.",
                    checked_at="2026-01-01T00:00:00+00:00",
                    latency_ms=1.25,
                    details={"output_on": True, "brightness": 77},
                    last_successful_at="2026-01-01T00:00:00+00:00",
                ),
            ),
        )

    def get_health(self) -> HealthReport:
        self.calls += 1
        return self.report

    def invalidate(self) -> None:
        self.invalidations += 1


def _session(control: ControlPlaneService) -> tuple[str, SessionContext]:
    result = control.authenticate("test_operator", _password(), "client")
    assert result.created_session is not None
    token = result.created_session.token
    session = control.resolve_session(token)
    assert session is not None
    return token, session


def _headers(
    *,
    body: bytes | None = None,
    cookie: str | None = None,
    content_type: str | None = None,
    content_length: str | None = None,
    transfer_encoding: str | None = None,
) -> Message:
    headers = Message()
    if body is not None or content_length is not None:
        headers["Content-Length"] = (
            str(len(body or b"")) if content_length is None else content_length
        )
    if cookie is not None:
        headers["Cookie"] = cookie
    if content_type is not None:
        headers["Content-Type"] = content_type
    if transfer_encoding is not None:
        headers["Transfer-Encoding"] = transfer_encoding
    return headers


def _request(
    health: StubHealthService,
    control: ControlPlaneService,
    wled: WLEDControlService,
    path: str,
    *,
    method: str = "GET",
    body: bytes = b"",
    headers: Message | None = None,
) -> tuple[bytes, str, HTTPStatus, dict[str, str]]:
    handler = DashboardHandler.__new__(DashboardHandler)
    handler.server = SimpleNamespace(
        health_service=health,
        refresh_seconds=5,
        control_plane=control,
        wled_controls=wled,
    )
    handler.path = path
    handler.headers = Message() if headers is None else headers
    handler.rfile = io.BytesIO(body)
    handler.client_address = ("test-client", 0)
    responses: list[tuple[bytes, str, HTTPStatus, dict[str, str]]] = []

    def capture(
        response_body: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
        response_headers: dict[str, str] | None = None,
    ) -> None:
        responses.append(
            (
                response_body,
                content_type,
                status,
                {} if response_headers is None else dict(response_headers),
            )
        )

    handler._send = capture  # type: ignore[method-assign]
    getattr(handler, f"do_{method}")()
    return responses[-1]


def _http_services(
    *,
    operations: tuple[str, ...] = (
        "wled.power_on",
        "wled.power_off",
        "wled.brightness_set",
    ),
    controls_enabled: bool = True,
    adapter: FakeAdapter | None = None,
) -> tuple[
    StubHealthService,
    ControlPlaneService,
    WLEDControlService,
    str,
    SessionContext,
    FakeAdapter,
]:
    settings = _settings(
        operations=operations,
        controls_enabled=controls_enabled,
    )
    health = StubHealthService()
    control = ControlPlaneService(
        settings.dashboard.authentication,
        limiter_digest_key=b"login-limiter-key",
    )
    active_adapter = FakeAdapter() if adapter is None else adapter
    wled = WLEDControlService(
        settings.wled,
        authentication_enabled=True,
        adapter=active_adapter,
        limiter_digest_key=b"mutation-limiter-key",
        cache_invalidator=health.invalidate,
    )
    token, session = _session(control)
    cookie = f"{SESSION_COOKIE_NAME}={token}"
    return health, control, wled, cookie, session, active_adapter


def test_control_configuration_defaults_are_read_only_and_backward_compatible() -> None:
    settings = load_settings(environment={})
    assert not settings.wled.controls.enabled
    assert settings.wled.controls.allowed_operations == ()
    old = load_settings(
        environment={},
        cli_overrides={"wled": {"enabled": True, "host": "device.invalid"}},
    )
    assert not old.wled.controls.enabled


def test_valid_control_configuration_and_environment_mapping() -> None:
    settings = load_settings(
        environment={
            "AURORA_WLED__ENABLED": "true",
            "AURORA_WLED__HOST": "device.invalid",
            "AURORA_WLED__CONTROLS__ENABLED": "true",
            "AURORA_WLED__CONTROLS__ALLOWED_OPERATIONS": (
                "wled.power_on,wled.brightness_set"
            ),
            "AURORA_WLED__CONTROLS__TIMEOUT_SECONDS": "0.5",
            "AURORA_WLED__CONTROLS__MAXIMUM_BRIGHTNESS": "128",
            "AURORA_WLED__CONTROLS__OPERATION_LIMIT": "3",
            "AURORA_WLED__CONTROLS__OPERATION_WINDOW_SECONDS": "10",
        }
    )
    assert settings.wled.controls.enabled
    assert settings.wled.controls.allowed_operations == (
        WLEDOperation.POWER_ON,
        WLEDOperation.BRIGHTNESS_SET,
    )
    assert settings.wled.controls.timeout_seconds == 0.5
    assert settings.wled.controls.maximum_brightness == 128


@pytest.mark.parametrize(
    "controls",
    (
        {"allowed_operations": ["wled.unknown"]},
        {"allowed_operations": ["wled.power_on", "wled.power_on"]},
        {"allowed_operations": "wled.power_on"},
        {"timeout_seconds": 0.09},
        {"timeout_seconds": 5.1},
        {"maximum_brightness": 0},
        {"maximum_brightness": 256},
        {"operation_limit": 0},
        {"operation_limit": 121},
        {"operation_window_seconds": 0},
        {"operation_window_seconds": 3601},
    ),
)
def test_invalid_control_configuration_is_rejected(
    controls: dict[str, object],
) -> None:
    with pytest.raises(AuroraConfigurationError):
        load_settings(
            environment={},
            cli_overrides={
                "wled": {
                    "enabled": True,
                    "host": "device.invalid",
                    "controls": controls,
                }
            },
        )


@pytest.mark.parametrize(
    "wled",
    (
        {"controls": {"enabled": True}},
        {"enabled": False, "controls": {"enabled": True}},
    ),
)
def test_enabled_controls_require_enabled_validated_wled(
    wled: dict[str, object],
) -> None:
    with pytest.raises(AuroraConfigurationError) as error:
        load_settings(environment={}, cli_overrides={"wled": wled})
    assert "device.invalid" not in str(error.value)


def test_registry_contains_exact_typed_fixed_contracts_in_order() -> None:
    registry = operation_registry(1.25)
    assert tuple(item.operation_id for item in registry) == (
        WLEDOperation.POWER_ON,
        WLEDOperation.POWER_OFF,
        WLEDOperation.BRIGHTNESS_SET,
    )
    assert IMPLEMENTED_OPERATION_ORDER == tuple(item.operation_id for item in registry)
    assert registry[0].input_model is NoOperationInput
    assert registry[1].disruptive and registry[1].confirmation_metadata_id
    assert registry[2].input_model is BrightnessInput
    assert all(item.timeout_seconds == 1.25 for item in registry)
    with pytest.raises(ValueError):
        OperationContract(
            WLEDOperation.POWER_ON,
            dict,  # type: ignore[arg-type]
            1.0,
            "wled.fixed_state_adapter",
            False,
        )
    with pytest.raises(ValueError):
        BrightnessInput(True)  # type: ignore[arg-type]


def test_capabilities_are_dynamic_intersection_and_deterministic() -> None:
    settings = _settings(
        operations=("wled.brightness_set", "wled.power_on"),
    )
    service = WLEDControlService(
        settings.wled,
        authentication_enabled=True,
        adapter=FakeAdapter(),
        limiter_digest_key=b"key",
    )
    assert service.capabilities().to_dict() == {
        "schema_version": 1,
        "authenticated": True,
        "mutations_enabled": True,
        "available_operations": ["wled.power_on", "wled.brightness_set"],
    }
    assert all(
        contract.timeout_seconds == 1.5 for contract in service.operation_contracts
    )
    disabled = WLEDControlService(
        settings.wled,
        authentication_enabled=False,
        adapter=FakeAdapter(),
        limiter_digest_key=b"key",
    )
    assert disabled.capabilities().available_operations == ()
    assert not disabled.capabilities().mutations_enabled
    assert disabled.availability is WLEDControlAvailability.AUTHENTICATION_UNAVAILABLE
    empty_settings = _settings(operations=())
    empty = WLEDControlService(
        empty_settings.wled,
        authentication_enabled=True,
        adapter=FakeAdapter(),
        limiter_digest_key=b"key",
    )
    assert empty.availability is WLEDControlAvailability.NO_OPERATIONS


@pytest.mark.parametrize(
    ("operation", "operation_input", "response", "payload"),
    (
        (
            WLEDOperation.POWER_ON,
            NoOperationInput(),
            b'{"on":true}',
            b'{"on":true,"v":true}',
        ),
        (
            WLEDOperation.POWER_OFF,
            NoOperationInput(),
            b'{"on":false}',
            b'{"on":false,"v":true}',
        ),
        (
            WLEDOperation.BRIGHTNESS_SET,
            BrightnessInput(42),
            b'{"bri":42}',
            b'{"bri":42,"v":true}',
        ),
    ),
)
def test_adapter_generates_exact_payload_and_verifies_state(
    operation: WLEDOperation,
    operation_input: NoOperationInput | BrightnessInput,
    response: bytes,
    payload: bytes,
) -> None:
    transport = FakeTransport(response)
    adapter = WLEDMutationAdapter(
        host="device.invalid",
        port=80,
        timeout_seconds=1.5,
        transport=transport,
    )
    assert adapter.execute(operation, operation_input) == AdapterResult(
        True, AdapterReason.VERIFIED
    )
    assert len(transport.calls) == 1
    assert transport.calls[0] == {
        "host": "device.invalid",
        "port": 80,
        "timeout_seconds": 1.5,
        "payload": payload,
    }


@pytest.mark.parametrize(
    ("response", "reason"),
    (
        (b"{", AdapterReason.MALFORMED_JSON),
        (b"[]", AdapterReason.MALFORMED_JSON),
        (b"{}", AdapterReason.MISSING_EXPECTED_STATE),
        (b'{"on":false}', AdapterReason.STATE_VERIFICATION_MISMATCH),
        (b'{"on":1}', AdapterReason.STATE_VERIFICATION_MISMATCH),
        (b"x" * (MAX_MUTATION_RESPONSE_BYTES + 1), AdapterReason.OVERSIZED_RESPONSE),
    ),
)
def test_adapter_rejects_unverified_responses(
    response: bytes,
    reason: AdapterReason,
) -> None:
    transport = FakeTransport(response)
    result = WLEDMutationAdapter(
        host="device.invalid",
        port=80,
        timeout_seconds=1.0,
        transport=transport,
    ).execute(WLEDOperation.POWER_ON, NoOperationInput())
    assert result == AdapterResult(False, reason)
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "reason",
    (
        AdapterReason.CONNECTION_FAILURE,
        AdapterReason.TIMEOUT,
        AdapterReason.HTTP_REJECTION,
        AdapterReason.REDIRECT_REJECTION,
        AdapterReason.OVERSIZED_RESPONSE,
    ),
)
def test_adapter_sanitizes_transport_failures_without_retry(
    reason: AdapterReason,
) -> None:
    transport = FakeTransport(WLEDMutationTransportFailure(reason))
    adapter = WLEDMutationAdapter(
        host="device.invalid",
        port=80,
        timeout_seconds=1.0,
        transport=transport,
    )
    assert adapter.execute(WLEDOperation.POWER_ON, NoOperationInput()) == AdapterResult(
        False, reason
    )
    assert len(transport.calls) == 1
    assert "device.invalid" not in repr(adapter.execute)


def test_production_transport_is_fixed_post_bounded_and_rejects_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            captured["read_amount"] = amount
            return b'{"on":true}'

    response = Response()

    class Opener:
        def open(self, request: object, timeout: float) -> Response:
            captured["request"] = request
            captured["timeout"] = timeout
            return response

    monkeypatch.setattr(
        "aurora_core.control_plane.wled_adapter.build_opener",
        lambda handler: Opener(),
    )
    body = UrllibWLEDMutationTransport().post_state(
        host="device.invalid",
        port=81,
        timeout_seconds=1.25,
        payload=b'{"on":true,"v":true}',
    )
    request = captured["request"]
    assert body == b'{"on":true}' and response.closed
    assert request.get_method() == "POST"  # type: ignore[union-attr]
    assert request.full_url.endswith(":81/json/state")  # type: ignore[union-attr]
    assert request.data == b'{"on":true,"v":true}'  # type: ignore[union-attr]
    assert request.get_header("Content-type") == "application/json"  # type: ignore[union-attr]
    assert captured["timeout"] == 1.25
    assert captured["read_amount"] == MAX_MUTATION_RESPONSE_BYTES + 1

    class RedirectOpener:
        def open(self, request: object, timeout: float) -> None:
            raise HTTPError("redacted", 302, "redacted", {}, None)

    monkeypatch.setattr(
        "aurora_core.control_plane.wled_adapter.build_opener",
        lambda handler: RedirectOpener(),
    )
    with pytest.raises(WLEDMutationTransportFailure) as error:
        UrllibWLEDMutationTransport().post_state(
            host="device.invalid",
            port=80,
            timeout_seconds=1.0,
            payload=b"{}",
        )
    assert error.value.reason is AdapterReason.REDIRECT_REJECTION


def test_service_requires_auth_csrf_allowlist_confirmation_and_brightness_bound() -> (
    None
):
    settings = _settings(operations=("wled.power_off", "wled.brightness_set"))
    adapter = FakeAdapter()
    service = WLEDControlService(
        settings.wled,
        authentication_enabled=True,
        adapter=adapter,
        limiter_digest_key=b"key",
    )
    session = SessionContext("operator", "A" * 43, 60)
    assert service.power_off(None, None, None, "client").reason.value == (
        "authentication_required"
    )
    assert (
        service.power_off(session, None, None, "client").reason.value == "missing_csrf"
    )
    assert service.power_off(session, "B" * 43, None, "client").reason.value == (
        "invalid_csrf"
    )
    assert service.power_on(session, "A" * 43, "client").reason.value == (
        "operation_not_allowlisted"
    )
    assert service.power_off(session, "A" * 43, None, "client").reason.value == (
        "missing_confirmation"
    )
    assert service.power_off(session, "A" * 43, "wrong", "client").reason.value == (
        "invalid_confirmation"
    )
    assert service.set_brightness(session, "A" * 43, 0, "client").reason.value == (
        "invalid_brightness"
    )
    assert service.set_brightness(session, "A" * 43, 201, "client").reason.value == (
        "invalid_brightness"
    )
    assert adapter.calls == []


def test_verified_success_invalidates_once_but_failures_do_not() -> None:
    settings = _settings(operations=("wled.power_on",))
    invalidations: list[str] = []
    adapter = FakeAdapter()
    service = WLEDControlService(
        settings.wled,
        authentication_enabled=True,
        adapter=adapter,
        limiter_digest_key=b"key",
        cache_invalidator=lambda: invalidations.append("invalidate"),
    )
    session = SessionContext("operator", "A" * 43, 60)
    result = service.power_on(session, "A" * 43, "client")
    assert result.status is WLEDControlStatus.VERIFIED
    assert invalidations == ["invalidate"]

    adapter.result = AdapterResult(False, AdapterReason.MALFORMED_JSON)
    result = service.power_on(session, "A" * 43, "client")
    assert result.status is WLEDControlStatus.UNVERIFIED
    assert invalidations == ["invalidate"]


def test_unexpected_adapter_exception_is_sanitized_without_retry() -> None:
    settings = _settings(operations=("wled.power_on",))
    adapter = RaisingAdapter()
    service = WLEDControlService(
        settings.wled,
        authentication_enabled=True,
        adapter=adapter,
        limiter_digest_key=b"key",
    )
    session = SessionContext("operator", "A" * 43, 60)
    result = service.power_on(session, "A" * 43, "client")
    assert result.status is WLEDControlStatus.FAILED
    assert result.reason.value == "connection_failure"
    assert "private-device-response" not in repr(result)
    assert len(adapter.calls) == 1


def test_mutations_are_nonblocking_serialized_and_thread_safe() -> None:
    settings = _settings(operations=("wled.power_on",))
    adapter = BlockingAdapter()
    service = WLEDControlService(
        settings.wled,
        authentication_enabled=True,
        adapter=adapter,
        limiter_digest_key=b"key",
    )
    session = SessionContext("operator", "A" * 43, 60)
    with ThreadPoolExecutor(max_workers=2) as pool:
        active = pool.submit(service.power_on, session, "A" * 43, "client-a")
        assert adapter.entered.wait(timeout=1)
        busy = service.power_on(session, "A" * 43, "client-b")
        assert busy.status is WLEDControlStatus.BUSY
        adapter.release.set()
        assert active.result(timeout=1).status is WLEDControlStatus.VERIFIED
    assert len(adapter.calls) == 1


def test_mutation_limiter_is_monotonic_bounded_and_separate() -> None:
    now = [0.0]
    settings = _settings(
        operations=("wled.power_on",),
        operation_limit=1,
        operation_window_seconds=2,
    )
    adapter = FakeAdapter()
    service = WLEDControlService(
        settings.wled,
        authentication_enabled=True,
        adapter=adapter,
        clock=lambda: now[0],
        limiter_digest_key=b"key",
    )
    session = SessionContext("operator", "A" * 43, 60)
    assert service.power_on(session, "A" * 43, "client").status is (
        WLEDControlStatus.VERIFIED
    )
    assert service.power_on(session, "A" * 43, "client").status is (
        WLEDControlStatus.RATE_LIMITED
    )
    for number in range(300):
        service.power_on(session, "A" * 43, f"client-{number}")
    assert service.tracked_client_count <= 256
    now[0] = 3.0
    assert service.power_on(session, "A" * 43, "client").status is (
        WLEDControlStatus.VERIFIED
    )


def test_audit_events_are_fixed_and_redacted() -> None:
    events: list[tuple[str, dict[str, str | int]]] = []
    audit = SecurityAudit(lambda event, fields: events.append((event, dict(fields))))
    settings = _settings(operations=("wled.power_on",))
    adapter = FakeAdapter(AdapterResult(False, AdapterReason.TIMEOUT))
    service = WLEDControlService(
        settings.wled,
        authentication_enabled=True,
        adapter=adapter,
        limiter_digest_key=b"key",
        audit=audit,
    )
    session = SessionContext("private-user", "S" * 43, 60)
    service.power_on(session, "S" * 43, "private-client")
    brightness_settings = _settings(operations=("wled.brightness_set",))
    brightness_service = WLEDControlService(
        brightness_settings.wled,
        authentication_enabled=True,
        adapter=FakeAdapter(),
        limiter_digest_key=b"brightness-key",
        audit=audit,
    )
    brightness_service.set_brightness(session, "S" * 43, 42, "private-client")
    serialized = json.dumps(events)
    assert "wled_operation_failed" in serialized
    assert "wled.power_on" in serialized
    for forbidden in (
        "private-user",
        "private-client",
        "S" * 43,
        "device.invalid",
        "42",
    ):
        assert forbidden not in serialized


def test_success_verification_confirmation_rate_limit_and_busy_are_audited() -> None:
    events: list[tuple[str, dict[str, str | int]]] = []
    audit = SecurityAudit(lambda event, fields: events.append((event, dict(fields))))
    session = SessionContext("operator", "A" * 43, 60)

    settings = _settings(operation_limit=1)
    adapter = FakeAdapter()
    service = WLEDControlService(
        settings.wled,
        authentication_enabled=True,
        adapter=adapter,
        limiter_digest_key=b"key-one",
        audit=audit,
    )
    service.power_on(session, "A" * 43, "client")
    service.power_on(session, "A" * 43, "client")
    service.power_off(session, "A" * 43, None, "other-client")

    unverified_adapter = FakeAdapter(
        AdapterResult(False, AdapterReason.MISSING_EXPECTED_STATE)
    )
    unverified = WLEDControlService(
        settings.wled,
        authentication_enabled=True,
        adapter=unverified_adapter,
        limiter_digest_key=b"key-two",
        audit=audit,
    )
    unverified.power_on(session, "A" * 43, "client")

    blocking_adapter = BlockingAdapter()
    blocking = WLEDControlService(
        settings.wled,
        authentication_enabled=True,
        adapter=blocking_adapter,
        limiter_digest_key=b"key-three",
        audit=audit,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        active = pool.submit(blocking.power_on, session, "A" * 43, "client-a")
        assert blocking_adapter.entered.wait(timeout=1)
        blocking.power_on(session, "A" * 43, "client-b")
        blocking_adapter.release.set()
        active.result(timeout=1)

    serialized = json.dumps(events)
    for event in (
        "wled_operation_succeeded",
        "wled_operation_rate_limited",
        "wled_confirmation_rejected",
        "wled_state_verification_failed",
        "wled_operation_busy",
    ):
        assert event in serialized


def test_authenticated_wled_page_uses_one_shared_snapshot_and_fixed_forms() -> None:
    health, control, wled, cookie, session, adapter = _http_services()
    body, content_type, status, _ = _request(
        health,
        control,
        wled,
        "/controls/wled",
        headers=_headers(cookie=cookie),
    )
    assert status is HTTPStatus.OK and content_type.startswith("text/html")
    assert health.calls == 1 and adapter.calls == []
    assert b"WLED controls available" in body
    assert b'action="/controls/wled/power-on"' in body
    assert b'action="/controls/wled/power-off"' in body
    assert b'action="/controls/wled/brightness"' in body
    assert b"Confirm that lighting may black out" in body
    assert b'max="200"' in body
    assert body.count(session.csrf_token.encode()) == 3
    assert b"device.invalid" not in body
    assert b"json/state" not in body


def test_disabled_operations_are_not_rendered_and_dynamic_status_does_not_poll() -> (
    None
):
    health, control, wled, cookie, _, adapter = _http_services(
        operations=("wled.power_on",)
    )
    body, _, status, _ = _request(
        health,
        control,
        wled,
        "/api/control/status",
        headers=_headers(cookie=cookie),
    )
    assert status is HTTPStatus.OK
    assert json.loads(body) == {
        "schema_version": 1,
        "authenticated": True,
        "mutations_enabled": True,
        "available_operations": ["wled.power_on"],
    }
    assert health.calls == 0 and adapter.calls == []
    page, _, _, _ = _request(
        health,
        control,
        wled,
        "/controls/wled",
        headers=_headers(cookie=cookie),
    )
    assert b"power-on" in page
    assert b"power-off" not in page
    assert b'action="/controls/wled/brightness"' not in page


def test_controls_page_uses_one_cached_report_and_no_direct_wled_request() -> None:
    health, control, wled, cookie, _, adapter = _http_services()
    controls, _, status, _ = _request(
        health,
        control,
        wled,
        "/controls",
        headers=_headers(cookie=cookie),
    )
    assert status is HTTPStatus.OK
    assert b"WLED controls available" in controls
    assert b'href="/controls/wled"' in controls
    _request(health, control, wled, PORTAL_CSS_PATH)
    assert health.calls == 1 and adapter.calls == []


def test_public_health_api_remains_schema_version_one_with_controls_enabled() -> None:
    health, control, wled, _, _, adapter = _http_services()
    body, content_type, status, _ = _request(
        health,
        control,
        wled,
        "/api/health",
    )
    payload = json.loads(body)
    assert status is HTTPStatus.OK
    assert content_type == "application/json; charset=utf-8"
    assert payload["schema_version"] == 1
    assert set(payload) == {
        "status",
        "checked_at",
        "service_uptime_seconds",
        "components",
        "schema_version",
    }
    assert health.calls == 1 and adapter.calls == []


@pytest.mark.parametrize(
    ("path", "fields", "expected_operation"),
    (
        (
            "/controls/wled/power-on",
            {},
            WLEDOperation.POWER_ON,
        ),
        (
            "/controls/wled/power-off",
            {"confirmation": POWER_OFF_CONFIRMATION_VALUE},
            WLEDOperation.POWER_OFF,
        ),
        (
            "/controls/wled/brightness",
            {"brightness": "42"},
            WLEDOperation.BRIGHTNESS_SET,
        ),
    ),
)
def test_authenticated_csrf_protected_routes_execute_once_and_redirect(
    path: str,
    fields: dict[str, str],
    expected_operation: WLEDOperation,
) -> None:
    health, control, wled, cookie, session, adapter = _http_services()
    body = urlencode({"csrf_token": session.csrf_token, **fields}).encode()
    response, _, status, headers = _request(
        health,
        control,
        wled,
        path,
        method="POST",
        body=body,
        headers=_headers(
            body=body,
            cookie=cookie,
            content_type="application/x-www-form-urlencoded",
        ),
    )
    assert response == b"" and status is HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/controls/wled?notice=verified"
    assert len(adapter.calls) == 1
    assert adapter.calls[0][0] is expected_operation
    assert health.invalidations == 1


def test_mutation_routes_fail_closed_for_authentication_and_allowlist() -> None:
    health, control, wled, _, session, adapter = _http_services(
        operations=("wled.power_off",)
    )
    body = urlencode({"csrf_token": session.csrf_token}).encode()
    _, _, status, headers = _request(
        health,
        control,
        wled,
        "/controls/wled/power-on",
        method="POST",
        body=body,
        headers=_headers(
            body=body,
            content_type="application/x-www-form-urlencoded",
        ),
    )
    assert status is HTTPStatus.SEE_OTHER
    assert headers["Location"].startswith("/login?")
    assert adapter.calls == []

    _, _, status, headers = _request(
        health,
        control,
        wled,
        "/controls/wled/power-on",
        method="POST",
        body=body,
        headers=_headers(
            body=body,
            cookie=f"{SESSION_COOKIE_NAME}=unknown",
            content_type="application/x-www-form-urlencoded",
        ),
    )
    assert status is HTTPStatus.SEE_OTHER
    assert headers["Location"].startswith("/login?")

    token, active_session = _session(control)
    cookie = f"{SESSION_COOKIE_NAME}={token}"
    body = urlencode({"csrf_token": active_session.csrf_token}).encode()
    _, _, status, headers = _request(
        health,
        control,
        wled,
        "/controls/wled/power-on",
        method="POST",
        body=body,
        headers=_headers(
            body=body,
            cookie=cookie,
            content_type="application/x-www-form-urlencoded",
        ),
    )
    assert status is HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/controls/wled?notice=denied"
    assert adapter.calls == []


def test_authentication_disabled_and_controls_disabled_mutations_fail_closed() -> None:
    disabled_auth_settings = _settings(authentication_enabled=False)
    health = StubHealthService()
    control = ControlPlaneService(disabled_auth_settings.dashboard.authentication)
    adapter = FakeAdapter()
    wled = WLEDControlService(
        disabled_auth_settings.wled,
        authentication_enabled=False,
        adapter=adapter,
        limiter_digest_key=b"key",
    )
    _, _, status, _ = _request(
        health,
        control,
        wled,
        "/controls/wled/power-on",
        method="POST",
    )
    assert status is HTTPStatus.NOT_FOUND

    health, control, wled, cookie, session, adapter = _http_services(
        controls_enabled=False
    )
    body = urlencode({"csrf_token": session.csrf_token}).encode()
    _, _, status, headers = _request(
        health,
        control,
        wled,
        "/controls/wled/power-on",
        method="POST",
        body=body,
        headers=_headers(
            body=body,
            cookie=cookie,
            content_type="application/x-www-form-urlencoded",
        ),
    )
    assert status is HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/controls/wled?notice=denied"
    assert adapter.calls == []


def test_mutation_routes_reject_missing_and_repeated_content_length() -> None:
    health, control, wled, cookie, _, adapter = _http_services()
    body = b"csrf_token=x"
    missing = Message()
    missing["Content-Type"] = "application/x-www-form-urlencoded"
    missing["Cookie"] = cookie
    _, _, status, _ = _request(
        health,
        control,
        wled,
        "/controls/wled/power-on",
        method="POST",
        body=body,
        headers=missing,
    )
    assert status is HTTPStatus.LENGTH_REQUIRED

    repeated = _headers(
        body=body,
        cookie=cookie,
        content_type="application/x-www-form-urlencoded",
    )
    repeated["Content-Length"] = str(len(body))
    _, _, status, _ = _request(
        health,
        control,
        wled,
        "/controls/wled/power-on",
        method="POST",
        body=body,
        headers=repeated,
    )
    assert status is HTTPStatus.BAD_REQUEST
    assert adapter.calls == []


@pytest.mark.parametrize(
    ("body", "headers", "expected_status"),
    (
        (b"csrf_token=x", {}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE),
        (
            b"csrf_token=x",
            {"content_type": "text/plain"},
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
        ),
        (
            b"csrf_token=x",
            {
                "content_type": "application/x-www-form-urlencoded",
                "content_length": "-1",
            },
            HTTPStatus.BAD_REQUEST,
        ),
        (
            b"x" * 1025,
            {"content_type": "application/x-www-form-urlencoded"},
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
        ),
        (
            b"csrf_token=x",
            {
                "content_type": "application/x-www-form-urlencoded",
                "transfer_encoding": "chunked",
            },
            HTTPStatus.BAD_REQUEST,
        ),
        (
            b"csrf_token=%GG",
            {"content_type": "application/x-www-form-urlencoded"},
            HTTPStatus.BAD_REQUEST,
        ),
        (
            b"csrf_token=%FF",
            {"content_type": "application/x-www-form-urlencoded"},
            HTTPStatus.BAD_REQUEST,
        ),
        (
            b"csrf_token=x&csrf_token=y",
            {"content_type": "application/x-www-form-urlencoded"},
            HTTPStatus.BAD_REQUEST,
        ),
        (
            b"csrf_token=x&unknown=y",
            {"content_type": "application/x-www-form-urlencoded"},
            HTTPStatus.BAD_REQUEST,
        ),
    ),
)
def test_mutation_request_boundary_rejects_malformed_forms(
    body: bytes,
    headers: dict[str, str],
    expected_status: HTTPStatus,
) -> None:
    health, control, wled, cookie, _, adapter = _http_services()
    request_headers = _headers(body=body, cookie=cookie, **headers)
    _, _, status, _ = _request(
        health,
        control,
        wled,
        "/controls/wled/power-on",
        method="POST",
        body=body,
        headers=request_headers,
    )
    assert status is expected_status
    assert adapter.calls == []


@pytest.mark.parametrize("brightness", ("", "0", "-1", "+1", "1.0", "01", "256"))
def test_brightness_form_accepts_only_canonical_bounded_integer(
    brightness: str,
) -> None:
    health, control, wled, cookie, session, adapter = _http_services()
    body = urlencode(
        {"csrf_token": session.csrf_token, "brightness": brightness}
    ).encode()
    _, _, status, headers = _request(
        health,
        control,
        wled,
        "/controls/wled/brightness",
        method="POST",
        body=body,
        headers=_headers(
            body=body,
            cookie=cookie,
            content_type="application/x-www-form-urlencoded",
        ),
    )
    assert status is HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/controls/wled?notice=invalid"
    assert adapter.calls == []


def test_invalid_brightness_cannot_bypass_csrf_validation() -> None:
    health, control, wled, cookie, _, adapter = _http_services()
    body = urlencode({"brightness": "invalid"}).encode()
    _, _, status, headers = _request(
        health,
        control,
        wled,
        "/controls/wled/brightness",
        method="POST",
        body=body,
        headers=_headers(
            body=body,
            cookie=cookie,
            content_type="application/x-www-form-urlencoded",
        ),
    )
    assert status is HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/controls/wled?notice=denied"
    assert adapter.calls == []


def test_missing_csrf_and_power_off_confirmation_never_call_adapter() -> None:
    health, control, wled, cookie, session, adapter = _http_services()
    for path, fields in (
        ("/controls/wled/power-on", {}),
        ("/controls/wled/power-off", {"csrf_token": session.csrf_token}),
        (
            "/controls/wled/power-off",
            {"csrf_token": session.csrf_token, "confirmation": "wrong"},
        ),
    ):
        body = urlencode(fields).encode()
        _, _, status, headers = _request(
            health,
            control,
            wled,
            path,
            method="POST",
            body=body,
            headers=_headers(
                body=body,
                cookie=cookie,
                content_type="application/x-www-form-urlencoded",
            ),
        )
        assert status is HTTPStatus.SEE_OTHER
        assert headers["Location"] == "/controls/wled?notice=denied"
    assert adapter.calls == []


def test_wled_notices_and_dynamic_health_values_are_escaped() -> None:
    health, control, wled, cookie, _, _ = _http_services()
    health.report = HealthReport(
        status=HealthStatus.DEGRADED,
        checked_at="safe",
        service_uptime_seconds=1,
        components=(
            ComponentHealth(
                "wled",
                HealthStatus.DEGRADED,
                "safe",
                "safe",
                1,
                {"brightness": "<script>private.invalid</script>"},
                "<last-observation>",
            ),
        ),
    )
    body, _, status, _ = _request(
        health,
        control,
        wled,
        "/controls/wled?notice=unverified&next=https://evil.invalid",
        headers=_headers(cookie=cookie),
    )
    assert status is HTTPStatus.OK
    assert b"State could not be verified" in body
    assert b"&lt;last-observation&gt;" in body
    assert b"<last-observation>" not in body
    assert b"evil.invalid" not in body
    assert b"private.invalid" not in body


def test_unsupported_generic_mutation_routes_and_methods_are_absent() -> None:
    health, control, wled, cookie, _, adapter = _http_services()
    for path in ("/controls/wled/execute", "/api/control/execute", "/api/wled"):
        _, _, status, headers = _request(
            health,
            control,
            wled,
            path,
            method="POST",
            headers=_headers(cookie=cookie),
        )
        assert status is HTTPStatus.METHOD_NOT_ALLOWED
        assert headers["Allow"] == "GET"
    _, _, status, headers = _request(
        health,
        control,
        wled,
        "/controls/wled/power-on",
        method="PUT",
        headers=_headers(cookie=cookie),
    )
    assert status is HTTPStatus.METHOD_NOT_ALLOWED
    assert headers["Allow"] == "POST"
    _, _, status, headers = _request(
        health,
        control,
        wled,
        "/controls/wled/power-on",
        headers=_headers(cookie=cookie),
    )
    assert status is HTTPStatus.METHOD_NOT_ALLOWED
    assert headers["Allow"] == "POST"
    assert adapter.calls == []
