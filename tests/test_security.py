"""Unit tests for bounded Milestone 14 security primitives."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from aurora_core.__main__ import build_parser, main
from aurora_core.config import AuroraConfigurationError, load_settings
from aurora_core.control_plane.audit import SecurityAudit
from aurora_core.control_plane.contracts import (
    CONTROL_CAPABILITIES,
    REGISTERED_OPERATIONS,
    FutureOperationContract,
)
from aurora_core.control_plane.cookies import (
    MAX_COOKIE_HEADER_BYTES,
    SESSION_COOKIE_NAME,
    cleared_session_cookie,
    read_session_cookie,
    session_cookie,
)
from aurora_core.control_plane.limiter import LoginAttemptLimiter
from aurora_core.control_plane.service import ControlPlaneService, LoginStatus
from aurora_core.control_plane.sessions import (
    SessionContext,
    SessionLookupStatus,
    SessionStore,
    csrf_is_valid,
)
from aurora_core.security.passwords import (
    HASH_SCHEME,
    HASH_VERSION,
    MAX_ITERATIONS,
    MAX_PASSWORD_CHARACTERS,
    PasswordHashError,
    hash_password,
    validate_password_hash,
    verify_password,
)


def _password() -> str:
    return "".join(("unit", "-", "credential", "-", "canary"))


def _hash() -> str:
    return hash_password(_password(), salt=bytes(range(16)))


def _auth_settings(**overrides: object):
    authentication: dict[str, object] = {
        "enabled": True,
        "username": "test_operator",
        "password_hash": _hash(),
        "session_ttl_minutes": 5,
        "maximum_sessions": 4,
        "login_attempt_limit": 3,
        "login_attempt_window_seconds": 30,
    }
    authentication.update(overrides)
    return load_settings(
        environment={},
        cli_overrides={"dashboard": {"authentication": authentication}},
    ).dashboard.authentication


class SequenceTokenFactory:
    def __init__(self) -> None:
        self._value = 0
        self._lock = threading.Lock()

    def __call__(self, byte_count: int) -> str:
        with self._lock:
            self._value += 1
            return f"{self._value:043d}"


def test_password_hash_round_trip_is_versioned_and_salted() -> None:
    first = hash_password(_password())
    second = hash_password(_password())
    assert first.startswith(f"{HASH_SCHEME}${HASH_VERSION}$i=")
    assert first != second
    assert verify_password(_password(), first)
    assert not verify_password(_password() + "x", first)
    assert _password() not in first


def test_password_hash_rejects_empty_long_and_bad_parameters() -> None:
    with pytest.raises(PasswordHashError):
        hash_password("")
    with pytest.raises(PasswordHashError):
        hash_password("x" * (MAX_PASSWORD_CHARACTERS + 1))
    with pytest.raises(PasswordHashError):
        hash_password(_password(), iterations=MAX_ITERATIONS + 1)
    with pytest.raises(PasswordHashError):
        hash_password(_password(), salt=b"short")


@pytest.mark.parametrize(
    "encoded_hash",
    (
        "",
        "unsupported$v1$i=600000$abc$def",
        "aurora-pbkdf2-sha256$v2$i=600000$abc$def",
        "aurora-pbkdf2-sha256$v1$i=word$abc$def",
        "aurora-pbkdf2-sha256$v1$i=1$abc$def",
        "aurora-pbkdf2-sha256$v1$i=999999999$abc$def",
        "aurora-pbkdf2-sha256$v1$i=600000$%%%$def",
    ),
)
def test_malformed_and_unsupported_hashes_fail_safely(encoded_hash: str) -> None:
    with pytest.raises(PasswordHashError):
        validate_password_hash(encoded_hash)
    assert not verify_password(_password(), encoded_hash)


def test_excessive_hash_parameters_are_rejected_before_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "aurora_core.security.passwords.hashlib.pbkdf2_hmac",
        lambda *args, **kwargs: pytest.fail("derivation must not run"),
    )
    encoded = f"{HASH_SCHEME}${HASH_VERSION}$i={MAX_ITERATIONS + 1}$abc$def"
    assert not verify_password(_password(), encoded)


def test_password_verification_uses_constant_time_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comparisons: list[tuple[bytes, bytes]] = []

    def compare(actual: bytes, expected: bytes) -> bool:
        comparisons.append((actual, expected))
        return actual == expected

    monkeypatch.setattr("aurora_core.security.passwords.hmac.compare_digest", compare)
    assert verify_password(_password(), _hash())
    assert len(comparisons) == 1
    assert len(comparisons[0][0]) == len(comparisons[0][1]) == 32


def test_hash_password_cli_prompts_twice_and_prints_only_hash(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    responses = iter((_password(), _password()))
    monkeypatch.setattr(
        "aurora_core.__main__.getpass.getpass",
        lambda prompt: next(responses),
    )
    monkeypatch.setattr("sys.argv", ["aurora", "security", "hash-password"])
    assert main() == 0
    output = capsys.readouterr()
    encoded = output.out.strip()
    assert output.err == ""
    assert verify_password(_password(), encoded)
    assert _password() not in output.out


@pytest.mark.parametrize(
    "responses",
    (("one", "two"), ("", ""), ("x" * (MAX_PASSWORD_CHARACTERS + 1),) * 2),
)
def test_hash_password_cli_rejects_invalid_input_without_leaking_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    responses: tuple[str, str],
) -> None:
    supplied = iter(responses)
    monkeypatch.setattr(
        "aurora_core.__main__.getpass.getpass",
        lambda prompt: next(supplied),
    )
    monkeypatch.setattr("sys.argv", ["aurora", "security", "hash-password"])
    assert main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    if responses[0]:
        assert responses[0] not in output.err


def test_hash_password_cli_has_no_plaintext_argument() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["security", "hash-password", "--password", "forbidden"])


def test_authentication_configuration_defaults_disabled_and_fail_closed() -> None:
    authentication = load_settings(environment={}).dashboard.authentication
    assert not authentication.enabled
    assert authentication.username is None
    assert authentication.password_hash is None
    assert ControlPlaneService(authentication).authentication_enabled is False


def test_enabled_authentication_configuration_and_environment_are_validated() -> None:
    encoded = _hash()
    settings = load_settings(
        environment={
            "AURORA_DASHBOARD__AUTHENTICATION__ENABLED": "true",
            "AURORA_DASHBOARD__AUTHENTICATION__USERNAME": "test_operator",
            "AURORA_DASHBOARD__AUTHENTICATION__PASSWORD_HASH": encoded,
            "AURORA_DASHBOARD__AUTHENTICATION__SESSION_TTL_MINUTES": "60",
            "AURORA_DASHBOARD__AUTHENTICATION__MAXIMUM_SESSIONS": "8",
            "AURORA_DASHBOARD__AUTHENTICATION__SECURE_COOKIE": "true",
            "AURORA_DASHBOARD__AUTHENTICATION__LOGIN_ATTEMPT_LIMIT": "4",
            "AURORA_DASHBOARD__AUTHENTICATION__LOGIN_ATTEMPT_WINDOW_SECONDS": "120",
        }
    )
    authentication = settings.dashboard.authentication
    assert authentication.enabled
    assert authentication.username == "test_operator"
    assert authentication.password_hash is not None
    assert authentication.password_hash.get_secret_value() == encoded
    assert authentication.session_ttl_minutes == 60
    assert authentication.maximum_sessions == 8
    assert authentication.secure_cookie


@pytest.mark.parametrize(
    "authentication",
    (
        {"enabled": True},
        {"enabled": True, "username": "test_operator"},
        {"enabled": True, "password_hash": "unsupported"},
        {"username": "not allowed"},
        {"session_ttl_minutes": 1},
        {"session_ttl_minutes": 1441},
        {"maximum_sessions": 0},
        {"maximum_sessions": 65},
        {"login_attempt_limit": 0},
        {"login_attempt_limit": 21},
        {"login_attempt_window_seconds": 29},
        {"login_attempt_window_seconds": 3601},
    ),
)
def test_invalid_authentication_configuration_is_rejected(
    authentication: dict[str, object],
) -> None:
    with pytest.raises(AuroraConfigurationError):
        load_settings(
            environment={},
            cli_overrides={"dashboard": {"authentication": authentication}},
        )


def test_password_hash_is_redacted_from_settings_and_validation_errors() -> None:
    encoded = _hash()
    settings = load_settings(
        environment={},
        cli_overrides={"dashboard": {"authentication": {"password_hash": encoded}}},
    )
    assert encoded not in repr(settings)
    with pytest.raises(AuroraConfigurationError) as error:
        load_settings(
            environment={},
            cli_overrides={
                "dashboard": {
                    "authentication": {
                        "password_hash": encoded,
                        "maximum_sessions": 0,
                    }
                }
            },
        )
    assert encoded not in str(error.value)
    malformed = "unsupported-private-hash-canary"
    with pytest.raises(AuroraConfigurationError) as malformed_error:
        load_settings(
            environment={},
            cli_overrides={
                "dashboard": {"authentication": {"password_hash": malformed}}
            },
        )
    assert malformed not in str(malformed_error.value)


def test_session_store_creation_expiration_and_unknown_tokens() -> None:
    now = [0.0]
    factory = SequenceTokenFactory()
    store = SessionStore(
        ttl_seconds=10,
        maximum_sessions=2,
        clock=lambda: now[0],
        token_factory=factory,
    )
    created = store.create("test_operator")
    assert created.token not in repr(store)
    assert store.resolve(created.token).status is SessionLookupStatus.VALID
    assert store.resolve("Z" * 43).status is SessionLookupStatus.INVALID
    assert store.resolve("malformed").status is SessionLookupStatus.INVALID
    now[0] = 10.0
    assert store.resolve(created.token).status is SessionLookupStatus.EXPIRED
    assert store.active_count == 0


def test_session_cookie_parsing_and_serialization_are_bounded() -> None:
    token = "A" * 43
    assert read_session_cookie(None).token is None
    assert read_session_cookie(f"{SESSION_COOKIE_NAME}={token}").token == token
    assert read_session_cookie(f"{SESSION_COOKIE_NAME}=").token is None
    for malformed in (
        "x" * (MAX_COOKIE_HEADER_BYTES + 1),
        f"{SESSION_COOKIE_NAME}={token}\x01",
        f"{SESSION_COOKIE_NAME}={token}; {SESSION_COOKIE_NAME}={token}",
    ):
        assert read_session_cookie(malformed).malformed
    with pytest.raises(ValueError):
        session_cookie("short", max_age_seconds=60, secure=False)
    with pytest.raises(ValueError):
        session_cookie(token, max_age_seconds=0, secure=False)
    assert "Secure" in cleared_session_cookie(secure=True)


def test_session_store_enforces_maximum_and_logout_invalidation() -> None:
    store = SessionStore(
        ttl_seconds=60,
        maximum_sessions=2,
        token_factory=SequenceTokenFactory(),
    )
    first = store.create("test_operator")
    second = store.create("test_operator")
    third = store.create("test_operator")
    assert store.active_count == 2
    assert store.resolve(first.token).status is SessionLookupStatus.INVALID
    assert store.resolve(second.token).status is SessionLookupStatus.VALID
    assert store.invalidate(third.token)
    assert store.resolve(third.token).status is SessionLookupStatus.INVALID


def test_service_rotates_session_after_successful_authentication() -> None:
    service = ControlPlaneService(
        _auth_settings(),
        token_factory=SequenceTokenFactory(),
        limiter_digest_key=b"test-key",
    )
    first = service.authenticate("test_operator", _password(), "client")
    assert first.status is LoginStatus.SUCCESS
    assert first.created_session is not None
    second = service.authenticate(
        "test_operator",
        _password(),
        "client",
        prior_session_token=first.created_session.token,
    )
    assert second.status is LoginStatus.SUCCESS
    assert second.created_session is not None
    assert second.created_session.token != first.created_session.token
    assert service.resolve_session(first.created_session.token) is None
    assert service.resolve_session(second.created_session.token) is not None


def test_session_expiration_and_process_restart_fail_closed_with_audit() -> None:
    now = [0.0]
    events: list[tuple[str, object]] = []
    audit = SecurityAudit(lambda event, fields: events.append((event, dict(fields))))
    service = ControlPlaneService(
        _auth_settings(session_ttl_minutes=5),
        clock=lambda: now[0],
        token_factory=SequenceTokenFactory(),
        limiter_digest_key=b"test-key",
        audit=audit,
    )
    result = service.authenticate("test_operator", _password(), "client")
    assert result.created_session is not None
    token = result.created_session.token
    now[0] = 300.0
    assert service.resolve_session(token) is None
    assert "expired_session" in json.dumps(events)

    restarted = ControlPlaneService(
        _auth_settings(),
        token_factory=SequenceTokenFactory(),
        limiter_digest_key=b"new-process-key",
    )
    assert restarted.resolve_session(token) is None


def test_username_comparison_uses_constant_time_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comparisons: list[tuple[bytes, bytes]] = []

    def compare(actual: bytes, expected: bytes) -> bool:
        comparisons.append((actual, expected))
        return False

    monkeypatch.setattr(
        "aurora_core.control_plane.service.hmac.compare_digest", compare
    )
    service = ControlPlaneService(
        _auth_settings(),
        token_factory=SequenceTokenFactory(),
        limiter_digest_key=b"test-key",
    )
    assert (
        service.authenticate("unknown_operator", _password(), "client").status
        is LoginStatus.FAILURE
    )
    assert comparisons[0] == (b"unknown_operator", b"test_operator")


def test_csrf_validation_uses_constant_time_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SessionContext("test_operator", "A" * 43, 10.0)
    calls: list[tuple[str, str]] = []

    def compare(actual: str, expected: str) -> bool:
        calls.append((actual, expected))
        return actual == expected

    monkeypatch.setattr(
        "aurora_core.control_plane.sessions.hmac.compare_digest", compare
    )
    assert csrf_is_valid(session, "A" * 43)
    assert calls == [("A" * 43, "A" * 43)]
    assert not csrf_is_valid(session, "short")
    assert len(calls) == 1


def test_login_attempt_limiter_is_bounded_monotonic_and_resets() -> None:
    now = [0.0]
    limiter = LoginAttemptLimiter(
        attempt_limit=2,
        window_seconds=10,
        clock=lambda: now[0],
        digest_key=b"test-key",
        maximum_clients=2,
    )
    assert limiter.begin_attempt("client-a")
    assert limiter.begin_attempt("client-a")
    assert not limiter.begin_attempt("client-a")
    limiter.clear("client-a")
    assert limiter.begin_attempt("client-a")
    assert limiter.begin_attempt("client-b")
    assert limiter.begin_attempt("client-c")
    assert limiter.tracked_client_count == 2
    now[0] = 11.0
    assert limiter.tracked_client_count == 0


def test_login_attempt_limiter_has_a_global_window_cap() -> None:
    limiter = LoginAttemptLimiter(
        attempt_limit=2,
        window_seconds=60,
        digest_key=b"test-key",
    )
    decisions = tuple(
        limiter.begin_attempt(f"client-{number}") for number in range(100)
    )
    assert sum(decisions) == 32


def test_session_and_attempt_stores_are_thread_safe_and_bounded() -> None:
    session_store = SessionStore(ttl_seconds=60, maximum_sessions=16)
    limiter = LoginAttemptLimiter(
        attempt_limit=5,
        window_seconds=60,
        digest_key=b"test-key",
    )
    with ThreadPoolExecutor(max_workers=16) as pool:
        sessions = tuple(pool.map(session_store.create, ("test_operator",) * 100))
        attempts = tuple(pool.map(limiter.begin_attempt, ("one-client",) * 100))
    assert session_store.active_count == 16
    assert sum(attempts) == 5
    assert limiter.attempt_count("one-client") == 5
    assert all(created.token for created in sessions)


def test_security_audit_events_do_not_capture_credentials_or_identifiers() -> None:
    events: list[tuple[str, object]] = []
    audit = SecurityAudit(lambda event, fields: events.append((event, dict(fields))))
    service = ControlPlaneService(
        _auth_settings(login_attempt_limit=1),
        token_factory=SequenceTokenFactory(),
        limiter_digest_key=b"test-key",
        audit=audit,
    )
    assert (
        service.authenticate(
            "private-user-canary", "private-pass-canary", "private-client"
        ).status
        is LoginStatus.FAILURE
    )
    assert (
        service.authenticate(
            "private-user-canary", "private-pass-canary", "private-client"
        ).status
        is LoginStatus.RATE_LIMITED
    )
    service.resolve_session("Z" * 43)
    serialized = json.dumps(events)
    for forbidden in (
        "private-user-canary",
        "private-pass-canary",
        "private-client",
        "Z" * 43,
        _hash(),
    ):
        assert forbidden not in serialized
    assert "login_failed" in serialized
    assert "login_rate_limited" in serialized
    assert "session_rejected" in serialized


def test_future_operation_contracts_have_no_registered_executor_or_capability() -> None:
    assert REGISTERED_OPERATIONS == ()
    assert CONTROL_CAPABILITIES.to_dict() == {
        "schema_version": 1,
        "authenticated": True,
        "mutations_enabled": False,
        "available_operations": [],
    }
    with pytest.raises(ValueError):
        FutureOperationContract("", "Input", 1.0, "adapter", False)
    with pytest.raises(ValueError):
        FutureOperationContract("future.test", "Input", 20.0, "adapter", False)
    with pytest.raises(ValueError):
        FutureOperationContract(
            "future.test",
            "Input",
            1.0,
            "adapter",
            False,
            authentication_required=False,
        )
    with pytest.raises(ValueError):
        FutureOperationContract("future.test", "Input", 1.0, "adapter", True)
    contract = FutureOperationContract(
        "future.test",
        "Input",
        1.0,
        "future.adapter",
        True,
        confirmation_metadata_id="future.confirmation",
    )
    assert contract.authentication_required
    assert contract.csrf_required
    assert contract.audit_required
    assert contract.sanitized_errors_required
