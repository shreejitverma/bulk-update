"""Amazon marketplace registry.

A marketplace is the unit of everything in SP-API: authorization, rate limits, product-type
schemas, pricing, and required attributes are all per-marketplace. Region determines the API
host; a single refresh token is only valid for the region it was granted in, so US+CA+MX share
one authorization while the UK/EU marketplaces need a second, separate one.

Nothing here is inferred at runtime — the IDs are constants published by Amazon and wrong values
fail closed (SP-API rejects an unknown marketplace ID rather than guessing).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Region(str, Enum):
    """SP-API regional endpoint. One LWA authorization per region, not per marketplace."""

    NA = "na"
    EU = "eu"
    FE = "fe"


REGION_ENDPOINTS: dict[Region, str] = {
    Region.NA: "https://sellingpartnerapi-na.amazon.com",
    Region.EU: "https://sellingpartnerapi-eu.amazon.com",
    Region.FE: "https://sellingpartnerapi-fe.amazon.com",
}

# Sandbox hosts mirror production paths but return canned responses. Useful for wiring checks
# only — sandbox does NOT validate listing attributes, so it can never replace VALIDATION_PREVIEW.
REGION_SANDBOX_ENDPOINTS: dict[Region, str] = {
    Region.NA: "https://sandbox.sellingpartnerapi-na.amazon.com",
    Region.EU: "https://sandbox.sellingpartnerapi-eu.amazon.com",
    Region.FE: "https://sandbox.sellingpartnerapi-fe.amazon.com",
}


@dataclass(frozen=True)
class Marketplace:
    """One Amazon marketplace.

    ``locale`` drives ``issueLocale`` on Listings Items calls so error messages come back in a
    language the operator reads. ``language`` drives copy generation: an EU marketplace demands
    localized ``item_name``/``bullet_point``, and shipping English copy to amazon.de is both a
    conversion problem and a suppression risk.
    """

    code: str  # short handle used in the CLI and spreadsheet, e.g. "US"
    marketplace_id: str
    country: str
    region: Region
    currency: str
    locale: str
    language: str
    domain: str


# fmt: off  — this is an aligned data table; wrapping it would make it unreadable.
# ruff: noqa: E501
_MARKETPLACES: tuple[Marketplace, ...] = (
    # --- North America ---
    Marketplace(
        "US", "ATVPDKIKX0DER", "United States", Region.NA, "USD", "en_US", "en", "amazon.com"
    ),
    Marketplace("CA", "A2EUQ1WTGCTBG2", "Canada", Region.NA, "CAD", "en_CA", "en", "amazon.ca"),
    Marketplace("MX", "A1AM78C64UM0Y8", "Mexico", Region.NA, "MXN", "es_MX", "es", "amazon.com.mx"),
    Marketplace("BR", "A2Q3Y263D00KWC", "Brazil", Region.NA, "BRL", "pt_BR", "pt", "amazon.com.br"),
    # --- Europe ---
    Marketplace(
        "UK", "A1F83G8C2ARO7P", "United Kingdom", Region.EU, "GBP", "en_GB", "en", "amazon.co.uk"
    ),
    Marketplace("DE", "A1PA6795UKMFR9", "Germany", Region.EU, "EUR", "de_DE", "de", "amazon.de"),
    Marketplace("FR", "A13V1IB3VIYZZH", "France", Region.EU, "EUR", "fr_FR", "fr", "amazon.fr"),
    Marketplace("IT", "APJ6JRA9NG5V4", "Italy", Region.EU, "EUR", "it_IT", "it", "amazon.it"),
    Marketplace("ES", "A1RKKUPIHCS9HS", "Spain", Region.EU, "EUR", "es_ES", "es", "amazon.es"),
    Marketplace(
        "NL", "A1805IZSGTT6HS", "Netherlands", Region.EU, "EUR", "nl_NL", "nl", "amazon.nl"
    ),
    Marketplace("SE", "A2NODRKZP88ZB9", "Sweden", Region.EU, "SEK", "sv_SE", "sv", "amazon.se"),
    Marketplace("PL", "A1C3SOZRARQ6R3", "Poland", Region.EU, "PLN", "pl_PL", "pl", "amazon.pl"),
    Marketplace("BE", "AMEN7PMS3EDWL", "Belgium", Region.EU, "EUR", "fr_BE", "fr", "amazon.com.be"),
    Marketplace("IE", "A28R8C7NBKEWEA", "Ireland", Region.EU, "EUR", "en_IE", "en", "amazon.ie"),
    Marketplace("TR", "A33AVAJ2PDY3EV", "Turkey", Region.EU, "TRY", "tr_TR", "tr", "amazon.com.tr"),
    Marketplace(
        "AE", "A2VIGQ35RCS4UG", "United Arab Emirates", Region.EU, "AED", "en_AE", "en", "amazon.ae"
    ),
    Marketplace(
        "SA", "A17E79C6D8DWNP", "Saudi Arabia", Region.EU, "SAR", "en_AE", "en", "amazon.sa"
    ),
    Marketplace("EG", "ARBP9OOSHTCHU", "Egypt", Region.EU, "EGP", "en_AE", "en", "amazon.eg"),
    Marketplace("IN", "A21TJRUUN4KGV", "India", Region.EU, "INR", "en_IN", "en", "amazon.in"),
    # --- Far East ---
    Marketplace("JP", "A1VC38T7YXB528", "Japan", Region.FE, "JPY", "ja_JP", "ja", "amazon.co.jp"),
    Marketplace(
        "AU", "A39IBJ37TRP1C6", "Australia", Region.FE, "AUD", "en_AU", "en", "amazon.com.au"
    ),
    Marketplace("SG", "A19VAU5U5O7RUS", "Singapore", Region.FE, "SGD", "en_SG", "en", "amazon.sg"),
)

BY_CODE: dict[str, Marketplace] = {m.code: m for m in _MARKETPLACES}
BY_ID: dict[str, Marketplace] = {m.marketplace_id: m for m in _MARKETPLACES}

# The launch set agreed with the operator: prove US, then the rest of North America, then EU.
# Order matters — the CLI treats the first entry as the marketplace to validate against by default.
DEFAULT_ROLLOUT: tuple[str, ...] = ("US", "CA", "MX", "UK", "DE", "FR", "IT", "ES")


class UnknownMarketplace(KeyError):
    """Raised for a marketplace handle the registry does not know. Never guessed."""


def resolve(code_or_id: str) -> Marketplace:
    """Resolve a marketplace by short code (``US``) or marketplace ID (``ATVPDKIKX0DER``)."""
    key = code_or_id.strip()
    if key.upper() in BY_CODE:
        return BY_CODE[key.upper()]
    if key in BY_ID:
        return BY_ID[key]
    raise UnknownMarketplace(
        f"{code_or_id!r} is not a known marketplace. Known codes: {', '.join(sorted(BY_CODE))}"
    )


def resolve_all(codes: list[str] | tuple[str, ...] | str) -> list[Marketplace]:
    """Resolve a comma-separated string or sequence of handles, preserving order and de-duping."""
    items = codes.split(",") if isinstance(codes, str) else list(codes)
    out: list[Marketplace] = []
    seen: set[str] = set()
    for raw in items:
        if not raw.strip():
            continue
        m = resolve(raw)
        if m.marketplace_id not in seen:
            seen.add(m.marketplace_id)
            out.append(m)
    return out


def group_by_region(markets: list[Marketplace]) -> dict[Region, list[Marketplace]]:
    """Group marketplaces by region. Each group needs its own refresh token and its own client."""
    grouped: dict[Region, list[Marketplace]] = {}
    for m in markets:
        grouped.setdefault(m.region, []).append(m)
    return grouped
