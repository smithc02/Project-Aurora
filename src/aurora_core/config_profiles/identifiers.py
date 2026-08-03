"""Strict logical identifiers for managed configuration profiles and backups."""

from __future__ import annotations

import re
from datetime import datetime

PROFILE_ID_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){0,39}\Z")
BACKUP_ID_PATTERN = re.compile(
    r"(?P<timestamp>[0-9]{8}T[0-9]{12}Z)-(?P<suffix>[0-9a-f]{12})\Z"
)


def is_profile_id(value: object) -> bool:
    """Return whether ``value`` is one bounded logical profile identifier."""
    return isinstance(value, str) and PROFILE_ID_PATTERN.fullmatch(value) is not None


def require_profile_id(value: object) -> str:
    """Return a valid profile identifier or raise a value-only error."""
    if not is_profile_id(value):
        raise ValueError("invalid profile identifier")
    assert isinstance(value, str)
    return value


def is_backup_id(value: object) -> bool:
    """Return whether ``value`` is one generated backup identifier."""
    if not isinstance(value, str):
        return False
    match = BACKUP_ID_PATTERN.fullmatch(value)
    if match is None:
        return False
    try:
        datetime.strptime(match.group("timestamp"), "%Y%m%dT%H%M%S%fZ")
    except ValueError:
        return False
    return True


def require_backup_id(value: object) -> str:
    """Return a valid generated backup identifier or raise a value-only error."""
    if not is_backup_id(value):
        raise ValueError("invalid backup identifier")
    assert isinstance(value, str)
    return value
