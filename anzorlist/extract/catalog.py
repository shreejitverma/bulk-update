"""Enumerate the store's catalog.

The listing pipeline is keyed on SKUs, and the operator should not have to type them. The legacy
store exposes a paginated category listing at ``prodList.asp?idCategory={id}&curPage={n}``,
20 products per page, which is enough to walk the whole catalog deterministically.

Two properties matter for a walk of this size (the rings category alone is ~94 pages):

* **It must terminate on its own.** Classic ASP paginators frequently clamp an out-of-range
  ``curPage`` back to the last valid page instead of returning an error, so "keep going until
  404" never stops. Termination here is driven by *seeing no new SKUs*, which is robust to
  clamping, to duplicate cross-listings, and to a page count that changes mid-walk.
* **It must be resumable and polite.** The walk runs through the throttled
  :class:`~anzorlist.extract.client.SiteClient`, and partial results are returned even when it
  is interrupted — a scan of a few thousand pages should never be all-or-nothing.

A scan discovers *identifiers only*. It deliberately does not fetch product pages: enumerating
the catalog is cheap and safe to re-run, while extracting every product is neither.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

import structlog

from anzorlist.extract.client import SiteClient

log = structlog.get_logger(__name__)

# The seven top-level departments linked from the homepage. Sub-categories are reachable too,
# but every product appears under a top-level department, so these cover the catalog without
# the duplication a full sub-category walk would produce.
TOP_LEVEL_CATEGORIES: dict[int, str] = {
    15: "Rings",
    16: "Earrings",
    17: "Necklaces",
    18: "Bracelets",
    19: "Pendants",
    20: "Sets",
    66: "Wedding Bands",
}

_SKU_RE = re.compile(r"prodview\.asp\?SKU=([A-Za-z0-9_-]+)", re.IGNORECASE)
_PAGE_COUNT_RE = re.compile(r"of\s+(\d+)\s*(?:&nbsp;|\s|<)", re.IGNORECASE)
_CATEGORY_RE = re.compile(r"idCategory=(\d+)")

# A page that yields nothing new this many times in a row ends the category. One empty page can
# be a transient blip or a gap in the listing; three in a row is the end.
EMPTY_PAGE_TOLERANCE = 3
MAX_PAGES_PER_CATEGORY = 500  # backstop against a paginator that never stops clamping


@dataclass
class CategoryScan:
    category_id: int
    name: str
    skus: list[str] = field(default_factory=list)
    pages_fetched: int = 0
    reported_pages: int | None = None
    truncated: bool = False

    @property
    def complete(self) -> bool:
        """Whether the walk covered every page the site said existed."""
        return self.reported_pages is None or self.pages_fetched >= self.reported_pages


@dataclass
class CatalogScan:
    categories: list[CategoryScan] = field(default_factory=list)

    @property
    def skus(self) -> list[str]:
        """Every SKU found, de-duplicated, in discovery order.

        Products are cross-listed across departments, so the same SKU legitimately appears more
        than once. The listing pipeline keys on seller SKU, so a duplicate would collide.
        """
        seen: set[str] = set()
        out: list[str] = []
        for cat in self.categories:
            for sku in cat.skus:
                if sku not in seen:
                    seen.add(sku)
                    out.append(sku)
        return out

    def category_of(self) -> dict[str, str]:
        """SKU -> the first department it was found in. Used to annotate the workbook."""
        out: dict[str, str] = {}
        for cat in self.categories:
            for sku in cat.skus:
                out.setdefault(sku, cat.name)
        return out

    def by_prefix(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for sku in self.skus:
            counts[sku[0].upper()] = counts.get(sku[0].upper(), 0) + 1
        return dict(sorted(counts.items()))

    @property
    def incomplete_categories(self) -> list[CategoryScan]:
        return [c for c in self.categories if not c.complete or c.truncated]


class CatalogScanner:
    """Walks category listings to discover SKUs. Read-only; safe to re-run."""

    def __init__(self, client: SiteClient) -> None:
        self._client = client

    def _page_url(self, category_id: int, page: int) -> str:
        base = f"{self._client.base_url}/Scripts/prodList.asp?idCategory={category_id}"
        return base if page <= 1 else f"{base}&curPage={page}"

    def scan_category(self, category_id: int, name: str = "") -> CategoryScan:
        """Walk one category to exhaustion."""
        scan = CategoryScan(
            category_id=category_id,
            name=name or TOP_LEVEL_CATEGORIES.get(category_id, str(category_id)),
        )
        seen: set[str] = set()
        empty_streak = 0

        for page in range(1, MAX_PAGES_PER_CATEGORY + 1):
            try:
                resp = self._client._get(self._page_url(category_id, page))
            except Exception as exc:  # noqa: BLE001 — return what we have rather than nothing
                log.warning("catalog.page_failed", category=category_id, page=page, error=str(exc))
                scan.truncated = True
                break

            html, _ = self._client._decode(resp.content)
            if scan.reported_pages is None:
                m = _PAGE_COUNT_RE.search(html)
                if m:
                    scan.reported_pages = int(m.group(1))
            scan.pages_fetched = page

            found = [s.upper() for s in _SKU_RE.findall(html)]
            fresh = [s for s in dict.fromkeys(found) if s not in seen]
            seen.update(fresh)
            scan.skus.extend(fresh)

            if fresh:
                empty_streak = 0
            else:
                # Either the end of the listing, or the paginator clamped us back onto a page
                # we have already read. Both mean stop.
                empty_streak += 1
                if empty_streak >= EMPTY_PAGE_TOLERANCE:
                    break

            if scan.reported_pages is not None and page >= scan.reported_pages:
                break
        else:
            scan.truncated = True
            log.warning("catalog.page_cap_hit", category=category_id, cap=MAX_PAGES_PER_CATEGORY)

        log.info(
            "catalog.category_done",
            category=category_id,
            name=scan.name,
            skus=len(scan.skus),
            pages=scan.pages_fetched,
            reported_pages=scan.reported_pages,
        )
        return scan

    def scan(self, category_ids: list[int] | None = None) -> CatalogScan:
        """Walk every requested category. Defaults to the seven top-level departments."""
        ids = category_ids if category_ids is not None else list(TOP_LEVEL_CATEGORIES)
        result = CatalogScan()
        for cid in ids:
            result.categories.append(self.scan_category(cid))
        log.info(
            "catalog.scan_done",
            categories=len(result.categories),
            skus=len(result.skus),
            by_prefix=result.by_prefix(),
        )
        return result

    def discover_categories(self) -> dict[int, str]:
        """Read the category IDs actually linked from the homepage.

        The built-in table is a convenience, not an authority — if the store adds a department,
        this finds it without a code change.
        """
        try:
            resp = self._client._get(self._client.base_url + "/")
            html, _ = self._client._decode(resp.content)
        except Exception as exc:  # noqa: BLE001
            log.warning("catalog.discover_failed", error=str(exc))
            return dict(TOP_LEVEL_CATEGORIES)
        found = {int(c) for c in _CATEGORY_RE.findall(html)}
        merged = {cid: TOP_LEVEL_CATEGORIES.get(cid, f"Category {cid}") for cid in sorted(found)}
        log.info("catalog.discovered", count=len(merged), ids=sorted(merged))
        return merged or dict(TOP_LEVEL_CATEGORIES)

    def iter_skus(self, category_ids: list[int] | None = None) -> Iterator[str]:
        """Stream SKUs as they are found, for callers that want to start work immediately."""
        seen: set[str] = set()
        for cid in category_ids if category_ids is not None else list(TOP_LEVEL_CATEGORIES):
            for sku in self.scan_category(cid).skus:
                if sku not in seen:
                    seen.add(sku)
                    yield sku
