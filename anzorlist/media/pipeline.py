"""Fetch, validate, and host product images.

Amazon does not accept uploaded image bytes for listings — it accepts **URLs**, which its
crawler fetches asynchronously. That has a consequence worth stating plainly: images on the
legacy store cannot be linked directly. The site is slow, the URLs are session-flavoured, and a
crawler failure surfaces days later as a suppressed listing with no useful error. So every image
is downloaded, validated, and re-hosted on Cloudflare R2 under a content-addressed key.

Content addressing means the same bytes always produce the same URL, so re-running the pipeline
is free and Amazon's crawler cache stays warm. It also means an image that changes on the source
site gets a new URL, which is what forces Amazon to re-crawl it.

Amazon's fine-jewelry image requirements, enforced here before anything is uploaded:

* JPEG, PNG, TIFF or GIF; JPEG strongly preferred.
* Longest side at least 1000 px — below that the zoom feature is disabled, which measurably
  reduces jewelry conversion.
* Longest side at most 10000 px, file at most 10 MB.
* The main image must be the product on a pure white background, with no text, watermark, logo,
  border, or additional props.

The last one cannot be fully verified mechanically. What *can* be checked — that the border
pixels are white — is checked, and anything ambiguous is reported as a warning for human review
rather than silently passed or silently dropped.
"""

from __future__ import annotations

import hashlib
import io
import mimetypes
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import httpx
import structlog
from PIL import Image, UnidentifiedImageError

from anzorlist.config import Settings
from anzorlist.models.product import Product

log = structlog.get_logger(__name__)

MIN_LONGEST_SIDE = 1000
RECOMMENDED_LONGEST_SIDE = 1600
MAX_LONGEST_SIDE = 10000
MAX_BYTES = 10 * 1024 * 1024
ACCEPTED_FORMATS = {"JPEG", "PNG", "TIFF", "GIF"}
LOCAL_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif"}
WHITE_THRESHOLD = 246  # per-channel; Amazon's own guidance is RGB 255,255,255 "pure white"
WHITE_BORDER_TOLERANCE = 0.90  # fraction of border pixels that must read as white


@dataclass
class ProcessedImage:
    """One image, downloaded and checked. ``public_url`` is set only after a successful upload."""

    role: str
    source_url: str
    local_path: Path | None = None
    public_url: str | None = None
    content_hash: str = ""
    width: int = 0
    height: int = 0
    image_format: str = ""
    size_bytes: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def longest_side(self) -> int:
        return max(self.width, self.height)


@dataclass
class MediaResult:
    sku: str
    images: list[ProcessedImage] = field(default_factory=list)
    source: str = "site"  # "site" or "local" (operator-supplied override directory)
    local_dir: Path | None = None
    hosting_note: str = ""  # why images were checked but not hosted, if they were not

    @property
    def main(self) -> ProcessedImage | None:
        return next((i for i in self.images if i.role == "main" and i.ok), None)

    @property
    def hosted_urls(self) -> list[str]:
        """Main first, then alternates - the order Amazon assigns to image slots.

        Empty unless the main image itself is hosted. An alternate is never promoted to main: it
        has not been through the white-background check, and slot one is what Amazon polices.
        """
        main = next((i.public_url for i in self.images if i.role == "main" and i.public_url), None)
        if main is None:
            return []
        alts = [i.public_url for i in self.images if i.role != "main" and i.public_url]
        return [main, *alts]

    @property
    def local_files(self) -> list[str]:
        """Checked image files on disk, main first - for channels that take bytes (Etsy).

        Empty unless the main image passed its checks, for the same reason as ``hosted_urls``.
        """
        main = next((i for i in self.images if i.role == "main" and i.ok and i.local_path), None)
        if main is None:
            return []
        alts = [i for i in self.images if i.role != "main" and i.ok and i.local_path]
        return [str(i.local_path) for i in [main, *alts]]

    def diagnosis(self) -> str:
        """Why there is no hosted main image, and what the operator does about it."""
        override = (
            f"Put high-resolution photos in {self.local_dir}/ - the first file by name becomes "
            f"the main image, and they replace the website's images."
            if self.local_dir is not None
            else ""
        )
        if not self.images:
            where = "the product page has no images" if self.source == "site" else "no images"
            return f"{where}. {override}".strip()
        main = next((i for i in self.images if i.role == "main"), None)
        if main is None or not main.ok:
            reason = main.errors[0] if main is not None and main.errors else "missing"
            origin = "website" if self.source == "site" else "local"
            fix = override if self.source == "site" else "Replace the first file in that folder."
            return f"the {origin} main image is unusable: {reason}. {fix}".strip()
        if main.public_url is None:
            return f"the main image passed every check but was not hosted: {self.hosting_note}"
        return ""


class MediaPipeline:
    """Download → validate → (optionally) host. Safe to run repeatedly; caches on disk by hash."""

    def __init__(
        self,
        settings: Settings,
        *,
        http: httpx.Client | None = None,
        uploader: R2Uploader | None = None,
    ) -> None:
        self.settings = settings
        self._owns_http = http is None
        self._http = http or httpx.Client(
            timeout=60.0,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent},
        )
        self._uploader = uploader

    # -- orchestration --

    def local_override_dir(self, sku: str) -> Path:
        return self.settings.images_dir / sku

    def local_overrides(self, sku: str) -> list[Path]:
        """Operator-supplied photos for this SKU, in name order. Empty when there are none."""
        folder = self.local_override_dir(sku)
        if not folder.is_dir():
            return []
        return sorted(
            (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in LOCAL_SUFFIXES),
            key=lambda p: p.name.lower(),
        )

    def process(self, product: Product, *, upload: bool = True) -> MediaResult:
        result = MediaResult(sku=product.sku, local_dir=self.local_override_dir(product.sku))
        local = self.local_overrides(product.sku)
        if local:
            # The operator's own photos win outright. Mixing them with the site's would put
            # low-resolution alternates next to a high-resolution main image.
            result.source = "local"
            candidates = [
                ("main" if i == 0 else "alternate", path.resolve().as_uri(), path.read_bytes)
                for i, path in enumerate(local)
            ]
        else:
            candidates = [
                (m.role, m.source_url, self._downloader(m.source_url))
                for m in product.media
                if m.kind == "image"
            ]
        if not candidates:
            log.warning("media.none", sku=product.sku)
            return result

        seen_hashes: set[str] = set()
        for role, source, read in candidates:
            processed = self._read_and_check(product.sku, role, source, read)
            # The legacy store serves the same file as both the main image and the first
            # thumbnail. Uploading it twice would waste a slot Amazon caps at nine.
            if processed.content_hash and processed.content_hash in seen_hashes:
                log.debug("media.duplicate_skipped", sku=product.sku, source=source)
                continue
            if processed.content_hash:
                seen_hashes.add(processed.content_hash)
            result.images.append(processed)

        if upload:
            uploader = self._get_uploader()
            if uploader is not None:
                for image in result.images:
                    if image.ok and image.local_path is not None:
                        try:
                            image.public_url = uploader.upload(image)
                        except Exception as exc:  # noqa: BLE001 - one SKU must not stop the run
                            result.hosting_note = f"upload to image hosting failed: {exc}"
                            log.error("media.upload_failed", sku=product.sku, error=str(exc))
            else:
                result.hosting_note = (
                    "image hosting is not configured (set the R2_* values in .env; see "
                    "docs/RUNBOOK.md step 5)"
                )
        else:
            result.hosting_note = "hosting was skipped (--no-upload)"

        log.info(
            "media.processed",
            sku=product.sku,
            images=len(result.images),
            usable=sum(1 for i in result.images if i.ok),
            hosted=sum(1 for i in result.images if i.public_url),
        )
        return result

    # -- fetch + validate --

    def _downloader(self, url: str) -> Callable[[], bytes]:
        def read() -> bytes:
            resp = self._http.get(url)
            resp.raise_for_status()
            return resp.content

        return read

    def _read_and_check(
        self, sku: str, role: str, source: str, read: Callable[[], bytes]
    ) -> ProcessedImage:
        out = ProcessedImage(role=role, source_url=source)
        try:
            data = read()
        except (httpx.HTTPError, OSError) as exc:
            out.errors.append(f"could not read: {exc}")
            log.warning("media.read_failed", sku=sku, source=source, error=str(exc))
            return out

        out.size_bytes = len(data)
        out.content_hash = hashlib.sha256(data).hexdigest()

        if out.size_bytes > MAX_BYTES:
            out.errors.append(f"{out.size_bytes / 1e6:.1f} MB exceeds Amazon's 10 MB limit")
        if out.size_bytes < 2000:
            out.errors.append(
                f"only {out.size_bytes} bytes — this is almost certainly a placeholder or an "
                f"error page, not a product photo"
            )
            return out

        try:
            with Image.open(io.BytesIO(data)) as img:
                out.width, out.height = img.size
                out.image_format = img.format or ""
                self._check_dimensions(out)
                if role == "main":
                    self._check_white_background(img, out)
        except UnidentifiedImageError:
            out.errors.append("not a decodable image (the URL may return an HTML error page)")
            return out
        except Exception as exc:  # noqa: BLE001 — a corrupt image must not kill the batch
            out.errors.append(f"could not inspect image: {exc}")
            return out

        cache_dir = self.settings.media_dir / sku
        cache_dir.mkdir(parents=True, exist_ok=True)
        ext = _extension_for(out.image_format, source)
        path = cache_dir / f"{out.content_hash[:16]}{ext}"
        if not path.exists():
            path.write_bytes(data)
        out.local_path = path
        return out

    @staticmethod
    def _check_dimensions(out: ProcessedImage) -> None:
        if out.image_format and out.image_format not in ACCEPTED_FORMATS:
            out.errors.append(
                f"format {out.image_format} is not accepted; Amazon takes "
                f"{', '.join(sorted(ACCEPTED_FORMATS))}"
            )
        if out.longest_side < MIN_LONGEST_SIDE:
            out.errors.append(
                f"longest side is {out.longest_side}px; at least {MIN_LONGEST_SIDE}px is "
                f"required (below that Amazon disables zoom, and jewelry does not sell without it)"
            )
        elif out.longest_side < RECOMMENDED_LONGEST_SIDE:
            out.warnings.append(
                f"longest side is {out.longest_side}px; {RECOMMENDED_LONGEST_SIDE}px+ renders "
                f"noticeably better on the detail page"
            )
        if out.longest_side > MAX_LONGEST_SIDE:
            out.errors.append(
                f"longest side is {out.longest_side}px; the maximum is {MAX_LONGEST_SIDE}px"
            )

    @staticmethod
    def _check_white_background(img: Image.Image, out: ProcessedImage) -> None:
        """Sample the border. A main image on a non-white background is a policy violation.

        This detects the common failure (a photographed-on-grey or lifestyle shot used as the
        main image) without claiming to be a full compliance check — it cannot see a watermark
        in the centre of the frame, and says so in the warning text.
        """
        try:
            rgb = img.convert("RGB")
            w, h = rgb.size
            step = max(1, min(w, h) // 100)
            coords = [(x, 0) for x in range(0, w, step)] + [(x, h - 1) for x in range(0, w, step)]
            coords += [(0, y) for y in range(0, h, step)] + [(w - 1, y) for y in range(0, h, step)]
            # An RGB image always yields an (r, g, b) tuple per pixel.
            border = [cast(tuple[int, int, int], rgb.getpixel(xy)) for xy in coords]
            if not border:
                return
            white = sum(1 for px in border if all(c >= WHITE_THRESHOLD for c in px))
            ratio = white / len(border)
            if ratio < WHITE_BORDER_TOLERANCE:
                out.warnings.append(
                    f"only {ratio:.0%} of the image border reads as white; Amazon requires the "
                    f"main image to be on a pure white background. Review this image manually — "
                    f"this check cannot see watermarks or text inside the frame."
                )
        except Exception as exc:  # noqa: BLE001
            out.warnings.append(f"background check skipped: {exc}")

    # -- hosting --

    def _get_uploader(self) -> R2Uploader | None:
        if self._uploader is not None:
            return self._uploader
        s = self.settings
        if not all(
            [
                s.r2_account_id,
                s.r2_access_key_id,
                s.r2_secret_access_key,
                s.r2_bucket,
                s.r2_public_base_url,
            ]
        ):
            return None
        self._uploader = R2Uploader(s)
        return self._uploader

    def close(self) -> None:
        if self._owns_http:
            self._http.close()


class R2Uploader:
    """Uploads to Cloudflare R2 over the S3-compatible API.

    R2 was chosen for zero egress fees: Amazon's crawler re-fetches listing images repeatedly
    across marketplaces and over the life of a listing, and on S3 that egress is a recurring
    per-image cost for no benefit.
    """

    def __init__(self, settings: Settings) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = settings.r2_bucket
        self.public_base = (settings.r2_public_base_url or "").rstrip("/")
        self._client = boto3.client(
            "s3",
            endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=settings.r2_access_key_id.get_secret_value(),  # type: ignore[union-attr]
            aws_secret_access_key=settings.r2_secret_access_key.get_secret_value(),  # type: ignore[union-attr]
            config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
            region_name="auto",
        )
        self._known: set[str] = set()

    def key_for(self, image: ProcessedImage) -> str:
        ext = image.local_path.suffix if image.local_path else ".jpg"
        return f"products/{image.content_hash[:2]}/{image.content_hash}{ext}"

    def upload(self, image: ProcessedImage) -> str:
        """Upload if absent, then return the public URL.

        Content-addressed, so this is idempotent.
        """
        key = self.key_for(image)
        url = f"{self.public_base}/{key}"

        if key in self._known:
            return url
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            self._known.add(key)
            log.debug("media.already_hosted", key=key)
            return url
        except Exception:  # noqa: BLE001 — any miss means "not there", so upload it
            pass

        assert image.local_path is not None
        content_type = mimetypes.guess_type(str(image.local_path))[0] or "image/jpeg"
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=image.local_path.read_bytes(),
            ContentType=content_type,
            # Amazon's crawler re-fetches over months; content-addressed keys are immutable, so
            # a long max-age is both safe and what keeps repeat crawls cheap.
            CacheControl="public, max-age=31536000, immutable",
        )
        self._known.add(key)
        log.info("media.uploaded", key=key, bytes=image.size_bytes, url=url)
        return url


def _extension_for(image_format: str, url: str) -> str:
    mapping = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "TIFF": ".tif"}
    if image_format in mapping:
        return mapping[image_format]
    suffix = Path(url.split("?")[0]).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".gif", ".tif"} else ".jpg"
