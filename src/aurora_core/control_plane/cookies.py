"""Opaque Project Aurora session-cookie parsing and serialization."""

from __future__ import annotations

from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie

from aurora_core.control_plane.sessions import opaque_token_is_valid

SESSION_COOKIE_NAME = "aurora_control_session"
MAX_COOKIE_HEADER_BYTES = 4096


@dataclass(frozen=True, slots=True)
class CookieLookup:
    token: str | None
    malformed: bool = False


def read_session_cookie(header_value: str | None) -> CookieLookup:
    """Return one opaque token or a generic malformed result."""
    if header_value is None:
        return CookieLookup(None)
    if len(header_value.encode("utf-8", errors="replace")) > MAX_COOKIE_HEADER_BYTES:
        return CookieLookup(None, malformed=True)
    if any(ord(character) < 32 or ord(character) == 127 for character in header_value):
        return CookieLookup(None, malformed=True)
    session_parts = tuple(
        part
        for part in header_value.split(";")
        if part.strip().startswith(f"{SESSION_COOKIE_NAME}=")
    )
    if len(session_parts) > 1:
        return CookieLookup(None, malformed=True)
    cookie = SimpleCookie()
    try:
        cookie.load(header_value)
    except CookieError:
        return CookieLookup(None, malformed=True)
    morsel = cookie.get(SESSION_COOKIE_NAME)
    if morsel is None:
        return CookieLookup(
            None,
            malformed=SESSION_COOKIE_NAME in header_value,
        )
    if not morsel.value:
        return CookieLookup(None)
    if not opaque_token_is_valid(morsel.value):
        return CookieLookup(None, malformed=True)
    return CookieLookup(morsel.value)


def session_cookie(
    token: str,
    *,
    max_age_seconds: int,
    secure: bool,
) -> str:
    if not opaque_token_is_valid(token) or max_age_seconds <= 0:
        raise ValueError("session-cookie values must satisfy the bounded policy")
    parts = [
        f"{SESSION_COOKIE_NAME}={token}",
        "HttpOnly",
        "SameSite=Strict",
        "Path=/",
        f"Max-Age={max_age_seconds}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def cleared_session_cookie(*, secure: bool) -> str:
    parts = [
        f"{SESSION_COOKIE_NAME}=",
        "HttpOnly",
        "SameSite=Strict",
        "Path=/",
        "Max-Age=0",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)
