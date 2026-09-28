"""eBay listing artifacts: exactly what is sent to the Inventory API, and nothing inferred later.

An :class:`EbayListing` is one eBay listing page for one website SKU. It holds one
:class:`EbayItem` for a single item, or one per size for an inventory item group. The request
bodies are rendered from it by methods here, so the artifact on disk and the bytes sent cannot
drift apart.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from anzorlist.models.listing import ListingIssue


class EbayItem(BaseModel):
    """One inventory item: one SKU, one price."""

    model_config = ConfigDict(extra="forbid")

    sku: str
    title: str
    aspects: dict[str, list[str]]
    image_urls: list[str]
    quantity: int
    price: Decimal
    condition: str = "NEW"

    def inventory_item_body(self, description: str) -> dict[str, Any]:
        """Body for createOrReplaceInventoryItem (and one bulk request entry, plus ``sku``)."""
        return {
            "availability": {"shipToLocationAvailability": {"quantity": self.quantity}},
            "condition": self.condition,
            "product": {
                "title": self.title,
                "description": description,
                "aspects": self.aspects,
                "imageUrls": self.image_urls,
            },
        }


class EbayListing(BaseModel):
    """One listing page: a single item, or a group of size variations."""

    model_config = ConfigDict(extra="forbid")

    source_sku: str
    marketplace_id: str
    category_id: str
    currency: str
    title: str
    description: str
    aspects: dict[str, list[str]]
    image_urls: list[str]
    group_key: str | None = None
    varies_by: dict[str, list[str]] | None = None
    items: list[EbayItem]
    issues: list[ListingIssue] = Field(default_factory=list)
    source_url: str = ""
    payload_hash: str = ""
    built_at: datetime | None = None

    @property
    def is_group(self) -> bool:
        return self.group_key is not None

    @property
    def blocking_issues(self) -> list[ListingIssue]:
        return [i for i in self.issues if i.blocking]

    @property
    def submittable(self) -> bool:
        return not self.blocking_issues

    def group_body(self) -> dict[str, Any]:
        """Body for createOrReplaceInventoryItemGroup."""
        assert self.varies_by is not None
        return {
            "title": self.title,
            "description": self.description,
            "imageUrls": self.image_urls,
            # Aspects shared by every member; the varying one lives on each item.
            "aspects": self.aspects,
            "variantSKUs": [i.sku for i in self.items],
            "variesBy": {
                "aspectsImageVariesBy": [],
                "specifications": [
                    {"name": name, "values": values} for name, values in self.varies_by.items()
                ],
            },
        }

    def offer_body(self, item: EbayItem, policies: dict[str, str]) -> dict[str, Any]:
        """Body for createOffer / updateOffer. ``policies`` comes from settings."""
        body: dict[str, Any] = {
            "sku": item.sku,
            "marketplaceId": self.marketplace_id,
            "format": "FIXED_PRICE",
            "availableQuantity": item.quantity,
            "categoryId": self.category_id,
            "listingPolicies": {
                "fulfillmentPolicyId": policies["EBAY_FULFILLMENT_POLICY_ID"],
                "paymentPolicyId": policies["EBAY_PAYMENT_POLICY_ID"],
                "returnPolicyId": policies["EBAY_RETURN_POLICY_ID"],
            },
            "merchantLocationKey": policies["EBAY_MERCHANT_LOCATION_KEY"],
            "pricingSummary": {"price": {"value": f"{item.price:.2f}", "currency": self.currency}},
        }
        if not self.is_group:
            # A group's description lives on the group; a single item's on its offer.
            body["listingDescription"] = self.description
        return body

    def compute_payload_hash(self) -> str:
        """Hash of everything that would be sent, excluding policies (they are account config)."""
        payload = {
            "category": self.category_id,
            "group": self.group_body() if self.is_group else None,
            "items": [
                {**i.inventory_item_body(self.description), "sku": i.sku, "price": str(i.price)}
                for i in self.items
            ],
        }
        canonical = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
