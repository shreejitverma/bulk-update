"""The workbook column contract.

One declarative table, :data:`COLUMNS`, is the single source of truth for the spreadsheet. The
template writer builds headers, widths, help comments, and dropdown validations from it; the
reader parses and validates against it. They cannot drift, because there is only one definition.

Design principle: **the operator should only type what the website cannot tell us.** Title,
description, specs, price, images, and ring sizes are all extracted from
anzorjewelrycorp.com. Every column here is therefore either the join key (``sku``), a listing
control (include / quantity / marketplaces), or an override that wins over the extracted value.

Only ``sku`` is mandatory. A blank cell means "use what the site says", never "clear the value".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

CellType = Literal["str", "int", "decimal", "bool", "list"]


@dataclass(frozen=True)
class Column:
    """One spreadsheet column.

    ``key`` is the snake_case attribute on :class:`~anzorlist.ingest.row.ListingRow`.
    ``header`` is what the operator sees. ``choices`` becomes an Excel dropdown, and the reader
    enforces the same set — an out-of-range value is a row error, not a silent pass-through.
    """

    key: str
    header: str
    type: CellType
    required: bool = False
    width: int = 18
    help: str = ""
    choices: tuple[str, ...] = ()
    example: object | None = None
    group: str = "General"


# Vocabularies shared between the dropdowns and the reader's validation.
CONDITIONS = ("new_new", "used_like_new", "used_very_good", "used_good", "used_acceptable")
GENDERS = ("female", "male", "unisex")
YES_NO = ("Y", "N")
VARIATION_SOURCES = ("site", "none")

# Amazon jewelry product types. These are the defaults used when the operator leaves the column
# blank; `anzorlist amazon product-types` replaces this list with the definitive set
# from the Product Type Definitions API for your marketplace, because the valid set is
# marketplace-specific and Amazon revises it.
JEWELRY_PRODUCT_TYPES = (
    "RING",
    "EARRING",
    "NECKLACE",
    "BRACELET",
    "CHARM",
    "ANKLET",
    "BODY_JEWELRY",
    "JEWELRY_SET",
    "PENDANT",
    "LOOSE_STONE",
)

METAL_TYPES = (
    "yellow_gold",
    "white_gold",
    "rose_gold",
    "two_tone_gold",
    "platinum",
    "sterling_silver",
    "palladium",
    "titanium",
    "stainless_steel",
)

GEM_TYPES = (
    "diamond",
    "sapphire",
    "ruby",
    "emerald",
    "pearl",
    "topaz",
    "amethyst",
    "garnet",
    "aquamarine",
    "opal",
    "tanzanite",
    "morganite",
    "citrine",
    "peridot",
    "none",
)


COLUMNS: tuple[Column, ...] = (
    # ---------------- Identity & control ----------------
    Column(
        "sku",
        "SKU",
        "str",
        required=True,
        width=14,
        group="Control",
        help="REQUIRED. The Anzor SKU exactly as it appears on the website, e.g. R985. "
        "This is the join key: the system fetches "
        "anzorjewelrycorp.com/Scripts/prodview.asp?SKU=<this> and extracts everything else.",
        example="R985",
    ),
    Column(
        "include",
        "Include?",
        "bool",
        width=10,
        group="Control",
        choices=YES_NO,
        help="Y to include this row in the next run, N to stage it without uploading. "
        "Blank counts as Y.",
        example="Y",
    ),
    Column(
        "marketplaces",
        "Marketplaces",
        "list",
        width=22,
        group="Control",
        help="Comma-separated marketplace codes: US, CA, MX, UK, DE, FR, IT, ES, NL, SE, PL, "
        "BE, IE, JP, AU, SG. Blank uses ANZOR_MARKETPLACES from .env (default US). "
        "EU codes need a separate SP-API authorization from US/CA/MX.",
        example="US",
    ),
    Column(
        "amazon_product_type",
        "Amazon Product Type",
        "str",
        width=22,
        group="Control",
        choices=JEWELRY_PRODUCT_TYPES,
        help="Blank is recommended — it is inferred from the SKU prefix "
        "(R=RING, E=EARRING, P=PENDANT, B=BRACELET, N=NECKLACE, S=JEWELRY_SET) and "
        "confirmed against Amazon's Product Type Definitions API. Set it only to override.",
    ),
    Column(
        "variation_source",
        "Variations",
        "str",
        width=12,
        group="Control",
        choices=VARIATION_SOURCES,
        help="'site' (default) builds a parent/child variation family from the ring sizes or "
        "chain lengths on the product page. 'none' lists a single standalone item using "
        "the base size only.",
        example="site",
    ),
    # ---------------- Offer ----------------
    Column(
        "quantity",
        "Quantity",
        "int",
        width=10,
        group="Offer",
        help="Sellable units. Blank uses ANZOR_DEFAULT_QUANTITY from .env. "
        "For a made-to-order size family this applies to every child SKU.",
        example=1,
    ),
    Column(
        "price_override_usd",
        "Price Override (USD)",
        "decimal",
        width=18,
        group="Offer",
        help="Blank is recommended. By default the price is the website price marked up by "
        "PRICE_MARKUP_AMAZON (0.20) so the Amazon referral fee does not eat your margin. "
        "Enter a number here to set the exact Amazon price instead, bypassing the markup.",
    ),
    Column(
        "list_price_usd",
        "List Price / MSRP (USD)",
        "decimal",
        width=20,
        group="Offer",
        help="Optional strike-through comparison price. Blank uses the website's MSRP if it has "
        "one. Amazon requires this to be a genuine former or manufacturer price.",
    ),
    Column(
        "condition",
        "Condition",
        "str",
        width=14,
        group="Offer",
        choices=CONDITIONS,
        help="Blank means new_new (brand new). Anything else needs a condition note.",
        example="new_new",
    ),
    Column(
        "handling_time_days",
        "Handling Time (days)",
        "int",
        width=18,
        group="Offer",
        help="Business days between order and ship. Blank uses ANZOR_HANDLING_TIME_DAYS (3). "
        "Be honest here — late shipment rate is an account-health metric.",
    ),
    Column(
        "upc_ean",
        "UPC / EAN",
        "str",
        width=16,
        group="Offer",
        help="Leave BLANK unless you own real GS1 barcodes. Blank triggers the GTIN-exemption "
        "path, which is the correct route for own-manufactured jewelry. "
        "Never invent a barcode — Amazon verifies them against the GS1 registry.",
    ),
    # ---------------- Copy overrides ----------------
    Column(
        "item_name_override",
        "Title Override",
        "str",
        width=44,
        group="Copy",
        help="Blank is recommended — a compliant title is generated from the extracted specs. "
        "Amazon caps jewelry titles at 200 characters and rejects ALL CAPS, promotional "
        "language ('BEST', 'SALE'), and symbols like ! $ ?.",
    ),
    Column(
        "bullet_1",
        "Bullet 1",
        "str",
        width=40,
        group="Copy",
        help="Optional override for the first bullet. Blank generates one from the specs. "
        "500 characters max, no promotional or price claims.",
    ),
    Column("bullet_2", "Bullet 2", "str", width=40, group="Copy", help="Optional. See Bullet 1."),
    Column("bullet_3", "Bullet 3", "str", width=40, group="Copy", help="Optional. See Bullet 1."),
    Column("bullet_4", "Bullet 4", "str", width=40, group="Copy", help="Optional. See Bullet 1."),
    Column("bullet_5", "Bullet 5", "str", width=40, group="Copy", help="Optional. See Bullet 1."),
    Column(
        "description_override",
        "Description Override",
        "str",
        width=50,
        group="Copy",
        help="Blank is recommended. The generated description is sanitised: shipping promises, "
        "phone numbers, competitor mentions, and site-navigation text are stripped, because "
        "Amazon suppresses listings that contain them.",
    ),
    Column(
        "search_terms",
        "Search Terms",
        "str",
        width=40,
        group="Copy",
        help="Optional backend keywords, space or comma separated, 250 bytes max for most "
        "marketplaces. Do not repeat words already in the title — Amazon ignores duplicates. "
        "No brand names you do not own, no competitor terms.",
    ),
    # ---------------- Attribute overrides ----------------
    Column(
        "metal_type",
        "Metal Type",
        "str",
        width=16,
        group="Attributes",
        choices=METAL_TYPES,
        help="Blank uses the metal parsed from the website's Item Details. Override only when "
        "the site data is wrong or ambiguous.",
    ),
    Column(
        "metal_stamp",
        "Metal Stamp",
        "str",
        width=14,
        group="Attributes",
        help="Purity stamp such as 14k, 18k, 925, 950. Blank uses the parsed value.",
    ),
    Column(
        "gem_type",
        "Gem Type",
        "str",
        width=16,
        group="Attributes",
        choices=GEM_TYPES,
        help="Primary stone. Blank uses the parsed value. Use 'none' for plain metal pieces. "
        "FTC rules require lab-grown and simulated stones to be disclosed — the generator "
        "copies the site's qualifier verbatim and never upgrades 'simulated' to 'genuine'.",
    ),
    Column(
        "total_gem_weight_ct",
        "Total Gem Weight (ct)",
        "decimal",
        width=20,
        group="Attributes",
        help="Total carat weight. Blank uses the parsed value. Amazon requires this to be "
        "accurate to the stated precision; over-stating carat weight is an FTC violation.",
    ),
    Column(
        "total_metal_weight_g",
        "Metal Weight (g)",
        "decimal",
        width=17,
        group="Attributes",
        help="Gross metal weight in grams. Blank uses the parsed value.",
    ),
    Column(
        "target_gender",
        "Target Gender",
        "str",
        width=14,
        group="Attributes",
        choices=GENDERS,
        help="Blank defaults to 'unisex'. Drives Amazon's department browse-node placement.",
    ),
    Column(
        "style",
        "Style",
        "str",
        width=18,
        group="Attributes",
        help="Optional style keyword, e.g. Solitaire, Halo, Eternity, Tennis, Stud.",
    ),
    Column(
        "ring_sizes_override",
        "Ring Sizes Override",
        "str",
        width=24,
        group="Attributes",
        help="Optional comma-separated sizes, e.g. 5,5.5,6,6.5,7. Blank uses the sizes offered "
        "on the product page. Only used when Variations = site.",
    ),
    # ---------------- Notes ----------------
    Column(
        "notes",
        "Notes (not uploaded)",
        "str",
        width=34,
        group="Notes",
        help="Free text for your own tracking. Never sent to Amazon.",
    ),
)

COLUMNS_BY_HEADER: dict[str, Column] = {c.header: c for c in COLUMNS}
COLUMNS_BY_KEY: dict[str, Column] = {c.key: c for c in COLUMNS}
REQUIRED_HEADERS: tuple[str, ...] = tuple(c.header for c in COLUMNS if c.required)

PRODUCTS_SHEET = "Products"
README_SHEET = "How to use"
REFERENCE_SHEET = "Reference"
RESULTS_SHEET = "Upload Results"

# Rows pre-filled in the generated template so the operator has a worked example to copy.
# These are the four SKUs with committed HTML fixtures, so they can be run end-to-end offline.
EXAMPLE_ROWS: tuple[dict[str, object], ...] = (
    {
        "sku": "R985",
        "include": "Y",
        "marketplaces": "US",
        "variation_source": "site",
        "quantity": 1,
        "condition": "new_new",
        "target_gender": "female",
        "notes": "Example row — delete or set Include=N once you add your own.",
    },
    {
        "sku": "E1154",
        "include": "Y",
        "marketplaces": "US",
        "variation_source": "none",
        "quantity": 1,
        "condition": "new_new",
        "target_gender": "female",
        "notes": "Example row — earrings have no size axis, so Variations = none.",
    },
    {
        "sku": "S220",
        "include": "N",
        "marketplaces": "US",
        "variation_source": "none",
        "quantity": 1,
        "condition": "new_new",
        "notes": "Example row, staged (Include = N) so it is skipped.",
    },
    {
        "sku": "E711",
        "include": "N",
        "marketplaces": "US",
        "variation_source": "none",
        "quantity": 1,
        "condition": "new_new",
        "notes": "Example row, staged (Include = N) so it is skipped.",
    },
)


@dataclass
class RowError:
    """A validation failure tied to a spreadsheet cell, addressed the way Excel addresses it."""

    row: int
    header: str
    value: object
    message: str
    cell: str = field(default="")

    def __str__(self) -> str:
        loc = self.cell or f"row {self.row}, {self.header!r}"
        return f"{loc}: {self.message} (got {self.value!r})"


def decimal_or_none(value: object) -> Decimal | None:
    """Coerce a spreadsheet cell to Decimal. Excel hands us float; go through str to avoid
    binary-float artefacts like 1234.5600000000001 landing in a price."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return Decimal(str(value).replace(",", "").replace("$", "").strip())
