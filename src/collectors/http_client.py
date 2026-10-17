"""Small JSON HTTP client shared by offer-source adapters (PRD 4.4, 11).

- injectable transport (tests never touch the network)
- per-run call cap, minimum interval between calls
- bounded retries with backoff for network errors / 429 / 5xx
- 401/403 raise ``ApiAuthError`` immediately (no retry)
- request URLs and headers are never put into exception messages, so query
  strings / headers carrying credentials cannot leak into logs or the DB
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

# transport(method, url, headers, body, timeout) -> (status, text)
HttpTransport = Callable[[str, str, dict[str, str], Optional[bytes], float], tuple[int, str]]


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class ApiAuthError(ApiError):
    """Credentials missing/invalid or the API is not enabled for the app."""


class CallLimitReached(ApiError):
    def __init__(self, limit: int) -> None:
        super().__init__(0, f"run call limit reached ({limit})")


def urllib_http_transport(
    method: str, url: str, headers: dict[str, str], body: Optional[bytes], timeout: float
) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https hosts
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def summarize_error_body(text: str) -> str:
    """Short, credential-free description of an error response body."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return (text or "")[:160]
    if isinstance(data, dict):
        if "errors" in data and isinstance(data["errors"], list) and data["errors"]:
            e = data["errors"][0]
            return f"{e.get('errorId', '')} {e.get('message', '')}".strip()[:160]
        for a, b in (("errorCode", "errorMessage"), ("error", "error_description")):
            if a in data or b in data:
                return f"{data.get(a, '')} {data.get(b, '')}".strip()[:160]
    return str(data)[:160]


class JsonHttpClient:
    def __init__(
        self,
        transport: Optional[HttpTransport] = None,
        max_calls: int = 50,
        min_interval_s: float = 0.5,
        max_retries: int = 2,
        timeout_s: float = 20.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport or urllib_http_transport
        self.max_calls = max_calls
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.timeout_s = timeout_s
        self._sleep = sleep
        self.calls_made = 0
        self._last: Optional[float] = None

    @property
    def calls_remaining(self) -> int:
        return max(0, self.max_calls - self.calls_made)

    def request(
        self,
        method: str,
        url: str,
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        body: Optional[bytes] = None,
    ) -> Any:
        if params:
            clean = {k: v for k, v in params.items() if v is not None and v != ""}
            url = f"{url}?{urllib.parse.urlencode(clean, quote_via=urllib.parse.quote)}"
        attempt = 0
        while True:
            if self.calls_made >= self.max_calls:
                raise CallLimitReached(self.max_calls)
            if self._last is not None:
                wait = self.min_interval_s - (time.monotonic() - self._last)
                if wait > 0:
                    self._sleep(wait)
            self.calls_made += 1
            self._last = time.monotonic()
            try:
                status, text = self._transport(method, url, dict(headers or {}), body, self.timeout_s)
            except OSError as exc:
                status, text = -1, type(exc).__name__
            if status == 200:
                try:
                    return json.loads(text)
                except ValueError as exc:
                    raise ApiError(status, "invalid JSON response") from exc
            if status in (401, 403):
                raise ApiAuthError(status, summarize_error_body(text))
            if (status == -1 or status == 429 or status >= 500) and attempt < self.max_retries:
                attempt += 1
                self._sleep(1.0 * (2 ** (attempt - 1)))
                continue
            raise ApiError(status, summarize_error_body(text))
