"""HTTP client for the EIA v2 API: pagination, retries and error classification.

Behaviour observed on the real API (see NOTES.md / FINDINGS.md):
- Pages are capped at 5000 rows.
- `total` cannot be trusted to stop paging: the facility route reports the generator row count
  (e.g. total=95 but 55 rows for one day). We stop when a page comes back short instead.
- Bad or missing keys return 403 with a JSON error code; that is never retried.
- Gateway timeouts (504) come back as HTML and requests can take ~60s; those are retried with backoff.
- The key travels as the `api_key` query parameter, so URLs are never logged as-is.
"""

import logging
import random
import re
import time
from collections.abc import Callable, Iterator
from datetime import date

import httpx

from outage_explorer.connector.routes import VALUE_COLUMNS, Route

log = logging.getLogger(__name__)

PAGE_SIZE = 5000  # EIA's maximum for JSON responses
MAX_PAGES = 1000  # safety net against an endless loop if the API misbehaves

_API_KEY_IN_URL = re.compile(r"(api_key=)[^&\s\"']+")


class RedactApiKeyFilter(logging.Filter):
    """httpx logs every request URL at INFO, query string included. Mask the key before it is emitted."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "api_key=" in message:
            record.msg, record.args = _API_KEY_IN_URL.sub(r"\1***", message), ()
        return True


# Logger-level filters run for every record created on that logger, whatever handlers are configured.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).addFilter(RedactApiKeyFilter())


class EIAError(Exception):
    """Base class. `retryable` tells the caller whether trying again later could help."""

    retryable = False


class EIAAuthError(EIAError):
    """Bad or missing API key. Fatal: retrying will not fix it."""


class EIARequestError(EIAError):
    """The API rejected our request (4xx other than auth). A bug on our side; fatal."""


class EIATransientError(EIAError):
    """Timeouts, connection errors, 429 and 5xx. Retried; raised only after retries run out."""

    retryable = True

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after  # seconds the server asked us to wait (429 Retry-After)


class EIABadResponseError(EIAError):
    """200 OK but the body is not the JSON shape we expect."""


class EIAClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        timeout: float = 180.0,
        max_retries: int = 5,
        backoff_base: float = 2.0,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise EIAAuthError("EIA_API_KEY is not set")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._http = http or httpx.Client(timeout=timeout)
        self._sleep = sleep  # injectable so tests don't actually wait

    def close(self) -> None:
        self._http.close()

    # ---- public -------------------------------------------------------------------------------

    def iter_pages(self, route: Route, start: date, end: date) -> Iterator[list[dict]]:
        """Yield every page of daily rows for `route` with period in [start, end]."""
        offset = 0
        for _ in range(MAX_PAGES):
            rows = self.fetch_page(route, start, end, offset)
            if rows:
                yield rows
            if len(rows) < PAGE_SIZE:
                return
            offset += len(rows)
        raise EIABadResponseError(f"{route.api_path}: more than {MAX_PAGES} pages, refusing to continue")

    def fetch_page(self, route: Route, start: date, end: date, offset: int) -> list[dict]:
        params: list[tuple[str, str | int]] = [
            ("frequency", "daily"),
            ("start", start.isoformat()),
            ("end", end.isoformat()),
            ("offset", offset),
            ("length", PAGE_SIZE),
        ]
        params += [(f"data[{i}]", col) for i, col in enumerate(VALUE_COLUMNS)]
        for i, col in enumerate(route.sort_columns):
            params += [(f"sort[{i}][column]", col), (f"sort[{i}][direction]", "asc")]

        body = self._get(f"/{route.api_path}/data/", params)
        try:
            rows = body["response"]["data"]
        except (KeyError, TypeError):
            raise EIABadResponseError(f"{route.api_path}: response has no response.data") from None
        if not isinstance(rows, list):
            raise EIABadResponseError(f"{route.api_path}: response.data is not a list")
        log.info("%s %s..%s offset=%d -> %d rows", route.dataset, start, end, offset, len(rows))
        return rows

    # ---- internals ----------------------------------------------------------------------------

    def _get(self, path: str, params: list[tuple[str, str | int]]) -> dict:
        """GET with retries. Only transient failures are retried; everything else raises at once."""
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._get_once(path, params)
            except EIATransientError as exc:
                if attempt > self._max_retries:
                    raise
                delay = exc.retry_after if exc.retry_after is not None else self._backoff(attempt)
                log.warning("%s attempt %d failed (%s); retrying in %.1fs", path, attempt, exc, delay)
                self._sleep(delay)

    def _get_once(self, path: str, params: list[tuple[str, str | int]]) -> dict:
        try:
            resp = self._http.get(self._base_url + path, params=[("api_key", self._api_key), *params])
        except httpx.TimeoutException:
            # `from None`: the httpx exception carries the full URL, including the key.
            raise EIATransientError("timeout") from None
        except httpx.TransportError as exc:
            raise EIATransientError(f"network error: {type(exc).__name__}") from None

        status = resp.status_code
        if status in (401, 403):
            code = _error_code(resp)
            raise EIAAuthError(f"EIA rejected the API key (HTTP {status}, {code or 'no error code'})")
        if status == 429 or status >= 500:
            raise EIATransientError(f"HTTP {status}", retry_after=_retry_after(resp))
        if status >= 400:
            raise EIARequestError(f"HTTP {status} on {path}: {_error_code(resp) or resp.text[:200]}")

        try:
            body = resp.json()
        except ValueError:
            # Seen in practice: proxies answering 200 with an HTML page. Treat like a gateway error.
            raise EIATransientError("HTTP 200 with a non-JSON body") from None
        if not isinstance(body, dict):
            raise EIABadResponseError(f"{path}: expected a JSON object")
        if "error" in body:
            raise EIARequestError(f"{path}: API error {body['error']}")
        return body

    def _backoff(self, attempt: int) -> float:
        # Exponential with "equal jitter", capped at 2 minutes: the wait is always at least half the
        # exponential step, so later retries really do wait longer (full jitter allowed 0.8s on a 4th retry
        # during a real run), while the random half keeps parallel workers from retrying in lockstep.
        step = min(120.0, self._backoff_base * 2 ** (attempt - 1))
        return step / 2 + random.uniform(0, step / 2)  # noqa: S311


def _error_code(resp: httpx.Response) -> str | None:
    try:
        err = resp.json().get("error")
    except (ValueError, AttributeError):
        return None
    return err.get("code") if isinstance(err, dict) else (str(err) if err else None)


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    try:
        return float(value) if value else None
    except ValueError:
        return None
