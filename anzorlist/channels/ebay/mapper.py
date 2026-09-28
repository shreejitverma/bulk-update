"""Map an extracted :class:`Product` into an eBay listing: one inventory item, or a size group.

eBay's Inventory API models a listing as *inventory items* (one per SKU, carrying the product
data) plus one *offer* per item (price, category, policies). A ring with sizes becomes an
*inventory item group*: one listing page whose variations are the member items, each with its own
price and a ``Ring Size`` aspect.

Two eBay constraints shape the copy:

* **Titles are capped at 80 characters.** The Amazon title is up to 200, and shortening it by
  cutting words could drop a qualifier ("Lab-Grown", "Plated") - exactly the FTC problem the copy
  validator exists to prevent. So the eBay title is composed from extracted facts only (purity,
  colour, metal, every stone, item type), with the brand dropped first when space runs out.
* **Item specifics (aspects) drive search filters.** Required aspects vary by category and are
  checked against eBay's Taxonomy API once cached; here each aspect is emitted only from a
  value the source page actually states.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from decimal import Decimal

from anzorlist.channels.amazon.mapper import AmazonMapper, size_label
from anzorlist.channels.ebay.models import EbayItem, EbayListing
from anzorlist.config import Settings
from anzorlist.generate.copy import ListingCopy
from anzorlist.ingest.row import ListingRow
from anzorlist.models.listing import IssueSeverity, ListingIssue
from anzorlist.models.product import Product, ProductFamily, Variation
from anzorlist.pricing import PriceQuote, quantize

# eBay US leaf categories for fine jewelry, as used on ebay.com's own browse pages
# (ebay.com/b/Fine-Rings/261994, Fine-Earrings/261990, Fine-Necklaces-Pendants/261993,
# Fine-Bracelets/261988, Fine-Jewelry-Sets/261992). `anzorlist ebay sync-aspects` confirms each
# against the Taxonomy API and caches its required item specifics.
FAMILY_CATEGORY: dict[ProductFamily, str] = {
    ProductFamily.RING: "261994",
    ProductFamily.EARRINGS: "261990",
    ProductFamily.PENDANT: "261993",
    ProductFamily.NECKLACE: "261993",
    ProductFamily.BRACELET: "261988",
    ProductFamily.SET: "261992",
}

FAMILY_TYPE: dict[ProductFamily, str] = {
    ProductFamily.RING: "Ring",
    ProductFamily.EARRINGS: "Earrings",
    ProductFamily.PENDANT: "Pendant",
    ProductFamily.NECKLACE: "Necklace",
    ProductFamily.BRACELET: "Bracelet",
    ProductFamily.SET: "Jewelry Set",
}

GENDER_DEPARTMENT = {"female": "Women", "male": "Men", "unisex": "Unisex Adult"}
AXIS_ASPECT = {
    "ring_size": "Ring Size",
    "chain_length": "Length",
    "bracelet_length": "Length",
    "length": "Length",
}

MAX_TITLE = 80
MAX_IMAGES = 24  # eBay accepts up to 24 pictures per listing


class EbayMapper:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def build(
        self,
        *,
        product: Product,
        row: ListingRow,
        copy: ListingCopy,
        quote: PriceQuote,
        image_urls: list[str],
    ) -> EbayListing:
        issues: list[ListingIssue] = []
        title = ebay_title(product, self.settings.brand_name)
        if row.item_name_override:
            if len(row.item_name_override) <= MAX_TITLE:
                title = row.item_name_override
            else:
                issues.append(
                    ListingIssue(
                        code="TitleOverrideTooLong",
                        message=f"the workbook title is {len(row.item_name_override)} characters; "
                        f"eBay allows {MAX_TITLE}. The generated title was used instead.",
                        severity=IssueSeverity.WARNING,
                    )
                )

        aspects = self._aspects(product, row)
        quantity = row.quantity if row.quantity is not None else self.settings.default_quantity
        sizes = [v for v in product.size_options if v.axis in AXIS_ASPECT]
        use_group = row.variation_source != "none" and len(sizes) >= 2

        if use_group:
            aspect_name = AXIS_ASPECT[sizes[0].axis]
            items = [
                EbayItem(
                    sku=AmazonMapper.child_sku(product.sku, v),
                    title=title,
                    aspects={**aspects, aspect_name: [size_label(v)]},
                    image_urls=image_urls[:MAX_IMAGES],
                    quantity=quantity,
                    price=_with_delta(quote, v),
                )
                for v in sizes
            ]
            varies_by: dict[str, list[str]] | None = {aspect_name: [size_label(v) for v in sizes]}
            group_key: str | None = f"{product.sku}-GROUP"
        else:
            items = [
                EbayItem(
                    sku=product.sku,
                    title=title,
                    aspects=aspects,
                    image_urls=image_urls[:MAX_IMAGES],
                    quantity=quantity,
                    price=quote.price,
                )
            ]
            varies_by, group_key = None, None

        if not image_urls:
            issues.append(
                ListingIssue(
                    code="NoMainImage",
                    message="no hosted image URL; eBay requires at least one picture",
                )
            )
        if row.condition != "new_new":
            issues.append(
                ListingIssue(
                    code="ConditionUnsupported",
                    message=f"condition {row.condition!r} is not mapped for eBay fine jewelry; "
                    f"only new items are listed",
                )
            )

        listing = EbayListing(
            source_sku=product.sku,
            marketplace_id=self.settings.ebay_marketplace_id,
            category_id=FAMILY_CATEGORY[product.family],
            currency=quote.currency,
            title=title,
            description=description_html(
                row.description_override or copy.description, row.bullets or copy.bullets
            ),
            aspects=aspects,
            image_urls=image_urls[:MAX_IMAGES],
            group_key=group_key,
            varies_by=varies_by,
            items=items,
            issues=issues,
            source_url=product.source_url,
            built_at=datetime.now(timezone.utc),
        )
        listing.payload_hash = listing.compute_payload_hash()
        return listing

    def _aspects(self, product: Product, row: ListingRow) -> dict[str, list[str]]:
        """Item specifics, each only from a stated value. eBay aspect values are lists."""
        a = product.attributes
        aspects: dict[str, list[str]] = {
            "Brand": [self.settings.brand_name],
            "Type": [FAMILY_TYPE[product.family]],
            "Country of Origin": [_country_name(self.settings.country_of_origin)],
        }
        metal = _metal_aspect(row.metal_type, a.metal_type, a.metal_color)
        if metal:
            aspects["Metal"] = [metal]
        purity = row.metal_stamp or a.metal_purity
        if purity:
            aspects["Metal Purity"] = [purity]
        stones: list[str] = []
        if row.gem_type:
            stones = [] if row.gem_type == "none" else [row.gem_type.title()]
        else:
            for g in a.gemstones:
                if g.type.title() not in stones:
                    stones.append(g.type.title())
        if stones:
            aspects["Main Stone"] = [stones[0]]
            if len(stones) > 1:
                aspects["Secondary Stone"] = stones[1:]
        else:
            aspects["Main Stone"] = ["No Stone"]
        weights = [g.carat_weight for g in a.gemstones]
        total = row.total_gem_weight_ct
        if total is None and weights and all(w is not None for w in weights) and not row.gem_type:
            total = sum((w for w in weights if w is not None), Decimal(0))
        if total and row.gem_type != "none":
            aspects["Total Carat Weight (TCW)"] = [f"{total.normalize():f} ctw"]
        gender = row.target_gender or "unisex"
        aspects["Department"] = [GENDER_DEPARTMENT[gender]]
        if row.style:
            aspects["Style"] = [row.style]
        return aspects


def ebay_title(product: Product, brand: str) -> str:
    """Purity, colour, metal, every stone, item type - then the brand in front if it fits."""
    a = product.attributes
    stones: list[str] = []
    for g in a.gemstones:
        if g.type not in stones:
            stones.append(g.type)
    colour = a.metal_color if a.metal_color and not a.metal_color.startswith("Two-Tone") else ""
    if a.metal_color and a.metal_color.startswith("Two-Tone"):
        colour = "Two-Tone"
    core = " ".join(
        x
        for x in (
            a.metal_purity,
            colour,
            a.metal_type,
            " & ".join(stones),
            FAMILY_TYPE[product.family],
        )
        if x
    )
    with_brand = f"{brand} {core}"
    if len(with_brand) <= MAX_TITLE:
        return with_brand
    if len(core) <= MAX_TITLE:
        return core
    # Still too long: drop secondary stones, never the qualifiers of the ones named.
    fallback = " ".join(
        x
        for x in (
            a.metal_purity,
            colour,
            a.metal_type,
            stones[0] if stones else "",
            FAMILY_TYPE[product.family],
        )
        if x
    )
    return fallback[:MAX_TITLE]


def _country_name(code: str) -> str:
    """eBay's Country of Origin aspect takes a name, not an ISO code."""
    return {"US": "United States"}.get(code.upper(), code)


def description_html(description: str, bullets: list[str]) -> str:
    """eBay renders HTML descriptions. Everything is escaped; only our own tags are emitted."""
    parts = [f"<p>{html.escape(p.strip())}</p>" for p in description.split("\n") if p.strip()]
    if bullets:
        items = "".join(f"<li>{html.escape(b)}</li>" for b in bullets)
        parts.append(f"<ul>{items}</ul>")
    return "".join(parts)


def _metal_aspect(override: str | None, metal: str | None, colour: str | None) -> str | None:
    if override:
        return override.replace("_", " ").title()
    if metal is None:
        return None
    if colour and colour.startswith("Two-Tone"):
        return f"Two-Tone {metal}"
    return f"{colour.split()[0]} {metal}" if colour else metal


def _with_delta(quote: PriceQuote, variation: Variation) -> Decimal:
    return quantize(quote.price + variation.price_delta.amount, quote.currency)
