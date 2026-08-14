"""Hardware-free tests for Milestone 16 bounded HyperHDR controls."""

from __future__ import annotations

import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from functools import cache
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

import pytest

from aurora_core.config import AuroraConfigurationError, load_settings
from aurora_core.config.models import HyperHDROperation
from aurora_core.control_plane.audit import SecurityAudit
from aurora_core.control_plane.contracts import (
    HYPERHDR_IMPLEMENTED_OPERATION_ORDER,
    LED_OUTPUT_DISABLE_CONFIRMATION_VALUE,
    VIDEO_GRABBER_DISABLE_CONFIRMATION_VALUE,
    NoOperationInput,
    hyperhdr_operation_registry,
)
from aurora_core.control_plane.cookies import SESSION_COOKIE_NAME
from aurora_core.control_plane.hyperhdr_adapter import (
    MAX_MUTATION_RESPONSE_BYTES,
    HyperHDRAdapterReason,
    HyperHDRAdapterResult,
    HyperHDRMutationAdapter,
    HyperHDRMutationTransportFailure,
    UrllibHyperHDRMutationTransport,
)
from aurora_core.control_plane.hyperhdr_service import (
    HyperHDRControlAvailability,
    HyperHDRControlService,
    HyperHDRControlStatus,
)
from aurora_core.control_plane.service import ControlPlaneService
from aurora_core.control_plane.sessions import SessionContext
from aurora_core.control_plane.wled_service import WLEDControlService
from aurora_core.dashboard.models import ComponentHealth, HealthReport, HealthStatus
from aurora_core.dashboard.server import DashboardHandler
from aurora_core.hardware.errors import HyperHDRTimeoutError
from aurora_core.security.passwords import hash_password

ALL_OPERATIONS = (
    "hyperhdr.video_grabber_enable",
    "hyperhdr.video_grabber_disable",
    "hyperhdr.led_output_enable",
    "hyperhdr.led_output_disable",
)


def _password() -> str:
    return "hyperhdr-test-credential"


@cache
def _password_hash() -> str:
    return hash_password(_password(), salt=bytes(range(16)))


def _settings(
    *,
    authentication_enabled: bool = True,
    hyperhdr_enabled: bool = True,
    controls_enabled: bool = True,
    operations: tuple[str, ...] = ALL_OPERATIONS,
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
            "hyperhdr": {
                "enabled": hyperhdr_enabled,
                "host": "hyperhdr.invalid",
                "port": 8090,
                "controls": {
                    "enabled": controls_enabled,
                    "allowed_operations": list(operations),
                    "timeout_seconds": 1.5,
                    "operation_limit": operation_limit,
                    "operation_window_seconds": operation_window_seconds,
                },
            },
        },
    )


def _write_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "aurora.yaml"
    path.write_text(
        """
hyperhdr:
  enabled: true
  host: hyperhdr.invalid
  port: 8090
  controls:
    enabled: true
    allowed_operations:
      - hyperhdr.video_grabber_disable
    timeout_seconds: 1.0
    operation_limit: 2
    operation_window_seconds: 30
""".strip(),
        encoding="utf-8",
    )
    return path


class FakeMutationTransport:
    def __init__(self, result: bytes | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def post_component_state(
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


class FakeServerInfoTransport:
    def __init__(self, result: bytes | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def fetch_server_info(
        self,
        *,
        host: str,
        port: int,
        timeout_seconds: float,
    ) -> bytes:
        self.calls.append(
            {
                "host": host,
                "port": port,
                "timeout_seconds": timeout_seconds,
            }
        )
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _serverinfo(component: str, state: bool) -> bytes:
    return json.dumps(
        {
            "success": True,
            "command": "serverinfo",
            "info": {"components": [{"name": component, "enabled": state}]},
        },
        separators=(",", ":"),
    ).encode()


class FakeAdapter:
    def __init__(self, result: HyperHDRAdapterResult | None = None) -> None:
        self.result = (
            HyperHDRAdapterResult(True, True, HyperHDRAdapterReason.VERIFIED)
            if result is None
            else result
        )
        self.calls: list[HyperHDROperation] = []

    def execute(self, operation: HyperHDROperation) -> HyperHDRAdapterResult:
        self.calls.append(operation)
        return self.result


class BlockingAdapter(FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def execute(self, operation: HyperHDROperation) -> HyperHDRAdapterResult:
        self.calls.append(operation)
        self.entered.set()
        assert self.release.wait(timeout=2)
        return self.result


class RaisingAdapter(FakeAdapter):
    def execute(self, operation: HyperHDROperation) -> HyperHDRAdapterResult:
        self.calls.append(operation)
        raise RuntimeError("private-hyperhdr-response")


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
                    name="hyperhdr",
                    status=HealthStatus.HEALTHY,
                    message="HyperHDR state observed.",
                    checked_at="2026-01-01T00:00:00+00:00",
                    latency_ms=1.25,
                    details={
                        "instance_running": True,
                        "grabber_active": True,
                        "led_output_active": True,
                    },
                    last_successful_at="2026-01-01T00:00:00+00:00",
                ),
            ),
        )

    def get_health(self) -> HealthReport:
        self.calls += 1
        return self.report

    def invalidate(self) -> None:
        self.invalidations += 1


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
    hyperhdr: HyperHDRControlService,
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
        hyperhdr_controls=hyperhdr,
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
    operations: tuple[str, ...] = ALL_OPERATIONS,
    controls_enabled: bool = True,
    adapter: FakeAdapter | None = None,
    wled_operations: tuple[str, ...] = (),
) -> tuple[
    StubHealthService,
    ControlPlaneService,
    HyperHDRControlService,
    WLEDControlService,
    str,
    SessionContext,
    FakeAdapter,
]:
    settings = _settings(operations=operations, controls_enabled=controls_enabled)
    health = StubHealthService()
    control = ControlPlaneService(
        settings.dashboard.authentication,
        limiter_digest_key=b"login-limiter-key",
    )
    active_adapter = FakeAdapter() if adapter is None else adapter
    hyperhdr = HyperHDRControlService(
        settings.hyperhdr,
        authentication_enabled=True,
        adapter=active_adapter,
        limiter_digest_key=b"hyperhdr-mutation-limiter-key",
        cache_invalidator=health.invalidate,
    )
    wled_settings = load_settings(
        environment={},
        cli_overrides={
            "wled": {
                "enabled": True,
                "host": "wled.invalid",
                "controls": {
                    "enabled": bool(wled_operations),
                    "allowed_operations": list(wled_operations),
                },
            }
        },
    )
    wled = WLEDControlService(
        wled_settings.wled,
        authentication_enabled=True,
        limiter_digest_key=b"wled-mutation-limiter-key",
    )
    login = control.authenticate("test_operator", _password(), "client")
    assert login.created_session is not None
    token = login.created_session.token
    session = control.resolve_session(token)
    assert session is not None
    return (
        health,
        control,
        hyperhdr,
        wled,
        f"{SESSION_COOKIE_NAME}={token}",
        session,
        active_adapter,
    )


def test_configuration_defaults_are_disabled_and_empty() -> None:
    settings = load_settings(environment={})
    assert not settings.hyperhdr.controls.enabled
    assert settings.hyperhdr.controls.allowed_operations == ()
    assert settings.hyperhdr.controls.timeout_seconds == 2.0
    assert settings.hyperhdr.controls.operation_limit == 20
    assert settings.hyperhdr.controls.operation_window_seconds == 60


def test_all_exact_operations_and_real_environment_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = _write_yaml(tmp_path)
    monkeypatch.setenv(
        "AURORA_HYPERHDR__CONTROLS__ALLOWED_OPERATIONS",
        ",".join(ALL_OPERATIONS),
    )
    settings = load_settings(config_path=path)
    assert settings.hyperhdr.controls.allowed_operations == tuple(HyperHDROperation)


@pytest.mark.parametrize(
    "value",
    (
        "hyperhdr.unknown",
        "hyperhdr.video_grabber_enable,hyperhdr.video_grabber_enable",
        "hyperhdr.video_grabber_enable,,hyperhdr.led_output_enable",
        "hyperhdr.video_grabber_enable, ,hyperhdr.led_output_enable",
    ),
)
def test_real_environment_invalid_allowlists_fail_without_echoing_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    value: str,
) -> None:
    path = _write_yaml(tmp_path)
    monkeypatch.setenv(
        "AURORA_HYPERHDR__CONTROLS__ALLOWED_OPERATIONS",
        value,
    )
    with pytest.raises(AuroraConfigurationError) as error:
        load_settings(config_path=path)
    assert value not in str(error.value)


def test_empty_environment_allowlist_is_empty_tuple(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = _write_yaml(tmp_path)
    monkeypatch.setenv("AURORA_HYPERHDR__CONTROLS__ALLOWED_OPERATIONS", "")
    assert load_settings(config_path=path).hyperhdr.controls.allowed_operations == ()


def test_yaml_environment_cli_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _write_yaml(tmp_path)
    monkeypatch.setenv("AURORA_HYPERHDR__CONTROLS__TIMEOUT_SECONDS", "2.5")
    environment = load_settings(config_path=path)
    assert environment.hyperhdr.controls.timeout_seconds == 2.5
    cli = load_settings(
        config_path=path,
        cli_overrides={"hyperhdr": {"controls": {"timeout_seconds": 3.5}}},
    )
    assert cli.hyperhdr.controls.timeout_seconds == 3.5
    monkeypatch.delenv("AURORA_HYPERHDR__CONTROLS__TIMEOUT_SECONDS")
    assert load_settings(config_path=path).hyperhdr.controls.timeout_seconds == 1.0


@pytest.mark.parametrize(
    "controls",
    (
        {"timeout_seconds": 0.09},
        {"timeout_seconds": 5.1},
        {"operation_limit": 0},
        {"operation_limit": 121},
        {"operation_window_seconds": 0},
        {"operation_window_seconds": 3601},
    ),
)
def test_strict_configuration_bounds(controls: dict[str, object]) -> None:
    with pytest.raises(AuroraConfigurationError):
        load_settings(
            environment={},
            cli_overrides={
                "hyperhdr": {
                    "enabled": True,
                    "host": "hyperhdr.invalid",
                    "port": 8090,
                    "controls": controls,
                }
            },
        )


@pytest.mark.parametrize(
    "hyperhdr",
    (
        {"controls": {"enabled": True}},
        {
            "enabled": True,
            "host": "hyperhdr.invalid",
            "controls": {"enabled": True},
        },
        {
            "enabled": False,
            "host": "hyperhdr.invalid",
            "port": 8090,
            "controls": {"enabled": True},
        },
    ),
)
def test_enabled_controls_require_enabled_validated_endpoint(
    hyperhdr: dict[str, object],
) -> None:
    with pytest.raises(AuroraConfigurationError) as error:
        load_settings(environment={}, cli_overrides={"hyperhdr": hyperhdr})
    assert "hyperhdr.invalid" not in str(error.value)


def test_registry_is_exact_typed_and_deterministic() -> None:
    registry = hyperhdr_operation_registry(1.25)
    assert tuple(item.operation_id for item in registry) == tuple(HyperHDROperation)
    assert HYPERHDR_IMPLEMENTED_OPERATION_ORDER == tuple(HyperHDROperation)
    assert all(item.input_model is NoOperationInput for item in registry)
    assert tuple(item.disruptive for item in registry) == (False, True, False, True)
    assert all(item.timeout_seconds == 1.25 for item in registry)


@pytest.mark.parametrize(
    ("operation", "component", "state"),
    (
        (HyperHDROperation.VIDEO_GRABBER_ENABLE, "VIDEOGRABBER", True),
        (HyperHDROperation.VIDEO_GRABBER_DISABLE, "VIDEOGRABBER", False),
        (HyperHDROperation.LED_OUTPUT_ENABLE, "LEDDEVICE", True),
        (HyperHDROperation.LED_OUTPUT_DISABLE, "LEDDEVICE", False),
    ),
)
def test_adapter_generates_exact_payload_then_verifies_once(
    operation: HyperHDROperation,
    component: str,
    state: bool,
) -> None:
    mutation = FakeMutationTransport(b'{"success":true,"command":"componentstate"}')
    verification = FakeServerInfoTransport(_serverinfo(component, state))
    result = HyperHDRMutationAdapter(
        host="hyperhdr.invalid",
        port=8090,
        timeout_seconds=1.5,
        mutation_transport=mutation,
        server_info_transport=verification,
    ).execute(operation)
    assert result == HyperHDRAdapterResult(
        True,
        True,
        HyperHDRAdapterReason.VERIFIED,
    )
    assert mutation.calls == [
        {
            "host": "hyperhdr.invalid",
            "port": 8090,
            "timeout_seconds": 1.5,
            "payload": json.dumps(
                {
                    "command": "componentstate",
                    "componentstate": {"component": component, "state": state},
                },
                separators=(",", ":"),
            ).encode(),
        }
    ]
    assert len(verification.calls) == 1


@pytest.mark.parametrize(
    ("response", "reason"),
    (
        (b"{", HyperHDRAdapterReason.MALFORMED_JSON),
        (b"[]", HyperHDRAdapterReason.NON_OBJECT_JSON),
        (b"{}", HyperHDRAdapterReason.MISSING_SUCCESS),
        (b'{"success":1}', HyperHDRAdapterReason.SUCCESS_WRONG_TYPE),
        (b'{"success":false}', HyperHDRAdapterReason.SUCCESS_FALSE),
        (
            b'{"success":true,"command":"serverinfo"}',
            HyperHDRAdapterReason.MISMATCHED_COMMAND,
        ),
        (
            b"x" * (MAX_MUTATION_RESPONSE_BYTES + 1),
            HyperHDRAdapterReason.OVERSIZED_RESPONSE,
        ),
    ),
)
def test_bad_acknowledgements_fail_without_verification_or_retry(
    response: bytes,
    reason: HyperHDRAdapterReason,
) -> None:
    mutation = FakeMutationTransport(response)
    verification = FakeServerInfoTransport(_serverinfo("VIDEOGRABBER", True))
    result = HyperHDRMutationAdapter(
        host="hyperhdr.invalid",
        port=8090,
        timeout_seconds=1.0,
        mutation_transport=mutation,
        server_info_transport=verification,
    ).execute(HyperHDROperation.VIDEO_GRABBER_ENABLE)
    assert result == HyperHDRAdapterResult(False, False, reason)
    assert len(mutation.calls) == 1
    assert verification.calls == []


@pytest.mark.parametrize(
    "reason",
    (
        HyperHDRAdapterReason.CONNECTION_FAILURE,
        HyperHDRAdapterReason.TIMEOUT,
        HyperHDRAdapterReason.HTTP_REJECTION,
        HyperHDRAdapterReason.REDIRECT_REJECTION,
        HyperHDRAdapterReason.UNAUTHORIZED_RESPONSE,
        HyperHDRAdapterReason.OVERSIZED_RESPONSE,
    ),
)
def test_mutation_transport_failures_are_sanitized_and_not_retried(
    reason: HyperHDRAdapterReason,
) -> None:
    mutation = FakeMutationTransport(HyperHDRMutationTransportFailure(reason))
    verification = FakeServerInfoTransport(_serverinfo("VIDEOGRABBER", True))
    result = HyperHDRMutationAdapter(
        host="hyperhdr.invalid",
        port=8090,
        timeout_seconds=1.0,
        mutation_transport=mutation,
        server_info_transport=verification,
    ).execute(HyperHDROperation.VIDEO_GRABBER_ENABLE)
    assert result == HyperHDRAdapterResult(False, False, reason)
    assert len(mutation.calls) == 1 and verification.calls == []


@pytest.mark.parametrize(
    ("serverinfo", "reason"),
    (
        (
            b'{"success":true,"info":{"components":[]}}',
            HyperHDRAdapterReason.MISSING_COMPONENT_STATE,
        ),
        (
            b'{"success":true,"info":{"components":'
            b'[{"name":"VIDEOGRABBER","enabled":"yes"}]}}',
            HyperHDRAdapterReason.AMBIGUOUS_COMPONENT_STATE,
        ),
        (
            _serverinfo("VIDEOGRABBER", False),
            HyperHDRAdapterReason.STATE_VERIFICATION_MISMATCH,
        ),
        (b"{", HyperHDRAdapterReason.VERIFICATION_MALFORMED_RESPONSE),
    ),
)
def test_acknowledged_but_unverified_results_are_distinct(
    serverinfo: bytes,
    reason: HyperHDRAdapterReason,
) -> None:
    mutation = FakeMutationTransport(b'{"success":true}')
    verification = FakeServerInfoTransport(serverinfo)
    result = HyperHDRMutationAdapter(
        host="hyperhdr.invalid",
        port=8090,
        timeout_seconds=1.0,
        mutation_transport=mutation,
        server_info_transport=verification,
    ).execute(HyperHDROperation.VIDEO_GRABBER_ENABLE)
    assert result == HyperHDRAdapterResult(False, True, reason)
    assert len(mutation.calls) == len(verification.calls) == 1


def test_verification_timeout_is_unverified_without_retry_or_rollback() -> None:
    mutation = FakeMutationTransport(b'{"success":true}')
    verification = FakeServerInfoTransport(HyperHDRTimeoutError())
    result = HyperHDRMutationAdapter(
        host="hyperhdr.invalid",
        port=8090,
        timeout_seconds=1.0,
        mutation_transport=mutation,
        server_info_transport=verification,
    ).execute(HyperHDROperation.LED_OUTPUT_DISABLE)
    assert result == HyperHDRAdapterResult(
        False,
        True,
        HyperHDRAdapterReason.VERIFICATION_TIMEOUT,
    )
    assert len(mutation.calls) == len(verification.calls) == 1


def test_production_transport_uses_fixed_post_headers_limit_and_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def getcode(self) -> int:
            return 200

        def read(self, amount: int) -> bytes:
            captured["read_amount"] = amount
            return b'{"success":true}'

    class Opener:
        def open(self, request: object, timeout: float) -> Response:
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

    monkeypatch.setattr(
        "aurora_core.control_plane.hyperhdr_adapter.build_opener",
        lambda handler: Opener(),
    )
    payload = b'{"command":"componentstate"}'
    body = UrllibHyperHDRMutationTransport().post_component_state(
        host="hyperhdr.invalid",
        port=8091,
        timeout_seconds=1.25,
        payload=payload,
    )
    request = captured["request"]
    assert body == b'{"success":true}'
    assert request.get_method() == "POST"  # type: ignore[union-attr]
    assert request.full_url == "http://hyperhdr.invalid:8091/json-rpc"  # type: ignore[union-attr]
    assert request.data == payload  # type: ignore[union-attr]
    assert request.get_header("Content-type") == "application/json"  # type: ignore[union-attr]
    assert request.get_header("Accept") == "application/json"  # type: ignore[union-attr]
    assert request.get_header("User-agent") == "Project-Aurora"  # type: ignore[union-attr]
    assert captured["timeout"] == 1.25
    assert captured["read_amount"] == MAX_MUTATION_RESPONSE_BYTES + 1


@pytest.mark.parametrize(
    ("exception", "reason"),
    (
        (
            HTTPError("redacted", 302, "redacted", {}, None),
            HyperHDRAdapterReason.REDIRECT_REJECTION,
        ),
        (
            HTTPError("redacted", 401, "redacted", {}, None),
            HyperHDRAdapterReason.UNAUTHORIZED_RESPONSE,
        ),
        (
            HTTPError("redacted", 500, "redacted", {}, None),
            HyperHDRAdapterReason.HTTP_REJECTION,
        ),
        (TimeoutError(), HyperHDRAdapterReason.TIMEOUT),
        (URLError("redacted"), HyperHDRAdapterReason.CONNECTION_FAILURE),
    ),
)
def test_production_transport_rejects_failures(
    monkeypatch: pytest.MonkeyPatch,
    exception: Exception,
    reason: HyperHDRAdapterReason,
) -> None:
    class Opener:
        def open(self, request: object, timeout: float) -> None:
            raise exception

    monkeypatch.setattr(
        "aurora_core.control_plane.hyperhdr_adapter.build_opener",
        lambda handler: Opener(),
    )
    with pytest.raises(HyperHDRMutationTransportFailure) as error:
        UrllibHyperHDRMutationTransport().post_component_state(
            host="hyperhdr.invalid",
            port=8090,
            timeout_seconds=1.0,
            payload=b"{}",
        )
    assert error.value.reason is reason


def test_service_gates_auth_csrf_allowlist_and_both_confirmations() -> None:
    settings = _settings(
        operations=(
            "hyperhdr.video_grabber_disable",
            "hyperhdr.led_output_disable",
        )
    )
    adapter = FakeAdapter()
    service = HyperHDRControlService(
        settings.hyperhdr,
        authentication_enabled=True,
        adapter=adapter,
        limiter_digest_key=b"key",
    )
    session = SessionContext("operator", "A" * 43, 60)
    assert service.video_grabber_enable(session, "A" * 43, "client").reason.value == (
        "operation_not_allowlisted"
    )
    assert service.video_grabber_disable(None, None, None, "client").reason.value == (
        "authentication_required"
    )
    assert service.video_grabber_disable(
        session, None, None, "client"
    ).reason.value == ("missing_csrf")
    assert (
        service.video_grabber_disable(session, "B" * 43, None, "client").reason.value
        == "invalid_csrf"
    )
    assert (
        service.video_grabber_disable(session, "A" * 43, None, "client").reason.value
        == "missing_confirmation"
    )
    assert (
        service.led_output_disable(session, "A" * 43, "wrong", "client").reason.value
        == "invalid_confirmation"
    )
    assert adapter.calls == []


def test_enable_operations_need_no_confirmation_and_verified_only_invalidates() -> None:
    invalidations: list[str] = []
    adapter = FakeAdapter()
    service = HyperHDRControlService(
        _settings().hyperhdr,
        authentication_enabled=True,
        adapter=adapter,
        limiter_digest_key=b"key",
        cache_invalidator=lambda: invalidations.append("invalidate"),
    )
    session = SessionContext("operator", "A" * 43, 60)
    assert (
        service.video_grabber_enable(session, "A" * 43, "client-a").status
        is HyperHDRControlStatus.VERIFIED
    )
    adapter.result = HyperHDRAdapterResult(
        False,
        True,
        HyperHDRAdapterReason.STATE_VERIFICATION_MISMATCH,
    )
    assert (
        service.led_output_enable(session, "A" * 43, "client-b").status
        is HyperHDRControlStatus.UNVERIFIED
    )
    adapter.result = HyperHDRAdapterResult(
        False,
        False,
        HyperHDRAdapterReason.TIMEOUT,
    )
    assert (
        service.led_output_enable(session, "A" * 43, "client-c").status
        is HyperHDRControlStatus.FAILED
    )
    assert invalidations == ["invalidate"]


def test_disabled_denied_and_limited_outcomes_do_not_invalidate_cache() -> None:
    invalidations: list[str] = []
    disabled = HyperHDRControlService(
        _settings(controls_enabled=False).hyperhdr,
        authentication_enabled=True,
        adapter=FakeAdapter(),
        limiter_digest_key=b"disabled-key",
        cache_invalidator=lambda: invalidations.append("invalidate"),
    )
    session = SessionContext("operator", "A" * 43, 60)
    assert (
        disabled.video_grabber_enable(session, "A" * 43, "client").status
        is HyperHDRControlStatus.DENIED
    )

    limited = HyperHDRControlService(
        _settings(operation_limit=1).hyperhdr,
        authentication_enabled=True,
        adapter=FakeAdapter(
            HyperHDRAdapterResult(
                False,
                False,
                HyperHDRAdapterReason.TIMEOUT,
            )
        ),
        limiter_digest_key=b"limited-key",
        cache_invalidator=lambda: invalidations.append("invalidate"),
    )
    assert (
        limited.video_grabber_enable(session, "A" * 43, "client").status
        is HyperHDRControlStatus.FAILED
    )
    assert (
        limited.video_grabber_enable(session, "A" * 43, "client").status
        is HyperHDRControlStatus.RATE_LIMITED
    )
    assert invalidations == []


def test_service_limiter_lock_and_unexpected_exception_are_bounded() -> None:
    now = [0.0]
    settings = _settings(operation_limit=1, operation_window_seconds=2)
    adapter = FakeAdapter()
    service = HyperHDRControlService(
        settings.hyperhdr,
        authentication_enabled=True,
        adapter=adapter,
        clock=lambda: now[0],
        limiter_digest_key=b"key",
    )
    session = SessionContext("operator", "A" * 43, 60)
    assert (
        service.video_grabber_enable(session, "A" * 43, "client").status
        is HyperHDRControlStatus.VERIFIED
    )
    assert (
        service.video_grabber_enable(session, "A" * 43, "client").status
        is HyperHDRControlStatus.RATE_LIMITED
    )
    for number in range(300):
        service.led_output_enable(session, "A" * 43, f"client-{number}")
    assert service.tracked_client_count <= 256

    blocking = BlockingAdapter()
    serialized = HyperHDRControlService(
        settings.hyperhdr,
        authentication_enabled=True,
        adapter=blocking,
        limiter_digest_key=b"blocking-key",
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        active = pool.submit(
            serialized.video_grabber_enable,
            session,
            "A" * 43,
            "client-a",
        )
        assert blocking.entered.wait(timeout=1)
        busy = serialized.led_output_enable(session, "A" * 43, "client-b")
        assert busy.status is HyperHDRControlStatus.BUSY
        blocking.release.set()
        assert active.result(timeout=1).status is HyperHDRControlStatus.VERIFIED
    assert len(blocking.calls) == 1

    raising = RaisingAdapter()
    failed = HyperHDRControlService(
        settings.hyperhdr,
        authentication_enabled=True,
        adapter=raising,
        limiter_digest_key=b"raising-key",
    ).video_grabber_enable(session, "A" * 43, "client")
    assert failed.status is HyperHDRControlStatus.FAILED
    assert "private-hyperhdr-response" not in repr(failed)


def test_service_availability_capabilities_and_sanitized_audit() -> None:
    events: list[tuple[str, dict[str, str | int]]] = []
    audit = SecurityAudit(lambda event, fields: events.append((event, dict(fields))))
    adapter = FakeAdapter(
        HyperHDRAdapterResult(False, False, HyperHDRAdapterReason.TIMEOUT)
    )
    service = HyperHDRControlService(
        _settings(
            operations=(
                "hyperhdr.led_output_disable",
                "hyperhdr.video_grabber_enable",
            )
        ).hyperhdr,
        authentication_enabled=True,
        adapter=adapter,
        limiter_digest_key=b"key",
        audit=audit,
    )
    assert service.availability is HyperHDRControlAvailability.AVAILABLE
    assert service.capabilities().available_operations == (
        "hyperhdr.video_grabber_enable",
        "hyperhdr.led_output_disable",
    )
    session = SessionContext("private-user", "S" * 43, 60)
    service.video_grabber_enable(session, "S" * 43, "private-client")
    serialized = json.dumps(events)
    assert "hyperhdr_operation_failed" in serialized
    assert "hyperhdr.video_grabber_enable" in serialized
    for forbidden in (
        "private-user",
        "private-client",
        "S" * 43,
        "hyperhdr.invalid",
        "VIDEOGRABBER",
    ):
        assert forbidden not in serialized

    authentication_disabled = HyperHDRControlService(
        _settings().hyperhdr,
        authentication_enabled=False,
        adapter=FakeAdapter(),
        limiter_digest_key=b"disabled-key",
    )
    assert authentication_disabled.available_operations == ()
    assert not authentication_disabled.capabilities().mutations_enabled
    assert authentication_disabled.availability is (
        HyperHDRControlAvailability.AUTHENTICATION_UNAVAILABLE
    )


def test_authenticated_page_uses_shared_snapshot_and_fixed_forms() -> None:
    health, control, hyperhdr, wled, cookie, session, adapter = _http_services()
    body, content_type, status, _ = _request(
        health,
        control,
        hyperhdr,
        wled,
        "/controls/hyperhdr",
        headers=_headers(cookie=cookie),
    )
    assert status is HTTPStatus.OK and content_type.startswith("text/html")
    assert health.calls == 1 and adapter.calls == []
    for path in (
        b"/controls/hyperhdr/video-grabber/enable",
        b"/controls/hyperhdr/video-grabber/disable",
        b"/controls/hyperhdr/led-output/enable",
        b"/controls/hyperhdr/led-output/disable",
    ):
        assert path in body
    assert b"interrupts HyperHDR capture" in body
    assert b"interrupts HyperHDR LED transmission" in body
    assert body.count(session.csrf_token.encode()) == 4
    assert b"hyperhdr.invalid" not in body and b"json-rpc" not in body


def test_unified_controls_page_uses_one_cached_report_and_no_hyperhdr_request() -> None:
    health, control, hyperhdr, wled, cookie, _, adapter = _http_services()
    body, content_type, status, _ = _request(
        health,
        control,
        hyperhdr,
        wled,
        "/controls",
        headers=_headers(cookie=cookie),
    )
    assert status is HTTPStatus.OK and content_type.startswith("text/html")
    assert b"Lighting Controls" in body
    for path in (
        b"/controls/hyperhdr/video-grabber/enable",
        b"/controls/hyperhdr/video-grabber/disable",
        b"/controls/hyperhdr/led-output/enable",
        b"/controls/hyperhdr/led-output/disable",
    ):
        assert path in body
    assert health.calls == 1 and adapter.calls == []


@pytest.mark.parametrize(
    ("path", "fields", "operation"),
    (
        (
            "/controls/hyperhdr/video-grabber/enable",
            {},
            HyperHDROperation.VIDEO_GRABBER_ENABLE,
        ),
        (
            "/controls/hyperhdr/video-grabber/disable",
            {"confirmation": VIDEO_GRABBER_DISABLE_CONFIRMATION_VALUE},
            HyperHDROperation.VIDEO_GRABBER_DISABLE,
        ),
        (
            "/controls/hyperhdr/led-output/enable",
            {},
            HyperHDROperation.LED_OUTPUT_ENABLE,
        ),
        (
            "/controls/hyperhdr/led-output/disable",
            {"confirmation": LED_OUTPUT_DISABLE_CONFIRMATION_VALUE},
            HyperHDROperation.LED_OUTPUT_DISABLE,
        ),
    ),
)
def test_routes_execute_fixed_operation_once_and_redirect(
    path: str,
    fields: dict[str, str],
    operation: HyperHDROperation,
) -> None:
    health, control, hyperhdr, wled, cookie, session, adapter = _http_services()
    body = urlencode({"csrf_token": session.csrf_token, **fields}).encode()
    response, _, status, headers = _request(
        health,
        control,
        hyperhdr,
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
    assert headers["Location"] == "/controls/hyperhdr?notice=verified"
    assert adapter.calls == [operation]
    assert health.invalidations == 1


@pytest.mark.parametrize(
    ("body", "headers", "status"),
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
            b"csrf_token=x&operation=private",
            {"content_type": "application/x-www-form-urlencoded"},
            HTTPStatus.BAD_REQUEST,
        ),
    ),
)
def test_route_form_boundary_is_strict(
    body: bytes,
    headers: dict[str, str],
    status: HTTPStatus,
) -> None:
    health, control, hyperhdr, wled, cookie, _, adapter = _http_services()
    _, _, actual, _ = _request(
        health,
        control,
        hyperhdr,
        wled,
        "/controls/hyperhdr/video-grabber/enable",
        method="POST",
        body=body,
        headers=_headers(body=body, cookie=cookie, **headers),
    )
    assert actual is status and adapter.calls == []


def test_routes_reject_missing_and_repeated_content_length() -> None:
    health, control, hyperhdr, wled, cookie, _, adapter = _http_services()
    body = b"csrf_token=x"
    missing = Message()
    missing["Content-Type"] = "application/x-www-form-urlencoded"
    missing["Cookie"] = cookie
    _, _, status, _ = _request(
        health,
        control,
        hyperhdr,
        wled,
        "/controls/hyperhdr/led-output/enable",
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
        hyperhdr,
        wled,
        "/controls/hyperhdr/led-output/enable",
        method="POST",
        body=body,
        headers=repeated,
    )
    assert status is HTTPStatus.BAD_REQUEST
    assert adapter.calls == []


def test_routes_require_login_csrf_confirmations_and_allowlist() -> None:
    health, control, hyperhdr, wled, _, session, adapter = _http_services(
        operations=("hyperhdr.video_grabber_disable",)
    )
    body = urlencode({"csrf_token": session.csrf_token}).encode()
    _, _, status, headers = _request(
        health,
        control,
        hyperhdr,
        wled,
        "/controls/hyperhdr/video-grabber/enable",
        method="POST",
        body=body,
        headers=_headers(
            body=body,
            content_type="application/x-www-form-urlencoded",
        ),
    )
    assert status is HTTPStatus.SEE_OTHER and headers["Location"].startswith("/login")

    login = control.authenticate("test_operator", _password(), "new-client")
    assert login.created_session is not None
    token = login.created_session.token
    active = control.resolve_session(token)
    assert active is not None
    cookie = f"{SESSION_COOKIE_NAME}={token}"
    for path, fields in (
        ("/controls/hyperhdr/video-grabber/enable", {}),
        ("/controls/hyperhdr/video-grabber/disable", {}),
        (
            "/controls/hyperhdr/video-grabber/disable",
            {"confirmation": "wrong"},
        ),
    ):
        request_body = urlencode({"csrf_token": active.csrf_token, **fields}).encode()
        _, _, result_status, result_headers = _request(
            health,
            control,
            hyperhdr,
            wled,
            path,
            method="POST",
            body=request_body,
            headers=_headers(
                body=request_body,
                cookie=cookie,
                content_type="application/x-www-form-urlencoded",
            ),
        )
        assert result_status is HTTPStatus.SEE_OTHER
        assert result_headers["Location"] == "/controls/hyperhdr?notice=denied"
    assert adapter.calls == []


def test_capability_union_is_deterministic_and_health_api_stays_v1() -> None:
    health, control, hyperhdr, wled, cookie, _, adapter = _http_services(
        operations=(
            "hyperhdr.led_output_disable",
            "hyperhdr.video_grabber_enable",
        ),
        wled_operations=("wled.brightness_set", "wled.power_on"),
    )
    body, _, status, _ = _request(
        health,
        control,
        hyperhdr,
        wled,
        "/api/control/status",
        headers=_headers(cookie=cookie),
    )
    assert status is HTTPStatus.OK
    assert json.loads(body)["available_operations"] == [
        "wled.power_on",
        "wled.brightness_set",
        "hyperhdr.video_grabber_enable",
        "hyperhdr.led_output_disable",
    ]
    public, _, public_status, _ = _request(
        health,
        control,
        hyperhdr,
        wled,
        "/api/health",
    )
    assert public_status is HTTPStatus.OK
    assert json.loads(public)["schema_version"] == 1
    assert adapter.calls == []


def test_hyperhdr_only_capabilities_expose_only_hyperhdr_operations() -> None:
    health, control, hyperhdr, wled, cookie, _, adapter = _http_services(
        operations=("hyperhdr.led_output_enable",),
    )
    body, _, status, _ = _request(
        health,
        control,
        hyperhdr,
        wled,
        "/api/control/status",
        headers=_headers(cookie=cookie),
    )
    assert status is HTTPStatus.OK
    assert json.loads(body)["available_operations"] == ["hyperhdr.led_output_enable"]
    assert health.calls == 0 and adapter.calls == []


def test_no_generic_hyperhdr_execute_route_or_wrong_method_exists() -> None:
    health, control, hyperhdr, wled, cookie, _, adapter = _http_services()
    for path in (
        "/controls/hyperhdr/execute",
        "/api/control/hyperhdr",
        "/api/hyperhdr",
    ):
        _, _, status, headers = _request(
            health,
            control,
            hyperhdr,
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
        hyperhdr,
        wled,
        "/controls/hyperhdr/led-output/enable",
        method="PUT",
        headers=_headers(cookie=cookie),
    )
    assert status is HTTPStatus.METHOD_NOT_ALLOWED
    assert headers["Allow"] == "POST"
    _, _, status, headers = _request(
        health,
        control,
        hyperhdr,
        wled,
        "/controls/hyperhdr/led-output/enable",
        headers=_headers(cookie=cookie),
    )
    assert status is HTTPStatus.METHOD_NOT_ALLOWED
    assert headers["Allow"] == "POST"
    assert adapter.calls == []
