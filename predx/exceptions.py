from __future__ import annotations
import httpx


class PredxError(Exception):
    pass


class AuthError(PredxError):
    pass


class RateLimitError(PredxError):
    pass


class NotFoundError(PredxError):
    pass


class MarketClosedError(PredxError):
    pass


class InsufficientFundsError(PredxError):
    pass


def map_http_error(exc: httpx.HTTPStatusError) -> PredxError:
    code = exc.response.status_code
    text = exc.response.text
    if code == 401:
        return AuthError(f"Unauthorized: {text}")
    if code == 403:
        return AuthError(f"Forbidden: {text}")
    if code == 404:
        return NotFoundError(f"Not found: {text}")
    if code == 429:
        return RateLimitError(f"Rate limited: {text}")
    return PredxError(f"HTTP {code}: {text}")
