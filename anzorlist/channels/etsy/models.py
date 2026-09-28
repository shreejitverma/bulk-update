"""Etsy listing artifacts: exactly what is sent to Open API v3.

Etsy differs from Amazon and eBay in three ways that shape this model:

* There is no SKU-keyed upsert. A listing is created once and then addressed by its numeric
  ``listing_id``, which the publisher records per website SKU (see ``publish.py``).
* Images are uploaded as bytes, not fetched from URLs, so the artifact carries local file paths
  and no image hosting is needed for Etsy.
* Sizes are *inventory products* on one listing, each with its own price and SKU, driven by a
  custom property ("Ring size").
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from anzorlist.models.listing import ListingIssue

# Etsy's reserved property ids for seller-defined variations.
CUSTOM_PROPERTY_ID = 513

# The photo formats Etsy's uploadListingImage accepts, by file suffix, with their content types.
IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
}


class EtsyVariation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str
    value: str  # "7", "18 in"
    price: Decimal


class EtsyListing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_sku: str
    family: str  # Ring, Earrings, ... - resolved to a taxonomy id against Etsy's own tree
    title: str
    description: str
    who_made: str  # i_did, someone_else, collective (ETSY_WHO_MADE)
    price: Decimal  # the base (single-item, or lowest-size) price
    currency: str
    quantity: int
    tags: list[str]
    materials: list[str]
    image_files: list[str]
    variation_property: str | None = None  # "Ring size"
    variations: list[EtsyVariation] = Field(default_factory=list)
    issues: list[ListingIssue] = Field(default_factory=list)
    source_url: str = ""
    payload_hash: str = ""
    built_at: datetime | None = None

    marketplace_id: str = "ETSY"

    @property
    def sku(self) -> str:
        return self.source_sku

    @property
    def blocking_issues(self) -> list[ListingIssue]:
        return [i for i in self.issues if i.blocking]

    @property
    def submittable(self) -> bool:
        return not self.blocking_issues

    def listing_fields(self) -> dict[str, Any]:
        """Fields shared by createDraftListing and updateListing (without shop settings)."""
        return {
            "title": self.title,
            "description": self.description,
            "tags": self.tags,
            "materials": self.materials,
            "who_made": self.who_made,
        }

    def inventory_body(self) -> dict[str, Any]:
        """Body for updateListingInventory: one product, or one per size with its own price."""
        if not self.variations:
            return {
                "products": [
                    {
                        "sku": self.source_sku,
                        "property_values": [],
                        "offerings": [
                            {
                                "price": float(self.price),
                                "quantity": self.quantity,
                                "is_enabled": True,
                            }
                        ],
                    }
                ],
                "price_on_property": [],
                "quantity_on_property": [],
                "sku_on_property": [],
            }
        return {
            "products": [
                {
                    "sku": v.sku,
                    "property_values": [
                        {
                            "property_id": CUSTOM_PROPERTY_ID,
                            "property_name": self.variation_property,
                            "values": [v.value],
                        }
                    ],
                    "offerings": [
                        {"price": float(v.price), "quantity": self.quantity, "is_enabled": True}
                    ],
                }
                for v in self.variations
            ],
            "price_on_property": [CUSTOM_PROPERTY_ID],
            "quantity_on_property": [],
            "sku_on_property": [CUSTOM_PROPERTY_ID],
        }

    def compute_payload_hash(self) -> str:
        payload = {
            "fields": self.listing_fields(),
            "family": self.family,
            "inventory": self.inventory_body(),
            "images": [_file_digest(f) for f in self.image_files],
        }
        canonical = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _file_digest(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return f"missing:{path}"
