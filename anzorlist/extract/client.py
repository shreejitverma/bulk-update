"""Site client for the legacy Classic ASP store.

Responsibilities:
  * Build the deterministic product URL (no crawling/JS).
  * Fetch with a real User-Agent, 1 req/sec throttle, and exponential backoff on 429/5xx.
  * Decode legacy windows-1252/latin-1 HTML correctly (mojibake defense).
  * Cache every raw response to ``data/raw/{sku}/`` keyed by content hash; re-fetch only on
    hash change.
  * Probe (don't assume) the per-SKU video URL.

No parsing happens here — this module only produces decoded HTML text + a content hash.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

log = structlog.get_logger(__name__)

DEFAULT_BASE_URL = "https://www.anzorjewelrycorp.com"
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; anzorlist/0.1; +ops@anzorjewelrycorp.com)"
PRODUCT_PATH = "/Scripts/prodview.asp"
CATEGORY_PATH = "/Scripts/prodList.asp"
VIDEO_URL_TMPL = "https://anzorjewelrycorp.com/jewelry/{sku_lower}va.mp4"

_CHARSET_RE = re.compile(rb"charset=([\w-]+)", re.IGNORECASE)


class RetryableHTTP(Exception):
    """Raised on 429/5xx so tenacity retries with backoff (never a hot loop)."""


@dataclass
class FetchResult:
    sku: str
    url: str
    html: str  # decoded to str
    raw_bytes: bytes
    content_hash: str
    encoding: str
    from_cache: bool
    cache_path: Path


class SiteClient:
    """Throttled, caching HTTP client. One instance per run; thread-safe throttle."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        req_per_sec: float = 1.0,
        data_dir: Path | str = "data/raw",
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self._min_interval = 1.0 / req_per_sec if req_per_sec > 0 else 0.0
        self.data_dir = Path(data_dir)
        self._last_request_at = 0.0
        self._throttle_lock = threading.Lock()
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        )

    # -- URL construction (deterministic; do not crawl) --

    def product_url(self, sku: str) -> str:
        return f"{self.base_url}{PRODUCT_PATH}?SKU={sku}"

    def category_url(self, id_category: int) -> str:
        return f"{self.base_url}{CATEGORY_PATH}?idCategory={id_category}"

    def video_url(self, sku: str) -> str:
        return VIDEO_URL_TMPL.format(sku_lower=sku.lower())

    # -- decoding --

    @staticmethod
    def _decode(raw: bytes) -> tuple[str, str]:
        """Decode legacy HTML. windows-1252 is a superset of latin-1 and fixes smart-quote
        mojibake, so we prefer it regardless of the declared iso-8859-1."""
        m = _CHARSET_RE.search(raw[:2048])
        declared = m.group(1).decode("ascii", "ignore").lower() if m else ""
        # Treat the latin-1 family as windows-1252 (handles 0x80-0x9F punctuation).
        if declared in {"", "iso-8859-1", "latin-1", "latin1", "windows-1252", "cp1252"}:
            enc = "cp1252"
        else:
            enc = declared
        try:
            return raw.decode(enc), enc
        except (LookupError, UnicodeDecodeError):
            return raw.decode("cp1252", errors="replace"), "cp1252"

    # -- throttle --

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        with self._throttle_lock:
            wait = self._min_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    # -- fetching --

    @retry(
        retry=retry_if_exception_type((RetryableHTTP, httpx.TransportError)),
        wait=wait_exponential_jitter(initial=1.0, max=30.0),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _get(self, url: str) -> httpx.Response:
        self._throttle()
        resp = self._client.get(url)
        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = resp.headers.get("Retry-After")
            log.warning("site.retryable", url=url, status=resp.status_code, retry_after=retry_after)
            raise RetryableHTTP(f"{resp.status_code} for {url}")
        resp.raise_for_status()
        return resp

    def fetch_product(self, sku: str, *, force: bool = False) -> FetchResult:
        """Fetch a product page. Uses the on-disk cache unless ``force`` or the remote content
        hash has changed. Raw bytes and decoded HTML are cached under ``data/raw/{sku}/``."""
        url = self.product_url(sku)
        cache_dir = self.data_dir / sku
        html_path = cache_dir / "prodview.html"
        hash_path = cache_dir / "prodview.sha256"

        if not force and html_path.exists() and hash_path.exists():
            raw = html_path.read_bytes()
            cached_hash = hash_path.read_text().strip()
            if hashlib.sha256(raw).hexdigest() == cached_hash:
                html, enc = self._decode(raw)
                log.info("site.cache_hit", sku=sku, content_hash=cached_hash[:12])
                return FetchResult(
                    sku=sku,
                    url=url,
                    html=html,
                    raw_bytes=raw,
                    content_hash=cached_hash,
                    encoding=enc,
                    from_cache=True,
                    cache_path=html_path,
                )

        resp = self._get(url)
        raw = resp.content
        content_hash = hashlib.sha256(raw).hexdigest()
        html, enc = self._decode(raw)

        cache_dir.mkdir(parents=True, exist_ok=True)
        html_path.write_bytes(raw)
        hash_path.write_text(content_hash)
        log.info(
            "site.fetched",
            sku=sku,
            status=resp.status_code,
            bytes=len(raw),
            content_hash=content_hash[:12],
            encoding=enc,
        )
        return FetchResult(
            sku=sku,
            url=url,
            html=html,
            raw_bytes=raw,
            content_hash=content_hash,
            encoding=enc,
            from_cache=False,
            cache_path=html_path,
        )

    def probe_video(self, sku: str) -> bool:
        """HEAD the deterministic video URL. Probe, don't assume — returns False on any failure."""
        url = self.video_url(sku)
        try:
            self._throttle()
            resp = self._client.head(url)
            ok = resp.status_code == 200
            log.info("site.video_probe", sku=sku, url=url, status=resp.status_code, ok=ok)
            return ok
        except httpx.HTTPError as exc:
            log.warning("site.video_probe_failed", sku=sku, url=url, error=str(exc))
            return False

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> SiteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
