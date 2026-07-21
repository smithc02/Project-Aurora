"""Internal, endpoint-safe errors for read-only WLED validation."""

from __future__ import annotations


class WLEDTransportError(Exception):
    reason_code = "connection_failed"


class WLEDTimeoutError(WLEDTransportError):
    reason_code = "timeout"


class WLEDRedirectError(WLEDTransportError):
    reason_code = "redirect_rejected"


class WLEDHTTPError(WLEDTransportError):
    reason_code = "http_error"


class WLEDResponseTooLargeError(WLEDTransportError):
    reason_code = "response_too_large"
