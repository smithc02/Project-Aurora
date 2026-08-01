"""Versioned, bounded password hashing using only the standard library."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets

HASH_SCHEME = "aurora-pbkdf2-sha256"
HASH_VERSION = "v1"
DEFAULT_ITERATIONS = 600_000
MIN_ITERATIONS = 200_000
MAX_ITERATIONS = 1_000_000
SALT_BYTES = 16
DERIVED_KEY_BYTES = 32
MAX_PASSWORD_CHARACTERS = 1024
MAX_ENCODED_HASH_CHARACTERS = 256
_ENCODED_PART = re.compile(r"[A-Za-z0-9_-]+")


class PasswordHashError(ValueError):
    """A password or encoded hash did not satisfy the bounded format."""


def password_is_acceptable(password: str) -> bool:
    """Return whether plaintext is non-empty and within the command boundary."""
    return 0 < len(password) <= MAX_PASSWORD_CHARACTERS


def hash_password(
    password: str,
    *,
    salt: bytes | None = None,
    iterations: int = DEFAULT_ITERATIONS,
) -> str:
    """Return one copyable, versioned PBKDF2-HMAC-SHA256 password hash."""
    if not password_is_acceptable(password):
        raise PasswordHashError("password length is outside the supported range")
    if not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
        raise PasswordHashError("password hash work factor is outside safe bounds")
    active_salt = secrets.token_bytes(SALT_BYTES) if salt is None else salt
    if len(active_salt) != SALT_BYTES:
        raise PasswordHashError("password hash salt length is unsupported")
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        active_salt,
        iterations,
        dklen=DERIVED_KEY_BYTES,
    )
    return "$".join(
        (
            HASH_SCHEME,
            HASH_VERSION,
            f"i={iterations}",
            _encode(active_salt),
            _encode(derived),
        )
    )


def validate_password_hash(encoded_hash: str) -> None:
    """Reject malformed, unsupported, or excessively expensive hashes."""
    _parse_password_hash(encoded_hash)


def verify_password(password: str, encoded_hash: str) -> bool:
    """Verify a bounded password hash using constant-time digest comparison."""
    if not password_is_acceptable(password):
        return False
    try:
        iterations, salt, expected = _parse_password_hash(encoded_hash)
    except PasswordHashError:
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
        dklen=DERIVED_KEY_BYTES,
    )
    return hmac.compare_digest(actual, expected)


def _parse_password_hash(encoded_hash: str) -> tuple[int, bytes, bytes]:
    if not 1 <= len(encoded_hash) <= MAX_ENCODED_HASH_CHARACTERS:
        raise PasswordHashError("password hash length is unsupported")
    parts = encoded_hash.split("$")
    if len(parts) != 5 or parts[0] != HASH_SCHEME or parts[1] != HASH_VERSION:
        raise PasswordHashError("password hash scheme or version is unsupported")
    parameter = parts[2]
    if not parameter.startswith("i=") or not parameter[2:].isdigit():
        raise PasswordHashError("password hash parameters are malformed")
    iterations = int(parameter[2:])
    if not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
        raise PasswordHashError("password hash work factor is outside safe bounds")
    salt = _decode(parts[3])
    expected = _decode(parts[4])
    if len(salt) != SALT_BYTES or len(expected) != DERIVED_KEY_BYTES:
        raise PasswordHashError("password hash material has an unsupported length")
    return iterations, salt, expected


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    if not value or _ENCODED_PART.fullmatch(value) is None:
        raise PasswordHashError("password hash encoding is malformed")
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as error:
        raise PasswordHashError("password hash encoding is malformed") from error
