"""Map an extracted :class:`Product` plus a spreadsheet row into Amazon listing attributes.

Amazon's attribute format is uniform and unusual: **every** attribute is a *list of objects*,
each scoped to a marketplace, even when only one value is possible. ``brand`` is
``[{"value": "Anzor", "marketplace_id": "ATVPDKIKX0DER"}]``, never ``"Anzor"``. Getting this
wrong is the single most common cause of a rejected listing, so all construction goes through
:func:`attr` rather than being hand-written per field.

Precedence is fixed and enforced in one place, :meth:`_Resolver.pick`:

    spreadsheet override  >  extracted site value  >  configured default  >  omit

Omission is deliberate. An attribute Amazon does not require and we cannot substantiate is left
out entirely — a wrong value is worse than a missing one, because a missing required attribute
comes back as a clear validation error while a wrong one becomes a live, incorrect listing.

Variation families are built here too. Ring sizes are a real parent/child family with a ``SIZE``
theme, not eight standalone listings: one detail page, one set of reviews, one Buy Box.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import structlog

from anzorlist.config import Settings
from anzorlist.generate.copy import ListingCopy
from anzorlist.ingest.row import ListingRow
from anzorlist.marketplaces import Marketplace
from anzorlist.models.listing import BuiltListing, ListingIssue, ListingStatus, OfferTerms
from anzorlist.models.product import Product, ProductFamily, Variation
from anzorlist.pricing import PriceQuote

log = structlog.get_logger(__name__)

# SKU-prefix family -> Amazon product type. These are the *defaults* used when the spreadsheet
# leaves the column blank. Amazon owns this vocabulary, revises it, and varies it per
# marketplace, so `anzorlist amazon product-types --refresh` replaces the mapping with the
# authoritative set from the Product Type Definitions API once credentials exist.
FAMILY_PRODUCT_TYPE: dict[ProductFamily, str] = {
    ProductFamily.RING: "RING",
    ProductFamily.EARRINGS: "EARRING",
    ProductFamily.PENDANT: "NECKLACE",  # Amazon files pendants under necklaces in most locales
    ProductFamily.BRACELET: "BRACELET",
    ProductFamily.NECKLACE: "NECKLACE",
    ProductFamily.SET: "JEWELRY_SET",
}

FAMILY_ITEM_TYPE_KEYWORD: dict[ProductFamily, str] = {
    ProductFamily.RING: "rings",
    ProductFamily.EARRINGS: "earrings",
    ProductFamily.PENDANT: "pendant-necklaces",
    ProductFamily.BRACELET: "bracelets",
    ProductFamily.NECKLACE: "necklaces",
    ProductFamily.SET: "jewelry-sets",
}

# Variation theme by sizing axis. Amazon rejects an unknown theme outright.
AXIS_VARIATION_THEME: dict[str, str] = {
    "ring_size": "SIZE",
    "chain_length": "SIZE",
    "bracelet_length": "SIZE",
    "length": "SIZE",
}

GENDER_DEPARTMENT: dict[str, str] = {
    "female": "womens",
    "male": "mens",
    "unisex": "unisex-adult",
}

METAL_TYPE_NORMALISED: dict[tuple[str | None, str | None], str] = {
    ("Gold", "Yellow"): "yellow_gold",
    ("Gold", "White"): "white_gold",
    ("Gold", "Rose"): "rose_gold",
    ("Platinum", None): "platinum",
}

MAX_ALTERNATE_IMAGES = 8  # Amazon accepts other_product_image_locator_1 .. _8


# ---------------------------------------------------------------------------- attribute shapes


def attr(value: Any, marketplace: Marketplace, **extra: Any) -> list[dict[str, Any]]:
    """Wrap a scalar in Amazon's marketplace-scoped attribute envelope."""
    entry: dict[str, Any] = {"value": value, "marketplace_id": marketplace.marketplace_id}
    entry.update(extra)
    return [entry]


def localized(value: str, marketplace: Marketplace) -> list[dict[str, Any]]:
    """A customer-facing text attribute. ``language_tag`` is required on these and its absence
    is reported as a confusing 'invalid attribute' rather than a missing-field error."""
    return [
        {
            "value": value,
            "language_tag": marketplace.locale,
            "marketplace_id": marketplace.marketplace_id,
        }
    ]


def multi_localized(values: list[str], marketplace: Marketplace) -> list[dict[str, Any]]:
    """A repeated text attribute such as ``bullet_point``. Order is preserved and meaningful."""
    return [
        {
            "value": v,
            "language_tag": marketplace.locale,
            "marketplace_id": marketplace.marketplace_id,
        }
        for v in values
    ]


def measure(value: Decimal | float, unit: str, marketplace: Marketplace) -> list[dict[str, Any]]:
    """A dimensional attribute. Amazon requires the unit alongside the magnitude — a bare number
    is rejected, and guessing the unit is how carat weights become gram weights."""
    return [
        {
            "value": float(value),
            "unit": unit,
            "marketplace_id": marketplace.marketplace_id,
        }
    ]


# ---------------------------------------------------------------------------- resolver


@dataclass
class _Resolver:
    """Applies the override > extracted > default > omit precedence, and records what it used."""

    row: ListingRow
    product: Product
    settings: Settings
    provenance: dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.provenance is None:
            self.provenance = {}

    def pick(self, key: str, override: Any, extracted: Any, default: Any = None) -> Any:
        """Return the winning value, or ``None`` to omit the attribute entirely."""
        if override is not None and override != "":
            self.provenance[key] = "spreadsheet"
            return override
        if extracted is not None and extracted != "":
            self.provenance[key] = "website"
            return extracted
        if default is not None and default != "":
            self.provenance[key] = "default"
            return default
        self.provenance[key] = "omitted"
        return None


# ---------------------------------------------------------------------------- the mapper


class AmazonMapper:
    """Builds :class:`BuiltListing` objects for one product in one marketplace."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # -- product type --

    def product_type(self, product: Product, row: ListingRow) -> str:
        if row.amazon_product_type:
            return row.amazon_product_type
        return FAMILY_PRODUCT_TYPE.get(product.family, "JEWELRY_SET")

    # -- entry point --

    def build(
        self,
        *,
        product: Product,
        row: ListingRow,
        marketplace: Marketplace,
        copy: ListingCopy,
        quote: PriceQuote,
        image_urls: list[str],
    ) -> list[BuiltListing]:
        """Build every listing for this SKU: one standalone, or a parent plus one child per size.

        Returns the parent first when a family is produced, because Amazon requires the parent to
        exist before its children reference it.
        """
        variations = self._variations(product, row)
        if variations:
            return self._build_family(
                product=product,
                row=row,
                marketplace=marketplace,
                copy=copy,
                quote=quote,
                image_urls=image_urls,
                variations=variations,
            )
        return [
            self._build_standalone(
                product=product,
                row=row,
                marketplace=marketplace,
                copy=copy,
                quote=quote,
                image_urls=image_urls,
            )
        ]

    # -- variation selection --

    def _variations(self, product: Product, row: ListingRow) -> list[Variation]:
        """Which size options become child listings.

        A single option is not a variation family — Amazon rejects a family with one child, and
        a one-size product is genuinely a standalone listing.
        """
        if row.variation_source == "none":
            return []
        sizing = [v for v in product.size_options if v.axis in AXIS_VARIATION_THEME]
        if len(sizing) < 2:
            return []

        wanted = row.ring_sizes()
        if wanted:
            wanted_norm = {w.strip().lower() for w in wanted}
            filtered = [
                v
                for v in sizing
                if v.label.strip().lower() in wanted_norm
                or (v.value is not None and str(v.value.normalize()) in wanted_norm)
            ]
            if len(filtered) >= 2:
                return filtered
            log.warning(
                "mapper.size_override_ignored",
                sku=product.sku,
                requested=wanted,
                matched=len(filtered),
                reason="fewer than 2 sizes matched; using all site sizes",
            )
        return sizing

    # -- standalone --

    def _build_standalone(
        self,
        *,
        product: Product,
        row: ListingRow,
        marketplace: Marketplace,
        copy: ListingCopy,
        quote: PriceQuote,
        image_urls: list[str],
    ) -> BuiltListing:
        resolver = _Resolver(row=row, product=product, settings=self.settings)
        attributes = self._common_attributes(
            product=product,
            row=row,
            marketplace=marketplace,
            copy=copy,
            image_urls=image_urls,
            resolver=resolver,
        )
        attributes.update(self._offer_attributes(row, marketplace, quote))
        return self._finalize(
            sku=product.sku,
            product=product,
            row=row,
            marketplace=marketplace,
            attributes=attributes,
            quote=quote,
            resolver=resolver,
        )

    # -- variation family --

    def _build_family(
        self,
        *,
        product: Product,
        row: ListingRow,
        marketplace: Marketplace,
        copy: ListingCopy,
        quote: PriceQuote,
        image_urls: list[str],
        variations: list[Variation],
    ) -> list[BuiltListing]:
        theme = AXIS_VARIATION_THEME[variations[0].axis]
        parent_sku = f"{product.sku}-PARENT"
        child_skus = [self._child_sku(product.sku, v) for v in variations]

        # -- parent: product data only. A parent is a browsable container, never buyable, so it
        # carries no purchasable_offer and no fulfillment_availability. Sending either makes
        # Amazon treat it as a standalone item and the family silently fails to group.
        parent_resolver = _Resolver(row=row, product=product, settings=self.settings)
        parent_attrs = self._common_attributes(
            product=product,
            row=row,
            marketplace=marketplace,
            copy=copy,
            image_urls=image_urls,
            resolver=parent_resolver,
        )
        parent_attrs["parentage_level"] = attr("parent", marketplace)
        parent_attrs["variation_theme"] = [
            {
                "name": theme,
                "marketplace_id": marketplace.marketplace_id,
            }
        ]
        parent = self._finalize(
            sku=parent_sku,
            product=product,
            row=row,
            marketplace=marketplace,
            attributes=parent_attrs,
            quote=None,
            resolver=parent_resolver,
            is_parent=True,
            variation_theme=theme,
            child_skus=child_skus,
        )

        listings = [parent]
        for variation, child_sku in zip(variations, child_skus, strict=True):
            child_resolver = _Resolver(row=row, product=product, settings=self.settings)
            child_attrs = self._common_attributes(
                product=product,
                row=row,
                marketplace=marketplace,
                copy=copy,
                image_urls=image_urls,
                resolver=child_resolver,
            )
            child_attrs["parentage_level"] = attr("child", marketplace)
            child_attrs["child_parent_sku_relationship"] = [
                {
                    "child_relationship_type": "variation",
                    "parent_sku": parent_sku,
                    "marketplace_id": marketplace.marketplace_id,
                }
            ]
            child_attrs["variation_theme"] = [
                {
                    "name": theme,
                    "marketplace_id": marketplace.marketplace_id,
                }
            ]
            child_attrs.update(self._size_attributes(variation, product, marketplace))

            # Each size carries its own price delta from the site, so the child price is the
            # parent's gross-up plus that delta — never the parent price repeated.
            child_quote = self._quote_with_delta(quote, variation)
            child_attrs.update(self._offer_attributes(row, marketplace, child_quote))

            listings.append(
                self._finalize(
                    sku=child_sku,
                    product=product,
                    row=row,
                    marketplace=marketplace,
                    attributes=child_attrs,
                    quote=child_quote,
                    resolver=child_resolver,
                    parent_sku=parent_sku,
                    variation_theme=theme,
                )
            )

        log.info(
            "mapper.family_built",
            sku=product.sku,
            marketplace=marketplace.code,
            theme=theme,
            children=len(child_skus),
        )
        return listings

    @staticmethod
    def _child_sku(base_sku: str, variation: Variation) -> str:
        """A stable, human-readable child SKU. Stability matters: Amazon keys the listing on it,
        so a SKU that changes between runs creates a duplicate listing instead of updating one."""
        token = variation.label
        if variation.value is not None:
            token = format(variation.value.normalize(), "f")
        safe = "".join(c if c.isalnum() or c in "-." else "-" for c in token).strip("-")
        return f"{base_sku}-{safe or (variation.option_value_id or 'V')}"

    @staticmethod
    def _quote_with_delta(quote: PriceQuote, variation: Variation) -> PriceQuote:
        delta = variation.price_delta.amount
        if not delta:
            return quote
        from anzorlist.pricing import quantize

        return PriceQuote(
            marketplace=quote.marketplace,
            currency=quote.currency,
            web_price=quote.web_price,
            price=quantize(quote.price + delta, quote.currency),
            list_price=(
                quantize(quote.list_price + delta, quote.currency)
                if quote.list_price is not None
                else None
            ),
            fee_fraction=quote.fee_fraction,
            estimated_referral_fee=quote.estimated_referral_fee,
            estimated_net=quote.estimated_net,
            source=quote.source,
        )

    def _size_attributes(
        self, variation: Variation, product: Product, marketplace: Marketplace
    ) -> dict[str, Any]:
        """The attribute that actually distinguishes one child from another."""
        out: dict[str, Any] = {"size": localized(variation.label, marketplace)}
        if variation.axis == "ring_size" and variation.value is not None:
            out["ring_size"] = localized(format(variation.value.normalize(), "f"), marketplace)
        elif variation.value is not None and variation.unit:
            out["item_length_description"] = localized(
                f"{format(variation.value.normalize(), 'f')} {variation.unit}", marketplace
            )
        return out

    # -- shared attribute construction --

    def _common_attributes(
        self,
        *,
        product: Product,
        row: ListingRow,
        marketplace: Marketplace,
        copy: ListingCopy,
        image_urls: list[str],
        resolver: _Resolver,
    ) -> dict[str, Any]:
        a = product.attributes
        attributes: dict[str, Any] = {}

        # ---- identity ----
        attributes["item_name"] = localized(
            resolver.pick("item_name", row.item_name_override, copy.title), marketplace
        )
        attributes["brand"] = attr(self.settings.brand_name, marketplace)
        attributes["manufacturer"] = attr(self.settings.manufacturer, marketplace)
        attributes["supplier_declared_dg_hz_regulation"] = attr("not_applicable", marketplace)
        attributes["country_of_origin"] = attr(self.settings.country_of_origin, marketplace)
        attributes["batteries_required"] = attr(False, marketplace)
        attributes["number_of_items"] = attr(1, marketplace)

        # ---- product identifier: GTIN or exemption ----
        if row.upc_ean:
            attributes["externally_assigned_product_identifier"] = [
                {
                    "type": _gtin_type(row.upc_ean),
                    "value": row.upc_ean,
                    "marketplace_id": marketplace.marketplace_id,
                }
            ]
        else:
            # The GTIN-exemption path. Amazon must have already approved the exemption for this
            # brand and category; the flag asserts it, it does not grant it.
            attributes["supplier_declared_has_product_identifier_exemption"] = attr(
                True, marketplace
            )

        # ---- copy ----
        bullets = row.bullets or copy.bullets
        if bullets:
            attributes["bullet_point"] = multi_localized(bullets[:5], marketplace)
        description = resolver.pick(
            "product_description", row.description_override, copy.description
        )
        if description:
            attributes["product_description"] = localized(description, marketplace)
        search_terms = resolver.pick("generic_keyword", row.search_terms, copy.search_terms)
        if search_terms:
            attributes["generic_keyword"] = localized(search_terms, marketplace)

        # ---- materials ----
        metal_type = resolver.pick(
            "metal_type", row.metal_type, _normalise_metal(a.metal_type, a.metal_color)
        )
        if metal_type:
            attributes["metal_type"] = attr(metal_type, marketplace)
            attributes["material"] = localized(metal_type.replace("_", " ").title(), marketplace)

        metal_stamp = resolver.pick("metal_stamp", row.metal_stamp, a.metal_purity)
        if metal_stamp:
            attributes["metal_stamp"] = attr(metal_stamp, marketplace)

        gem = resolver.pick(
            "gem_type",
            row.gem_type,
            a.gemstones[0].type.lower() if a.gemstones else None,
        )
        if gem and gem != "none":
            attributes["gem_type"] = attr(gem, marketplace)

        carat = resolver.pick(
            "total_gem_weight",
            row.total_gem_weight_ct,
            a.gemstones[0].carat_weight if a.gemstones else None,
        )
        if carat:
            attributes["total_gem_weight"] = measure(carat, "carats", marketplace)

        metal_weight = resolver.pick(
            "total_metal_weight", row.total_metal_weight_g, a.gross_weight_g
        )
        if metal_weight:
            attributes["total_metal_weight"] = measure(metal_weight, "grams", marketplace)

        # ---- merchandising ----
        gender = resolver.pick("target_gender", row.target_gender, None, "unisex")
        attributes["target_gender"] = attr(gender, marketplace)
        attributes["department"] = localized(GENDER_DEPARTMENT[gender], marketplace)
        keyword = FAMILY_ITEM_TYPE_KEYWORD.get(product.family)
        if keyword:
            attributes["item_type_keyword"] = attr(keyword, marketplace)
        if row.style:
            attributes["style"] = localized(row.style, marketplace)

        # ---- media ----
        if image_urls:
            attributes["main_product_image_locator"] = [
                {
                    "media_location": image_urls[0],
                    "marketplace_id": marketplace.marketplace_id,
                }
            ]
            for i, url in enumerate(image_urls[1 : MAX_ALTERNATE_IMAGES + 1], start=1):
                attributes[f"other_product_image_locator_{i}"] = [
                    {
                        "media_location": url,
                        "marketplace_id": marketplace.marketplace_id,
                    }
                ]

        return attributes

    def _offer_attributes(
        self, row: ListingRow, marketplace: Marketplace, quote: PriceQuote
    ) -> dict[str, Any]:
        """Price, condition, and availability — the buyable half of a listing."""
        quantity = row.quantity if row.quantity is not None else self.settings.default_quantity
        handling = (
            row.handling_time_days
            if row.handling_time_days is not None
            else self.settings.handling_time_days
        )

        out: dict[str, Any] = {
            "condition_type": attr(row.condition, marketplace),
            "purchasable_offer": [
                {
                    "currency": quote.currency,
                    "marketplace_id": marketplace.marketplace_id,
                    "audience": "ALL",
                    "our_price": [{"schedule": [{"value_with_tax": float(quote.price)}]}],
                }
            ],
            "fulfillment_availability": [
                {
                    "fulfillment_channel_code": "DEFAULT",
                    "quantity": quantity,
                    "lead_time_to_ship_max_days": handling,
                    "marketplace_id": marketplace.marketplace_id,
                }
            ],
        }
        if quote.list_price is not None:
            out["list_price"] = [
                {
                    "value": float(quote.list_price),
                    "currency": quote.currency,
                    "marketplace_id": marketplace.marketplace_id,
                }
            ]
        return out

    # -- finalisation --

    def _finalize(
        self,
        *,
        sku: str,
        product: Product,
        row: ListingRow,
        marketplace: Marketplace,
        attributes: dict[str, Any],
        quote: PriceQuote | None,
        resolver: _Resolver,
        is_parent: bool = False,
        parent_sku: str | None = None,
        variation_theme: str | None = None,
        child_skus: list[str] | None = None,
    ) -> BuiltListing:
        issues: list[ListingIssue] = []

        # Carry extraction warnings forward. A listing built on a page we could not fully parse
        # should say so on its face, not only in a log line from an earlier stage.
        for warning in product.warnings:
            if warning.kind == "MissingField" and warning.field.startswith("attributes"):
                issues.append(
                    ListingIssue(
                        code="ExtractionGap",
                        message=f"{warning.field} could not be parsed from the source page "
                        f"({warning.detail}); the attribute was omitted",
                        severity="WARNING",  # type: ignore[arg-type]
                        attribute_names=[warning.field],
                        source="local",
                    )
                )

        if not is_parent and "main_product_image_locator" not in attributes:
            issues.append(
                ListingIssue(
                    code="NoMainImage",
                    message="no main image URL — Amazon requires one and will suppress the listing "
                    "without it. Check the media pipeline and image hosting configuration.",
                    source="local",
                )
            )

        if row.uses_gtin_exemption and not self.settings.is_brand_registered:
            issues.append(
                ListingIssue(
                    code="GtinExemptionUnverified",
                    message="listing relies on a GTIN exemption, but ANZOR_BRAND_REGISTERED is "
                    "false. Confirm the exemption is approved for this brand and product type in "
                    "Seller Central before submitting.",
                    severity="WARNING",  # type: ignore[arg-type]
                    source="policy",
                )
            )

        payload_hash = hashlib.sha256(
            json.dumps(attributes, sort_keys=True, default=str).encode()
        ).hexdigest()

        listing = BuiltListing(
            sku=sku,
            parent_sku=parent_sku,
            source_sku=product.sku,
            marketplace_id=marketplace.marketplace_id,
            marketplace_code=marketplace.code,
            product_type=self.product_type(product, row),
            attributes=attributes,
            offer=(
                OfferTerms(
                    price=quote.price,
                    currency=quote.currency,
                    list_price=quote.list_price,
                    quantity=(
                        row.quantity if row.quantity is not None else self.settings.default_quantity
                    ),
                    handling_time_days=(
                        row.handling_time_days
                        if row.handling_time_days is not None
                        else self.settings.handling_time_days
                    ),
                    condition=row.condition,
                )
                if quote is not None
                else None
            ),
            is_parent=is_parent,
            variation_theme=variation_theme,
            child_skus=child_skus or [],
            issues=issues,
            status=ListingStatus.BUILT,
            source_url=product.source_url,
            content_hash=product.content_hash,
            payload_hash=payload_hash,
            built_at=datetime.now(timezone.utc),
        )
        log.debug(
            "mapper.built",
            sku=sku,
            marketplace=marketplace.code,
            attributes=len(attributes),
            issues=len(issues),
            provenance=resolver.provenance,
        )
        return listing


# ---------------------------------------------------------------------------- helpers


def _normalise_metal(metal_type: str | None, metal_color: str | None) -> str | None:
    """Map the parsed metal into Amazon's controlled ``metal_type`` vocabulary.

    Two-tone is deliberately unmapped: Amazon has no two-tone value in most marketplaces, and
    picking one of the two colours would be a false claim. It is omitted and surfaced as a
    spreadsheet override opportunity instead.
    """
    if metal_type is None:
        return None
    if metal_color and metal_color.startswith("Two-Tone"):
        return None
    colour = (metal_color or "").split()[0] if metal_color else None
    return METAL_TYPE_NORMALISED.get(
        (metal_type, colour), METAL_TYPE_NORMALISED.get((metal_type, None))
    )


def _gtin_type(digits: str) -> str:
    """Amazon wants the identifier *type* alongside the value, and infers nothing from length."""
    return {8: "ean", 12: "upc", 13: "ean", 14: "gtin"}.get(len(digits), "upc")
