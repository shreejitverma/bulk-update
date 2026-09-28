"""Catalog-wide image compliance audit.

Amazon suppresses a listing whose main image is under 1000 px on the longest side, and jewelry
does not convert without the zoom that threshold unlocks. On the Anzor catalog this is the
binding constraint: a survey of the first eight SKUs found one compliant image in twenty-seven.

The purpose of this module is to turn that from an anecdote into a number. "Most of the images
are too small" is not something you can plan a reshoot around; "1,842 SKUs need new photography,
and 214 already have compliant masters" is.

It is deliberately cheap: images are fetched with a **ranged GET** that pulls only the leading
bytes, because JPEG/PNG dimensions live in the header. Auditing several thousand images costs
kilobytes each rather than megabytes, which is the difference between a survey you run and one
you talk about running.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import structlog
from PIL import Image, UnidentifiedImageError

from anzorlist.media.pipeline import MIN_LONGEST_SIDE, RECOMMENDED_LONGEST_SIDE

log = structlog.get_logger(__name__)

# Enough of a JPEG/PNG to carry the dimension header in practice. Falls back to a full GET
# when a progressive or unusually-structured file needs more.
HEADER_BYTES = 65536
IMAGE_SUFFIXES = "abcdefg"


@dataclass
class ImageFact:
    sku: str
    slot: str
    url: str
    width: int = 0
    height: int = 0
    image_format: str = ""
    status: int = 0
    error: str = ""

    @property
    def exists(self) -> bool:
        return self.status == 200 and self.width > 0

    @property
    def longest(self) -> int:
        return max(self.width, self.height)

    @property
    def amazon_ready(self) -> bool:
        return self.exists and self.longest >= MIN_LONGEST_SIDE

    @property
    def verdict(self) -> str:
        if not self.exists:
            return "missing"
        if self.longest >= RECOMMENDED_LONGEST_SIDE:
            return "ready"
        if self.longest >= MIN_LONGEST_SIDE:
            return "minimum"
        return "too_small"


@dataclass
class SkuAudit:
    sku: str
    images: list[ImageFact] = field(default_factory=list)

    @property
    def main(self) -> ImageFact | None:
        return next((i for i in self.images if i.slot == "a" and i.exists), None)

    @property
    def listable(self) -> bool:
        """A SKU is listable only if its MAIN image clears the bar. Alternates are optional."""
        main = self.main
        return main is not None and main.amazon_ready

    @property
    def ready_count(self) -> int:
        return sum(1 for i in self.images if i.amazon_ready)

    @property
    def status(self) -> str:
        if self.main is None:
            return "no_main_image"
        return "listable" if self.listable else "main_too_small"


@dataclass
class AuditReport:
    skus: list[SkuAudit] = field(default_factory=list)

    @property
    def listable(self) -> list[SkuAudit]:
        return [s for s in self.skus if s.listable]

    @property
    def blocked(self) -> list[SkuAudit]:
        return [s for s in self.skus if not s.listable]

    def summary(self) -> dict[str, int]:
        images = [i for s in self.skus for i in s.images]
        by_verdict = Counter(i.verdict for i in images)
        return {
            "skus_audited": len(self.skus),
            "skus_listable": len(self.listable),
            "skus_blocked": len(self.blocked),
            "images_found": sum(1 for i in images if i.exists),
            "images_ready": by_verdict["ready"],
            "images_at_minimum": by_verdict["minimum"],
            "images_too_small": by_verdict["too_small"],
        }

    def size_histogram(self) -> dict[str, int]:
        """Where the images actually sit. Tells you whether a re-export can fix this or not."""
        buckets: Counter[str] = Counter()
        for image in (i for s in self.skus for i in s.images if i.exists):
            longest = image.longest
            if longest < 500:
                buckets["<500px"] += 1
            elif longest < 800:
                buckets["500-799px"] += 1
            elif longest < 1000:
                buckets["800-999px"] += 1
            elif longest < 1600:
                buckets["1000-1599px"] += 1
            else:
                buckets["1600px+"] += 1
        order = ["<500px", "500-799px", "800-999px", "1000-1599px", "1600px+"]
        return {k: buckets[k] for k in order if buckets[k]}

    def write_csv(self, path: Path | str) -> Path:
        """Per-SKU CSV — the worklist a photographer or an ops person can actually be handed."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                [
                    "sku",
                    "status",
                    "main_px",
                    "images_found",
                    "images_amazon_ready",
                    "needs_new_photography",
                    "main_url",
                ]
            )
            for audit in self.skus:
                main = audit.main
                writer.writerow(
                    [
                        audit.sku,
                        audit.status,
                        main.longest if main else 0,
                        sum(1 for i in audit.images if i.exists),
                        audit.ready_count,
                        "no" if audit.listable else "yes",
                        main.url if main else "",
                    ]
                )
        log.info("audit.csv_written", path=str(path), rows=len(self.skus))
        return path


class ImageAuditor:
    """Surveys image dimensions without downloading whole files."""

    IMAGE_BASE = "/jewelry/{sku_lower}{slot}.jpg"

    def __init__(self, base_url: str, user_agent: str, *, timeout: float = 20.0) -> None:
        self._base = base_url.rstrip("/")
        self._http = httpx.Client(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": user_agent}
        )

    def _url(self, sku: str, slot: str) -> str:
        return self._base + self.IMAGE_BASE.format(sku_lower=sku.lower(), slot=slot)

    def _probe(self, sku: str, slot: str) -> ImageFact:
        url = self._url(sku, slot)
        fact = ImageFact(sku=sku, slot=slot, url=url)
        try:
            resp = self._http.get(url, headers={"Range": f"bytes=0-{HEADER_BYTES - 1}"})
            fact.status = resp.status_code
            if resp.status_code not in (200, 206):
                return fact
            fact.status = 200
            data = resp.content
            try:
                with Image.open(io.BytesIO(data)) as img:
                    fact.width, fact.height = img.size
                    fact.image_format = img.format or ""
            except (UnidentifiedImageError, OSError):
                # Header not in the first chunk (progressive JPEG, odd chunking). Pay for the
                # whole file just for this one rather than reporting a false "missing".
                full = self._http.get(url)
                if full.status_code != 200:
                    fact.status = full.status_code
                    return fact
                with Image.open(io.BytesIO(full.content)) as img:
                    fact.width, fact.height = img.size
                    fact.image_format = img.format or ""
        except httpx.HTTPError as exc:
            fact.error = str(exc)[:120]
        except Exception as exc:  # noqa: BLE001 — one bad image must not stop a catalog audit
            fact.error = str(exc)[:120]
        return fact

    def audit_sku(self, sku: str, *, slots: str = IMAGE_SUFFIXES) -> SkuAudit:
        audit = SkuAudit(sku=sku)
        for slot in slots:
            fact = self._probe(sku, slot)
            if not fact.exists and slot != "a":
                # Slots are contiguous; the first gap after the main image ends the set.
                break
            audit.images.append(fact)
        return audit

    def audit(
        self, skus: list[str], *, slots: str = IMAGE_SUFFIXES, progress: object | None = None
    ) -> AuditReport:
        report = AuditReport()
        for i, sku in enumerate(skus, start=1):
            report.skus.append(self.audit_sku(sku, slots=slots))
            if progress is not None and hasattr(progress, "update"):
                progress.update(1)
            elif i % 50 == 0:
                log.info("audit.progress", done=i, of=len(skus))
        log.info("audit.done", **report.summary())
        return report

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ImageAuditor:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
