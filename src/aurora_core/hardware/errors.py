"""Internal, endpoint-safe errors for read-only hardware validation."""

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


class HyperHDRTransportError(Exception):
    reason_code = "connection_failed"


class HyperHDRTimeoutError(HyperHDRTransportError):
    reason_code = "timeout"


class HyperHDRRedirectError(HyperHDRTransportError):
    reason_code = "redirect_rejected"


class HyperHDRAuthorizationError(HyperHDRTransportError):
    reason_code = "authorization_required"


class HyperHDRHTTPError(HyperHDRTransportError):
    reason_code = "http_error"


class HyperHDRResponseTooLargeError(HyperHDRTransportError):
    reason_code = "response_too_large"
