"""Login With Amazon (LWA) token exchange.

SP-API dropped the AWS SigV4 requirement, so authentication is now a single bearer token:
exchange the long-lived refresh token for a one-hour access token and send it as
``x-amz-access-token``. There is no request signing.

Two properties matter here:

* **Caching.** The LWA token endpoint is rate limited far below the SP-API operations it
  authorizes. Minting a token per request would throttle the app long before the API did, so
  tokens are cached per region and reused until shortly before expiry.
* **Not leaking.** A refresh token is permanent until revoked and grants full access to the
  seller account. It is never logged, never written to disk, and :meth:`__repr__` is overridden
  so it cannot reach a traceback.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from anzorlist.config import Settings
from anzorlist.marketplaces import Region

log = structlog.get_logger(__name__)

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

# Refresh this many seconds before the token actually expires. A request that starts with 30
# seconds of validity left can still land after expiry on a slow connection.
EXPIRY_SKEW_SECONDS = 300


class LwaError(RuntimeError):
    """LWA refused the token exchange. Carries Amazon's own error code, which is diagnostic:
    ``invalid_client`` means wrong client id/secret, ``invalid_grant`` means the refresh token
    was revoked or belongs to a different app."""

    def __init__(self, status: int, code: str, description: str) -> None:
        hint = {
            "invalid_client": "SPAPI_LWA_CLIENT_ID / SPAPI_LWA_CLIENT_SECRET do not match a "
            "registered SP-API application.",
            "invalid_grant": "The refresh token is revoked, expired, or was issued for a "
            "different application. Re-run the self-authorization flow.",
            "unauthorized_client": "The application is not authorized for this grant type — "
            "check the app's role selection in Seller Central.",
        }.get(code, "")
        super().__init__(f"LWA {status} {code}: {description}. {hint}".strip())
        self.status = status
        self.code = code


@dataclass
class _CachedToken:
    value: str
    expires_at: float

    def valid(self) -> bool:
        return time.monotonic() < self.expires_at - EXPIRY_SKEW_SECONDS


class TokenProvider:
    """Thread-safe, per-region LWA access-token cache.

    One instance is shared by every SP-API client in a process so that N clients across N
    marketplaces in the same region share a single token rather than minting N of them.
    """

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._cache: dict[Region, _CachedToken] = {}
        self._locks: dict[Region, threading.Lock] = {r: threading.Lock() for r in Region}
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=30.0)

    def access_token(self, region: Region) -> str:
        cached = self._cache.get(region)
        if cached is not None and cached.valid():
            return cached.value

        # Double-checked locking: concurrent callers for the same region wait for one exchange
        # rather than each performing their own.
        with self._locks[region]:
            cached = self._cache.get(region)
            if cached is not None and cached.valid():
                return cached.value
            token, expires_in = self._exchange(region)
            self._cache[region] = _CachedToken(token, time.monotonic() + expires_in)
            return token

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        wait=wait_exponential_jitter(initial=1.0, max=20.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _exchange(self, region: Region) -> tuple[str, float]:
        client_id, client_secret = self._settings.client_credentials()
        refresh_token = self._settings.refresh_token(region)

        resp = self._client.post(
            LWA_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            raise LwaError(
                resp.status_code,
                str(body.get("error", "unknown")),
                str(body.get("error_description", resp.text[:200])),
            )

        payload = resp.json()
        expires_in = float(payload.get("expires_in", 3600))
        log.info("lwa.token_issued", region=region.value, expires_in=expires_in)
        return str(payload["access_token"]), expires_in

    def invalidate(self, region: Region) -> None:
        """Drop the cached token. Called on a 403 so the next request re-mints rather than
        retrying with a token the API has already rejected."""
        self._cache.pop(region, None)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __repr__(self) -> str:  # never let a token reach a traceback or log line
        return f"<TokenProvider regions={sorted(r.value for r in self._cache)}>"
