"""The validated spreadsheet row.

A :class:`ListingRow` is the operator's *intent*: which SKU, where to sell it, and any override
that should beat the extracted site data. It is deliberately almost-all-optional — the website is
the source of truth and the spreadsheet is the exception list.

Merging happens in :mod:`anzorlist.channels.amazon.mapper`, under one rule: **a non-empty
override always wins; a blank cell always defers to extraction.** A blank cell can never blank
out an extracted value, because that would make an accidental deletion indistinguishable from a
deliberate one.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from anzorlist.ingest.schema import (
    CONDITIONS,
    GENDERS,
    JEWELRY_PRODUCT_TYPES,
    VARIATION_SOURCES,
)


class ListingRow(BaseModel):
    """One row of the Products sheet, validated."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # Identity & control
    sku: str
    include: bool = True
    marketplaces: list[str] = Field(default_factory=list)  # empty = fall back to settings
    amazon_product_type: str | None = None
    variation_source: str = "site"

    # Offer
    quantity: int | None = None
    price_override_usd: Decimal | None = None
    list_price_usd: Decimal | None = None
    condition: str = "new_new"
    handling_time_days: int | None = None
    upc_ean: str | None = None

    # Copy
    item_name_override: str | None = None
    bullet_1: str | None = None
    bullet_2: str | None = None
    bullet_3: str | None = None
    bullet_4: str | None = None
    bullet_5: str | None = None
    description_override: str | None = None
    search_terms: str | None = None

    # Attributes
    metal_type: str | None = None
    metal_stamp: str | None = None
    gem_type: str | None = None
    total_gem_weight_ct: Decimal | None = None
    total_metal_weight_g: Decimal | None = None
    target_gender: str | None = None
    style: str | None = None
    ring_sizes_override: str | None = None

    # Bookkeeping
    notes: str | None = None
    source_row: int = 0  # 1-based spreadsheet row, for error messages

    # ---- validators ----

    @field_validator("sku")
    @classmethod
    def _sku_shape(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("SKU is required")
        if not v.replace("-", "").replace("_", "").isalnum():
            raise ValueError(
                f"SKU {v!r} contains characters the site's prodview.asp will not accept; "
                "expected letters, digits, hyphen or underscore only"
            )
        return v

    @field_validator("condition")
    @classmethod
    def _known_condition(cls, v: str) -> str:
        v = (v or "new_new").strip().lower()
        if v not in CONDITIONS:
            raise ValueError(f"unknown condition; expected one of {', '.join(CONDITIONS)}")
        return v

    @field_validator("variation_source")
    @classmethod
    def _known_variation_source(cls, v: str) -> str:
        v = (v or "site").strip().lower()
        if v not in VARIATION_SOURCES:
            raise ValueError(f"expected one of {', '.join(VARIATION_SOURCES)}")
        return v

    @field_validator("target_gender")
    @classmethod
    def _known_gender(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if v not in GENDERS:
            raise ValueError(f"expected one of {', '.join(GENDERS)}")
        return v

    @field_validator("amazon_product_type")
    @classmethod
    def _known_product_type(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().upper()
        if v not in JEWELRY_PRODUCT_TYPES:
            # Not fatal in principle — Amazon owns the vocabulary and revises it — but a typo here
            # produces a confusing 400 much later, so reject at the door with a readable message.
            raise ValueError(
                f"{v!r} is not in the known jewelry product-type list. "
                f"Leave blank to infer it, or run `anzorlist amazon product-types --refresh` "
                f"to pull the current list for your marketplace."
            )
        return v

    @field_validator("quantity", "handling_time_days")
    @classmethod
    def _non_negative(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("must be zero or greater")
        return v

    @field_validator(
        "price_override_usd", "list_price_usd", "total_gem_weight_ct", "total_metal_weight_g"
    )
    @classmethod
    def _positive_decimal(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v <= 0:
            raise ValueError("must be greater than zero")
        return v

    @field_validator("upc_ean")
    @classmethod
    def _gtin_shape(cls, v: str | None) -> str | None:
        """Validate the check digit locally. A malformed GTIN is a guaranteed rejection later,
        and jewelers frequently paste an internal part number into this column by mistake."""
        if v is None:
            return None
        digits = v.strip().replace("-", "").replace(" ", "")
        if not digits:
            return None
        if not digits.isdigit() or len(digits) not in (8, 12, 13, 14):
            raise ValueError(
                "a GTIN is 8, 12, 13 or 14 digits (EAN-8, UPC-A, EAN-13, GTIN-14). "
                "Leave this blank to use the GTIN-exemption path instead of inventing one."
            )
        if not _gtin_check_digit_ok(digits):
            raise ValueError(
                "GTIN check digit does not validate — this is not a real barcode. "
                "Leave blank for GTIN exemption rather than guessing."
            )
        return digits

    # ---- derived ----

    @property
    def bullets(self) -> list[str]:
        """Non-empty bullet overrides, in order. Fewer than 5 is fine; gaps are closed up."""
        return [
            b
            for b in (self.bullet_1, self.bullet_2, self.bullet_3, self.bullet_4, self.bullet_5)
            if b
        ]

    @property
    def uses_gtin_exemption(self) -> bool:
        return not self.upc_ean

    def ring_sizes(self) -> list[str] | None:
        if not self.ring_sizes_override:
            return None
        return [s.strip() for s in self.ring_sizes_override.split(",") if s.strip()]


def _gtin_check_digit_ok(digits: str) -> bool:
    """Standard GS1 mod-10: weight digits 3/1 from the right, excluding the check digit."""
    body, check = digits[:-1], int(digits[-1])
    total = 0
    for i, ch in enumerate(reversed(body)):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    return (10 - total % 10) % 10 == check
