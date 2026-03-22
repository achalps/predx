from __future__ import annotations
import time
from typing import Any, Optional
import httpx

from ..exceptions import map_http_error


class BaseClient:
    """
    Shared HTTP client with lazy session init, retry logic, and rate limit handling.
    All exchange clients inherit from this.
    """

    def __init__(self, base_url: str, timeout: float = 10.0, max_retries: int = 3):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._session: Optional[httpx.Client] = None

    def _get_session(self) -> httpx.Client:
        if self._session is None or self._session.is_closed:
            self._session = httpx.Client(
                base_url=self._base_url,
                timeout=self._timeout,
            )
        return self._session

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
        extra_headers: Optional[dict] = None,
    ) -> Any:
        session = self._get_session()
        last_exc: Optional[Exception] = None

        for attempt in range(self._max_retries):
            headers = extra_headers or {}
            try:
                resp = session.request(
                    method, path, params=params, json=json, headers=headers
                )
                if resp.status_code == 429:
                    wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                raise map_http_error(e) from e
            except httpx.RequestError as e:
                last_exc = e
                time.sleep(2 ** attempt)

        raise last_exc or RuntimeError("Request failed after retries")

    def close(self) -> None:
        if self._session and not self._session.is_closed:
            self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
