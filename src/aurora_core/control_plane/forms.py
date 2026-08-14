"""Strict parsing helpers for the small protected form boundary."""

from __future__ import annotations

import re
from dataclasses import dataclass
from http import HTTPStatus
from urllib.parse import parse_qsl

from aurora_core.control_plane.audit import AuditReason

LOGIN_BODY_LIMIT = 4096
LOGOUT_BODY_LIMIT = 1024
WLED_CONTROL_BODY_LIMIT = 1024
HYPERHDR_CONTROL_BODY_LIMIT = 1024
AMBIENT_CONTROL_BODY_LIMIT = 1024
MAX_FORM_FIELDS = 4
ALLOWED_NEXT_PATHS = frozenset({"/controls", "/controls/wled", "/controls/hyperhdr"})
_PERCENT_ESCAPE = re.compile(r"[0-9A-Fa-f]{2}")


@dataclass(frozen=True, slots=True)
class FormError:
    status: HTTPStatus
    reason: AuditReason


def validate_form_headers(
    *,
    content_types: list[str] | None,
    content_lengths: list[str] | None,
    transfer_encoding: str | None,
    maximum_body_bytes: int,
) -> tuple[int | None, FormError | None]:
    if transfer_encoding is not None:
        return None, FormError(
            HTTPStatus.BAD_REQUEST,
            AuditReason.TRANSFER_ENCODING,
        )
    if (
        content_types is None
        or len(content_types) != 1
        or not _supported_content_type(content_types[0])
    ):
        return None, FormError(
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            AuditReason.CONTENT_TYPE,
        )
    if content_lengths is None:
        return None, FormError(
            HTTPStatus.LENGTH_REQUIRED,
            AuditReason.CONTENT_LENGTH,
        )
    if len(content_lengths) != 1:
        return None, FormError(
            HTTPStatus.BAD_REQUEST,
            AuditReason.CONTENT_LENGTH,
        )
    raw_length = content_lengths[0]
    if len(raw_length) > 10 or not raw_length.isdigit():
        return None, FormError(
            HTTPStatus.BAD_REQUEST,
            AuditReason.CONTENT_LENGTH,
        )
    length = int(raw_length)
    if length > maximum_body_bytes:
        return None, FormError(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            AuditReason.BODY_TOO_LARGE,
        )
    return length, None


def parse_form(
    body: bytes,
    *,
    allowed_fields: frozenset[str],
    required_fields: frozenset[str] = frozenset(),
) -> dict[str, str] | None:
    try:
        encoded = body.decode("ascii")
    except UnicodeDecodeError:
        return None
    if not _percent_escapes_are_valid(encoded):
        return None
    try:
        pairs = parse_qsl(
            encoded,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=MAX_FORM_FIELDS,
        )
    except (UnicodeDecodeError, ValueError):
        return None
    fields: dict[str, str] = {}
    for key, value in pairs:
        if key not in allowed_fields or key in fields:
            return None
        fields[key] = value
    if not required_fields.issubset(fields):
        return None
    return fields


def safe_next_path(candidate: str | None) -> str:
    """Return only a fixed internal destination; never forward arbitrary URLs."""
    return candidate if candidate in ALLOWED_NEXT_PATHS else "/controls"


def _supported_content_type(content_type: str | None) -> bool:
    if content_type is None:
        return False
    parts = [part.strip() for part in content_type.split(";")]
    if parts[0].lower() != "application/x-www-form-urlencoded":
        return False
    for parameter in parts[1:]:
        if parameter.lower() not in {"charset=utf-8", 'charset="utf-8"'}:
            return False
    return True


def _percent_escapes_are_valid(encoded: str) -> bool:
    index = encoded.find("%")
    while index >= 0:
        if _PERCENT_ESCAPE.fullmatch(encoded[index + 1 : index + 3]) is None:
            return False
        index = encoded.find("%", index + 3)
    return True
