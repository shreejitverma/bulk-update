"""eBay REST transport: OAuth tokens, retries, and eBay's error format.

Every call uses a **user token**, minted from the seller's refresh token, for the Inventory and
Account APIs (they act on the seller's listings and policies). It expires after about two hours
and is cached until five minutes before expiry. The refresh token itself lasts about 18 months
and is not rotated by a refresh, so it can live in ``.env``.

Retries follow the same rules as the SP-API transport: 429 and 5xx back off and retry; any other
4xx is deterministic and raised immediately with eBay's own error ids, because those ids (25002,
25604, ...) are what the eBay docs and support act on.
"""

from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx
import structlog

from anzorlist.config import Settings

log = structlog.get_logger(__name__)

Method = Literal["GET", "POST", "PUT", "DELETE"]

API_HOSTS = {"PRODUCTION": "https://api.ebay.com", "SANDBOX": "https://api.sandbox.ebay.com"}
TOKEN_PATH = "/identity/v1/oauth2/token"

# Scopes the seller granted when the refresh token was minted. Requesting a scope the grant did
# not include fails with invalid_scope, so this is exactly the set the runbook tells them to grant.
USER_SCOPES = (
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",
)
EXPIRY_SKEW_SECONDS = 300
MAX_ATTEMPTS = 5


class EbayError(RuntimeError):
    """A non-retryable eBay API failure, carrying eBay's error ids and messages."""

    def __init__(self, status: int, method: str, path: str, errors: list[dict[str, Any]]) -> None:
        parts = [
            f"{e.get('errorId', '?')}: {e.get('longMessage') or e.get('message', '')}"
            for e in errors
        ] or ["(no error body)"]
        super().__init__(f"eBay {method} {path} failed with {status} - " + " | ".join(parts))
        self.status = status
        self.errors = errors

    @property
    def error_ids(self) -> set[int]:
        return {int(e["errorId"]) for e in self.errors if str(e.get("errorId", "")).isdigit()}


class EbayAuthError(RuntimeError):
    """The token exchange was refused. eBay's error code says which credential is wrong."""


@dataclass
class _Token:
    value: str
    expires_at: float

    def valid(self) -> bool:
        return time.monotonic() < self.expires_at - EXPIRY_SKEW_SECONDS


class EbayClient:
    """One client per process. Thread-safe token cache."""

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None) -> None:
        env = settings.ebay_env.upper()
        if env not in API_HOSTS:
            raise ValueError(f"EBAY_ENV must be SANDBOX or PRODUCTION, got {settings.ebay_env!r}")
        self.settings = settings
        self.environment = env
        self.base_url = API_HOSTS[env]
        self.marketplace_id = settings.ebay_marketplace_id
        self._http = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(60.0, connect=15.0),
            headers={"accept": "application/json"},
        )
        self._access: _Token | None = None
        self._lock = threading.Lock()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> EbayClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ tokens

    def _token(self) -> str:
        with self._lock:
            if self._access is not None and self._access.valid():
                return self._access.value
            client_id, client_secret, refresh_token = self.settings.ebay_credentials()
            data = {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": " ".join(USER_SCOPES),
            }
            basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
            resp = self._http.post(
                f"{self.base_url}{TOKEN_PATH}",
                data=data,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            if resp.status_code != 200:
                try:
                    body = resp.json()
                except ValueError:
                    body = {}
                code = body.get("error", "unknown")
                hint = {
                    "invalid_client": "EBAY_CLIENT_ID / EBAY_CLIENT_SECRET do not match a keyset "
                    f"for the {self.environment} environment.",
                    "invalid_grant": "EBAY_REFRESH_TOKEN is expired, revoked, or from another "
                    "keyset or environment. Mint a new user token.",
                    "invalid_scope": "The refresh token was granted without the sell.inventory "
                    "and sell.account scopes.",
                }.get(code, "")
                raise EbayAuthError(
                    f"eBay token exchange failed ({resp.status_code} {code}): "
                    f"{body.get('error_description', resp.text[:200])}. {hint}".strip()
                )
            payload = resp.json()
            self._access = _Token(
                str(payload["access_token"]),
                time.monotonic() + float(payload.get("expires_in", 7200)),
            )
            return self._access.value

    # ------------------------------------------------------------------ requests

    def request(
        self,
        method: Method,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        ok_statuses: tuple[int, ...] = (),
    ) -> tuple[int, Any]:
        """One call with retry. Returns (status, decoded JSON or None).

        ``ok_statuses`` lets a caller treat a specific 4xx as an answer rather than an error, such
        as 404 from a lookup.
        """
        url = f"{self.base_url}{path}"
        last: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            headers = {
                "Authorization": f"Bearer {self._token()}",
                # The Inventory API requires Content-Language on writes, and rejects a mismatch
                # with the marketplace's language.
                "Content-Language": "en-US",
                "X-EBAY-C-MARKETPLACE-ID": self.marketplace_id,
            }
            try:
                resp = self._http.request(
                    method, url, json=json_body, params=params, headers=headers
                )
            except httpx.TransportError as exc:
                last = EbayError(0, method, path, [{"errorId": "transport", "message": str(exc)}])
                time.sleep(min(30.0, 2.0**attempt))
                continue

            log.info("ebay.call", method=method, path=path, status=resp.status_code)
            body = _decode(resp)
            if resp.status_code < 300 or resp.status_code in ok_statuses:
                return resp.status_code, body
            errors = body.get("errors", []) if isinstance(body, dict) else []
            if resp.status_code == 401 and attempt == 1:
                # An expired token looks like any other 401; mint a fresh one once.
                with self._lock:
                    self._access = None
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last = EbayError(resp.status_code, method, path, errors)
                time.sleep(min(30.0, 2.0**attempt))
                continue
            raise EbayError(resp.status_code, method, path, errors)
        assert last is not None
        raise last


def _decode(resp: httpx.Response) -> Any:
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return {"errors": [{"errorId": f"HTTP{resp.status_code}", "message": resp.text[:300]}]}
