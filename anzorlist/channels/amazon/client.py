"""SP-API HTTP transport: rate limiting, retries, error decoding.

SP-API enforces a **token bucket per operation, per selling partner** — not a global limit. A
burst of Listings Items writes will 429 while Product Type Definitions reads sail through. So the
limiter here is keyed on the operation name, seeded with Amazon's documented rate/burst, and then
corrected at runtime from the ``x-amzn-RateLimit-Limit`` response header, which reports the
account's *actual* rate (Amazon adjusts it per seller and does not document the adjusted value).

Retry policy is deliberately narrow. 429 and 5xx are retried with jitter; 400 is not, because a
malformed listing payload will be malformed on every attempt and retrying it only burns quota.
403 triggers exactly one token refresh and retry, since an expired token is indistinguishable
from a genuine permission failure at the HTTP layer.

Every request logs ``x-amzn-RequestId``. That ID is the only thing Amazon Selling Partner Support
will act on, so losing it makes an escalation impossible.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
import structlog

from anzorlist.channels.amazon.auth import TokenProvider
from anzorlist.config import Settings
from anzorlist.marketplaces import REGION_ENDPOINTS, REGION_SANDBOX_ENDPOINTS, Region

log = structlog.get_logger(__name__)

Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]

# Amazon's documented default (rate per second, burst) per operation. These are starting values
# only — `x-amzn-RateLimit-Limit` overrides them per account at runtime.
DEFAULT_RATE_LIMITS: dict[str, tuple[float, int]] = {
    "getListingsItem": (5.0, 10),
    "putListingsItem": (5.0, 10),
    "patchListingsItem": (5.0, 5),
    "deleteListingsItem": (5.0, 10),
    "searchListingsItems": (5.0, 10),
    "getListingsRestrictions": (5.0, 10),
    "searchDefinitionsProductTypes": (5.0, 10),
    "getDefinitionsProductType": (5.0, 10),
    "searchCatalogItems": (2.0, 2),
    "getCatalogItem": (2.0, 2),
    "createFeed": (0.0083, 15),
    "getFeed": (2.0, 15),
    "createFeedDocument": (0.5, 15),
    "getFeedDocument": (0.0222, 15),
    "getMarketplaceParticipations": (0.016, 15),
}
_FALLBACK_LIMIT = (1.0, 1)  # unknown operation: crawl rather than guess generously


class SpApiError(RuntimeError):
    """A non-retryable SP-API failure, decoded into something an operator can act on."""

    def __init__(
        self,
        status: int,
        operation: str,
        errors: list[dict[str, Any]],
        request_id: str | None,
        body: str = "",
    ) -> None:
        parts = [f"{e.get('code', '?')}: {e.get('message', '')}" for e in errors] or [body[:300]]
        super().__init__(
            f"SP-API {operation} failed with {status} — "
            + " | ".join(parts)
            + (f" (x-amzn-RequestId: {request_id})" if request_id else "")
        )
        self.status = status
        self.operation = operation
        self.errors = errors
        self.request_id = request_id


class SpApiThrottled(RuntimeError):
    """429 after the retry budget was exhausted. Distinct from SpApiError so callers can
    slow a batch down rather than treating the SKU as invalid."""


@dataclass
class _Bucket:
    """Token bucket. ``rate`` tokens accrue per second up to ``burst``."""

    rate: float
    burst: int
    tokens: float = field(default=0.0)
    updated: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.tokens = float(self.burst)

    def acquire(self) -> float:
        """Consume one token, blocking if necessary. Returns the seconds spent waiting."""
        with self.lock:
            now = time.monotonic()
            self.tokens = min(float(self.burst), self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return 0.0
            wait = (1.0 - self.tokens) / self.rate if self.rate > 0 else 60.0
            self.tokens = 0.0
            self.updated = now + wait
        time.sleep(wait)
        return wait

    def retune(self, rate: float) -> None:
        """Adopt the account's real rate as reported by Amazon."""
        with self.lock:
            if rate > 0 and abs(rate - self.rate) / max(self.rate, 1e-9) > 0.01:
                self.rate = rate
                self.burst = max(1, min(self.burst, int(rate * 10) or 1))


@dataclass
class SpApiResponse:
    status: int
    json: Any
    headers: httpx.Headers
    request_id: str | None


class SpApiClient:
    """One client per region. Thread-safe; share a single instance across worker threads."""

    MAX_ATTEMPTS = 5

    def __init__(
        self,
        settings: Settings,
        region: Region,
        *,
        tokens: TokenProvider | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self.region = region
        endpoints = REGION_SANDBOX_ENDPOINTS if settings.use_sandbox else REGION_ENDPOINTS
        self.endpoint = endpoints[region]
        self._tokens = tokens or TokenProvider(settings)
        self._owns_http = http is None
        self._http = http or httpx.Client(
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=False,
            headers={
                "user-agent": "anzorlist/0.1 (Language=Python; Platform=Linux)",
                "accept": "application/json",
            },
        )
        self._buckets: dict[str, _Bucket] = {}
        self._buckets_lock = threading.Lock()

    @property
    def seller_id(self) -> str:
        return self.settings.seller_id(self.region)

    def _bucket(self, operation: str) -> _Bucket:
        with self._buckets_lock:
            b = self._buckets.get(operation)
            if b is None:
                rate, burst = DEFAULT_RATE_LIMITS.get(operation, _FALLBACK_LIMIT)
                b = _Bucket(rate=rate, burst=burst)
                self._buckets[operation] = b
            return b

    def request(
        self,
        method: Method,
        path: str,
        *,
        operation: str,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        expect_json: bool = True,
    ) -> SpApiResponse:
        """Perform one SP-API call with limiting, retry, and error decoding."""
        url = f"{self.endpoint}{path}"
        bucket = self._bucket(operation)
        refreshed_once = False
        last_exc: Exception | None = None

        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            waited = bucket.acquire()
            token = self._tokens.access_token(self.region)
            headers = {"x-amz-access-token": token}
            started = time.monotonic()
            try:
                resp = self._http.request(
                    method, url, params=params, json=json_body, headers=headers
                )
            except httpx.TransportError as exc:
                last_exc = exc
                backoff = min(30.0, 2.0**attempt)
                log.warning(
                    "spapi.transport_error",
                    operation=operation,
                    attempt=attempt,
                    error=str(exc),
                    backoff=backoff,
                )
                time.sleep(backoff)
                continue

            request_id = resp.headers.get("x-amzn-requestid") or resp.headers.get(
                "x-amzn-request-id"
            )
            self._retune(bucket, resp, operation)
            log.info(
                "spapi.call",
                operation=operation,
                method=method,
                status=resp.status_code,
                attempt=attempt,
                ms=round((time.monotonic() - started) * 1000),
                throttle_wait_ms=round(waited * 1000) or None,
                request_id=request_id,
            )

            if 200 <= resp.status_code < 300:
                payload: Any = None
                if expect_json and resp.content:
                    try:
                        payload = resp.json()
                    except ValueError:
                        payload = resp.text
                elif resp.content:
                    payload = resp.content
                return SpApiResponse(resp.status_code, payload, resp.headers, request_id)

            errors = _decode_errors(resp)

            if resp.status_code == 429:
                delay = _retry_after(resp, attempt)
                log.warning(
                    "spapi.throttled",
                    operation=operation,
                    attempt=attempt,
                    delay=delay,
                    request_id=request_id,
                )
                time.sleep(delay)
                continue

            if resp.status_code == 403 and not refreshed_once:
                # Could be an expired token or a genuine authorization gap. Refresh once; if the
                # second attempt also 403s, it is a real permissions problem and must surface.
                refreshed_once = True
                self._tokens.invalidate(self.region)
                log.warning("spapi.forbidden_retry", operation=operation, request_id=request_id)
                continue

            if resp.status_code >= 500:
                delay = min(30.0, 2.0**attempt)
                log.warning(
                    "spapi.server_error",
                    operation=operation,
                    status=resp.status_code,
                    attempt=attempt,
                    delay=delay,
                    request_id=request_id,
                )
                time.sleep(delay)
                continue

            # 4xx other than 429/403: deterministic. Retrying wastes quota and delays the fix.
            raise SpApiError(resp.status_code, operation, errors, request_id, resp.text)

        if last_exc is not None:
            raise SpApiError(
                0, operation, [{"code": "TransportError", "message": str(last_exc)}], None
            ) from last_exc
        raise SpApiThrottled(
            f"{operation} still throttled after {self.MAX_ATTEMPTS} attempts. "
            f"Reduce concurrency or retry later; SP-API quota is per selling partner."
        )

    @staticmethod
    def _retune(bucket: _Bucket, resp: httpx.Response, operation: str) -> None:
        raw = resp.headers.get("x-amzn-ratelimit-limit")
        if not raw:
            return
        try:
            rate = float(raw.split(";")[0].strip())
        except ValueError:
            return
        if rate > 0 and abs(rate - bucket.rate) / max(bucket.rate, 1e-9) > 0.01:
            log.debug("spapi.rate_retuned", operation=operation, was=bucket.rate, now=rate)
            bucket.retune(rate)

    # -- convenience wrappers --

    def get(self, path: str, *, operation: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, operation=operation, params=params).json

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> SpApiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _decode_errors(resp: httpx.Response) -> list[dict[str, Any]]:
    try:
        body = resp.json()
    except ValueError:
        return [{"code": f"HTTP{resp.status_code}", "message": resp.text[:400]}]
    errors = body.get("errors") if isinstance(body, dict) else None
    if isinstance(errors, list):
        return [e for e in errors if isinstance(e, dict)]
    return [{"code": f"HTTP{resp.status_code}", "message": str(body)[:400]}]


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    """Honour Retry-After when present; otherwise exponential backoff with a ceiling."""
    header = resp.headers.get("retry-after")
    if header:
        try:
            return min(60.0, max(1.0, float(header)))
        except ValueError:
            pass
    return min(30.0, 2.0**attempt)


class ClientPool:
    """Holds one :class:`SpApiClient` per region, sharing a single token provider.

    Listing across US+CA+MX+EU touches two regions; without pooling, each marketplace would open
    its own connection pool and mint its own token.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tokens = TokenProvider(settings)
        self._clients: dict[Region, SpApiClient] = {}
        self._lock = threading.Lock()

    def for_region(self, region: Region) -> SpApiClient:
        with self._lock:
            client = self._clients.get(region)
            if client is None:
                client = SpApiClient(self._settings, region, tokens=self._tokens)
                self._clients[region] = client
            return client

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._tokens.close()

    def __enter__(self) -> ClientPool:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
