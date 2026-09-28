"""The Product model — the single contract between extraction, generation, and channels.

Extraction produces a ``Product``; generation consumes it; channel adapters translate it.
No channel code ever touches HTML. Every extracted value carries a provenance key so any
material/gem/carat/purity claim in generated copy can be traced back to a source element.

Money is modeled with :class:`decimal.Decimal` throughout — never ``float`` on prices.
Ring/chain/bracelet sizing is modeled as :class:`Variation` and never flattened.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Money(BaseModel):
    """A monetary amount. Prices are always Decimal, quantized to cents by the parser."""

    model_config = ConfigDict(frozen=True)

    amount: Decimal
    currency: str = "USD"


class ProvenanceEntry(BaseModel):
    """Where a value came from, so any claim can be traced back to the source element."""

    source_url: str
    anchor: str  # the LABEL text or microdata itemprop we keyed on — never an nth-child path
    raw_snippet: str  # verbatim text/HTML the value was parsed from
    extracted_at: datetime


WarningKind = Literal[
    "MissingField",
    "AmbiguousParse",
    "EncodingRepair",
    "ProbeFailed",
    "FamilyMismatch",
]


class ExtractionWarning(BaseModel):
    """A non-fatal extraction issue. A missing field is recorded here, never fabricated."""

    kind: WarningKind
    field: str
    detail: str


class ProductFamily(str, Enum):
    RING = "Ring"
    EARRINGS = "Earrings"
    PENDANT = "Pendant"
    BRACELET = "Bracelet"
    NECKLACE = "Necklace"
    SET = "Set"
    LOOSE_STONE = "Loose Stone"


# SKU prefix -> family, inferred then verified against the breadcrumb.
SKU_PREFIX_FAMILY: dict[str, ProductFamily] = {
    "R": ProductFamily.RING,
    "E": ProductFamily.EARRINGS,
    "P": ProductFamily.PENDANT,
    "B": ProductFamily.BRACELET,
    "N": ProductFamily.NECKLACE,
    "S": ProductFamily.SET,
    "L": ProductFamily.LOOSE_STONE,  # loose diamonds — a stone, not a finished piece
}


class Gemstone(BaseModel):
    """A normalized gemstone claim. Every field here must trace to a source spec/description.

    FTC-critical qualifiers (``genuine``, ``origin``, ``treatment``) are preserved verbatim
    from the source and never inferred or defaulted to a more favorable value.
    """

    type: str  # "Sapphire", "Diamond", "Emerald"
    genuine: bool | None = None  # genuine vs simulated
    origin: Literal["natural", "lab_grown"] | None = None
    treatment: str | None = None  # e.g. "heat treated"
    carat_weight: Decimal | None = None
    count: int | None = None
    shape_cut: str | None = None  # "Round", "Princess"
    color: str | None = None
    clarity: str | None = None
    provenance_key: str


class Attributes(BaseModel):
    """Normalized, typed view of the Item Details. Best-effort — may be partial.

    Gaps become :class:`ExtractionWarning` (``MissingField``); they are never filled with
    a fabricated substitute.
    """

    metal_type: str | None = None  # "Yellow Gold", "Two-Tone Gold"
    metal_purity: str | None = None  # "18k", "14k"
    metal_color: str | None = None
    gross_weight_g: Decimal | None = None
    measurements: dict[str, str] = Field(default_factory=dict)
    gemstones: list[Gemstone] = Field(default_factory=list)


class SpecRow(BaseModel):
    """A raw Item Details key/value with the source label preserved exactly.

    Parsed by label text, not table position; labels vary per product.
    """

    label: str
    value: str
    provenance_key: str


class Variation(BaseModel):
    """A sizing option with a price delta. Not flattened — this drives variation families.

    ``axis`` is derived from the option group's label (e.g. "Choose Finger Size" -> ``ring_size``,
    "Chain Length" -> ``chain_length``). ``unit`` records the measurement unit when applicable.
    """

    axis: str  # "ring_size", "chain_length", "bracelet_length", "length", ...
    label: str  # source option text, e.g. "Size 7 (Women's Avg)"
    value: Decimal | None = None  # normalized numeric size, when parseable
    unit: str | None = None  # "in", "mm", None for ring size
    price_delta: Money
    option_value_id: str | None = None  # site's <option value=...> id (join key)
    child_sku: str | None = None  # derived, e.g. "R985-14"
    provenance_key: str


class AppraisalOption(BaseModel):
    """An appraisal/certificate add-on (Free / AGI / AAA)."""

    name: str
    price_delta: Money
    option_value_id: str | None = None
    provenance_key: str


class MediaAsset(BaseModel):
    """An image or video. Dimensions/probe fields are filled by the media pipeline."""

    role: Literal["main", "alternate", "video"]
    kind: Literal["image", "video"]
    source_url: str  # absolute
    local_path: str | None = None  # data/raw/{sku}/...
    content_hash: str | None = None
    width: int | None = None
    height: int | None = None
    probed_ok: bool | None = None  # video: probe, don't assume
    provenance_key: str


class Pricing(BaseModel):
    """Source pricing. ``our_price`` is the base (0-delta) selling price."""

    list_price: Money | None = None  # strikethrough MSRP
    our_price: Money | None = None
    you_save: Money | None = None
    you_save_percent: Decimal | None = None


class Product(BaseModel):
    """The single extraction contract. See module docstring."""

    model_config = ConfigDict(extra="forbid")

    sku: str
    internal_product_id: int | None = None  # from emailToFriend idProduct — stable join key
    family: ProductFamily
    family_verified: bool = False
    source_url: str

    marketing_title_raw: str  # bold heading; may hold literal <br> and trademark/patent claims
    short_name: str | None = None
    long_description_raw: str = ""  # CONTAMINATED — never shipped; sole input to sanitize()

    specs: list[SpecRow] = Field(default_factory=list)
    attributes: Attributes = Field(default_factory=Attributes)
    pricing: Pricing = Field(default_factory=Pricing)
    in_stock: bool | None = None
    free_shipping: bool | None = None

    size_options: list[Variation] = Field(default_factory=list)
    appraisal_options: list[AppraisalOption] = Field(default_factory=list)
    media: list[MediaAsset] = Field(default_factory=list)
    breadcrumbs: list[list[str]] = Field(default_factory=list)  # multiple taxonomies

    provenance: dict[str, ProvenanceEntry] = Field(default_factory=dict)
    warnings: list[ExtractionWarning] = Field(default_factory=list)

    content_hash: str  # of raw HTML — drives re-fetch and idempotency
    fetched_at: datetime
