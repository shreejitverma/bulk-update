"""Etsy Open API v3 transport.

Two Etsy specifics matter:

* **Refresh tokens rotate.** Every refresh returns a new refresh token and invalidates the old
  one. The new token is written to ``data/etsy/token.json`` (mode 0600) before it is used, and is
  preferred over ``ETSY_REFRESH_TOKEN`` from then on. Losing it would lock the tool out until the
  seller re-authorizes.
* **Every request carries ``x-api-key``**: the app keystring, joined to the shared secret as
  ``keystring:secret`` when ``ETSY_SHARED_SECRET`` is set, as Etsy now requires.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Literal

import httpx
import structlog

from anzorlist.config import Settings

log = structlog.get_logger(__name__)

API = "https://api.etsy.com"
TOKEN_URL = f"{API}/v3/public/oauth/token"
MAX_ATTEMPTS = 5

Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


class EtsyError(RuntimeError):
    def __init__(self, status: int, method: str, path: str, detail: str) -> None:
        super().__init__(f"Etsy {method} {path} failed with {status}: {detail}")
        self.status = status
        self.detail = detail


class EtsyClient:
    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        values = settings.etsy_settings()
        self.shop_id = values["ETSY_SHOP_ID"]
        secret = (
            settings.etsy_shared_secret.get_secret_value() if settings.etsy_shared_secret else ""
        )
        self._api_key = f"{values['ETSY_API_KEY']}:{secret}" if secret else values["ETSY_API_KEY"]
        self._client_id = values["ETSY_API_KEY"]
        self._token_file = settings.data_dir / "etsy" / "token.json"
        self._http = httpx.Client(transport=transport, timeout=httpx.Timeout(60.0, connect=15.0))
        self._lock = threading.Lock()
        self._access: tuple[str, float] | None = None

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ tokens

    def _refresh_token(self) -> str:
        if self._token_file.exists():
            return str(json.loads(self._token_file.read_text())["refresh_token"])
        return self.settings.etsy_settings()["ETSY_REFRESH_TOKEN"]

    def _save_refresh_token(self, token: str) -> None:
        self._token_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._token_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"refresh_token": token}))
        os.chmod(tmp, 0o600)
        tmp.replace(self._token_file)

    def _token(self) -> str:
        with self._lock:
            if self._access and time.monotonic() < self._access[1] - 300:
                return self._access[0]
            resp = self._http.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._client_id,
                    "refresh_token": self._refresh_token(),
                },
            )
            if resp.status_code != 200:
                raise EtsyError(
                    resp.status_code,
                    "POST",
                    "oauth/token",
                    f"{resp.text[:200]} - re-authorize the app and set ETSY_REFRESH_TOKEN "
                    f"(delete {self._token_file} if it holds a revoked token)",
                )
            payload = resp.json()
            # Persist the rotated refresh token before anything else can fail.
            self._save_refresh_token(str(payload["refresh_token"]))
            self._access = (
                str(payload["access_token"]),
                time.monotonic() + float(payload.get("expires_in", 3600)),
            )
            return self._access[0]

    # ------------------------------------------------------------------ requests

    def request(
        self,
        method: Method,
        path: str,
        *,
        json_body: Any = None,
        files: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> Any:
        url = f"{API}{path}"
        last: EtsyError | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            headers = {"x-api-key": self._api_key}
            if auth:
                headers["Authorization"] = f"Bearer {self._token()}"
            try:
                resp = self._http.request(
                    method, url, json=json_body, files=files, data=data, headers=headers
                )
            except httpx.TransportError as exc:
                last = EtsyError(0, method, path, str(exc))
                time.sleep(min(30.0, 2.0**attempt))
                continue
            log.info("etsy.call", method=method, path=path, status=resp.status_code)
            if resp.status_code < 300:
                return resp.json() if resp.content else None
            detail = resp.text[:400]
            if resp.status_code == 401 and attempt == 1:
                with self._lock:
                    self._access = None
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last = EtsyError(resp.status_code, method, path, detail)
                time.sleep(min(30.0, 2.0**attempt))
                continue
            raise EtsyError(resp.status_code, method, path, detail)
        assert last is not None
        raise last
