"""Map an extracted :class:`Product` into an Etsy listing.

Title, tags and materials are composed from extracted facts, like eBay's title, so no qualifier
can be lost to a length limit: Etsy titles are capped at 140 characters, a listing takes at most
13 tags of at most 20 characters, and materials may contain only letters, numbers and spaces.
A title may use each of ``%``, ``:``, ``&`` and ``+`` only once, so the generated title names
several stones as "Diamond, Ruby & Sapphire", and a workbook title that breaks the rule is held.
The description is the validated copy as plain text; Etsy does not render HTML.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from anzorlist.channels.amazon.mapper import AmazonMapper, size_label
from anzorlist.channels.ebay.mapper import AXIS_ASPECT, FAMILY_TYPE, ebay_title
from anzorlist.channels.etsy.models import IMAGE_TYPES, EtsyListing, EtsyVariation
from anzorlist.config import Settings
from anzorlist.generate.copy import ListingCopy
from anzorlist.ingest.row import ListingRow
from anzorlist.models.listing import ListingIssue
from anzorlist.models.product import Product
from anzorlist.pricing import PriceQuote, quantize

MAX_TITLE = 140
MAX_TAGS = 13
MAX_TAG_LEN = 20
TITLE_ONCE = "%:&+"
_MATERIAL_UNSAFE = re.compile(r"[^A-Za-z0-9 ]+")
_TAG_UNSAFE = re.compile(r"[^A-Za-z0-9 '-]+")


class EtsyMapper:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def build(
        self,
        *,
        product: Product,
        row: ListingRow,
        copy: ListingCopy,
        quote: PriceQuote,
        image_files: list[str],
    ) -> EtsyListing:
        a = product.attributes
        issues: list[ListingIssue] = []
        title = row.item_name_override or ebay_title(product, self.settings.brand_name, MAX_TITLE)
        if len(title) > MAX_TITLE:
            title = ebay_title(product, self.settings.brand_name, MAX_TITLE)
        repeated = [c for c in TITLE_ONCE if title.count(c) > 1]
        if repeated:
            issues.append(
                ListingIssue(
                    code="EtsyTitleRule",
                    message=f"Etsy allows each of {', '.join(repeated)} once in a title: {title!r}",
                )
            )

        stones: list[str] = []
        for g in a.gemstones:
            if g.type not in stones:
                stones.append(g.type)
        if row.gem_type:
            stones = [] if row.gem_type == "none" else [row.gem_type.title()]

        metal = " ".join(x for x in (a.metal_purity, a.metal_color, a.metal_type) if x)
        materials = [_MATERIAL_UNSAFE.sub(" ", m).strip() for m in [metal, *stones] if m]
        who_made = self.settings.etsy_who_made
        tags = _tags(
            [
                FAMILY_TYPE[product.family],
                f"{a.metal_purity} {a.metal_type}" if a.metal_purity and a.metal_type else "",
                a.metal_type or "",
                *[f"{s} {FAMILY_TYPE[product.family]}" for s in stones],
                *stones,
                row.style or "",
                "fine jewelry",
                "handmade jewelry" if who_made == "i_did" else "",
            ]
        )

        quantity = row.quantity if row.quantity is not None else self.settings.default_quantity
        sizes = [v for v in product.size_options if v.axis in AXIS_ASPECT]
        variations: list[EtsyVariation] = []
        prop: str | None = None
        if row.variation_source != "none" and len(sizes) >= 2:
            prop = "Ring size" if sizes[0].axis == "ring_size" else "Length"
            variations = [
                EtsyVariation(
                    sku=AmazonMapper.child_sku(product.sku, v),
                    value=size_label(v),
                    price=quantize(quote.price + v.price_delta.amount, quote.currency),
                )
                for v in sizes
            ]

        if not image_files:
            issues.append(
                ListingIssue(code="NoMainImage", message="no checked image file; Etsy needs one")
            )
        image_files = image_files[:10]  # Etsy allows 10 images per listing
        unsupported = [f for f in image_files if Path(f).suffix.lower() not in IMAGE_TYPES]
        if unsupported:
            issues.append(
                ListingIssue(
                    code="EtsyImageFormat",
                    message="Etsy accepts JPEG, PNG and GIF photos only; convert "
                    + ", ".join(Path(f).name for f in unsupported),
                )
            )
        description = "\n\n".join(
            [
                row.description_override or copy.description,
                *[f"- {b}" for b in (row.bullets or copy.bullets)],
            ]
        )
        listing = EtsyListing(
            source_sku=product.sku,
            family=FAMILY_TYPE[product.family],
            title=title,
            description=description,
            who_made=who_made,
            price=min([quote.price, *[v.price for v in variations]]),
            currency=quote.currency,
            quantity=quantity,
            tags=tags,
            materials=materials,
            image_files=image_files,
            variation_property=prop,
            variations=variations,
            issues=issues,
            source_url=product.source_url,
            built_at=datetime.now(timezone.utc),
        )
        listing.payload_hash = listing.compute_payload_hash()
        return listing


def _tags(candidates: list[str]) -> list[str]:
    out: list[str] = []
    for raw in candidates:
        tag = _TAG_UNSAFE.sub(" ", raw).strip().lower()
        tag = re.sub(r"\s+", " ", tag)
        if tag and len(tag) <= MAX_TAG_LEN and tag not in out:
            out.append(tag)
    return out[:MAX_TAGS]
