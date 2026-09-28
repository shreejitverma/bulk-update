"""Channel pricing.

The website price is what Anzor charges when it keeps 100% of the sale. Amazon takes a referral
fee off the top — 5% above $250 for fine jewelry, 20% below it, with a $2 minimum — so listing
the web price on Amazon silently gives that margin away.

The correction is a **gross-up**, not a markup, and the distinction is the whole point:

    naive markup:  amazon_price = web * (1 + f)      →  net = web * (1 + f) * (1 - f)  <  web
    gross-up:      amazon_price = web / (1 - f)      →  net = web                       ✓

With f = 0.20, a naive 20% markup on a $1,000 item nets $960 — a 4% loss that compounds across
the catalog and is invisible on any single line. Dividing nets exactly $1,000.

``PRICE_MARKUP_AMAZON`` is therefore read as *the fee fraction to absorb*, and the operator's
mental model ("absorb Amazon's 20%") produces the arithmetic that actually does that.

All money is :class:`~decimal.Decimal` with explicit rounding. Float pricing produces
off-by-a-cent listings that fail reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

import structlog

from anzorlist.marketplaces import Marketplace

log = structlog.get_logger(__name__)

CENT = Decimal("0.01")

# Amazon fine-jewelry referral fee, US. Tiered: the lower rate applies to the portion above the
# breakpoint. Used for the informational net-proceeds estimate, not to set the price itself.
JEWELRY_FEE_BREAKPOINT = Decimal("250.00")
JEWELRY_FEE_LOW = Decimal("0.05")  # portion above the breakpoint
JEWELRY_FEE_HIGH = Decimal("0.20")  # portion up to the breakpoint
MIN_REFERRAL_FEE = Decimal("2.00")

# Marketplaces where Amazon requires whole-number prices (no minor unit in practice).
ZERO_DECIMAL_CURRENCIES = {"JPY"}


class PricingError(ValueError):
    """A price could not be computed. Never resolved by inventing a number."""


@dataclass(frozen=True)
class PriceQuote:
    """A computed price with its full derivation, so any number can be explained."""

    marketplace: str
    currency: str
    web_price: Decimal
    price: Decimal
    list_price: Decimal | None
    fee_fraction: Decimal
    estimated_referral_fee: Decimal
    estimated_net: Decimal
    source: str  # "override" | "grossup"

    def explain(self) -> str:
        if self.source == "override":
            return (
                f"{self.marketplace}: {self.currency} {self.price} (manual override; "
                f"est. fee {self.estimated_referral_fee}, net {self.estimated_net})"
            )
        return (
            f"{self.marketplace}: web {self.web_price} / (1 - {self.fee_fraction}) = "
            f"{self.currency} {self.price}  "
            f"(est. referral fee {self.estimated_referral_fee}, net {self.estimated_net})"
        )


def quantize(amount: Decimal, currency: str = "USD") -> Decimal:
    """Round to the currency's minor unit, half-up. Banker's rounding is wrong for retail
    prices: it makes a $x.xx5 price round down half the time, which is not what a merchant
    setting a price expects."""
    if currency in ZERO_DECIMAL_CURRENCIES:
        return amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def estimate_referral_fee(price: Decimal) -> Decimal:
    """Amazon's US fine-jewelry referral fee for a given price. Tiered, with a floor."""
    if price <= JEWELRY_FEE_BREAKPOINT:
        fee = price * JEWELRY_FEE_HIGH
    else:
        fee = (
            JEWELRY_FEE_BREAKPOINT * JEWELRY_FEE_HIGH
            + (price - JEWELRY_FEE_BREAKPOINT) * JEWELRY_FEE_LOW
        )
    return quantize(max(fee, MIN_REFERRAL_FEE))


def charm_price(price: Decimal, currency: str = "USD") -> Decimal:
    """Nudge to a .99 ending without ever lowering the price below the computed floor.

    Rounding *up* to the next .99 matters: rounding down would erode exactly the margin the
    gross-up just protected.
    """
    if currency in ZERO_DECIMAL_CURRENCIES:
        return price.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    whole = int(price)
    candidate = Decimal(whole) + Decimal("0.99")
    if candidate < price:
        candidate = Decimal(whole + 1) + Decimal("0.99")
    return quantize(candidate, currency)


def price_for(
    web_price: Decimal | None,
    marketplace: Marketplace,
    *,
    fee_fraction: Decimal,
    override: Decimal | None = None,
    list_price: Decimal | None = None,
    floor: Decimal = Decimal("10.00"),
    fx_rate: Decimal | None = None,
    charm: bool = False,
) -> PriceQuote:
    """Compute the marketplace price for one item.

    ``fx_rate`` converts from USD into the marketplace currency. It is required — never guessed —
    for any non-USD marketplace, because a missing rate that silently defaults to 1.0 would list
    a $1,200 ring for ¥1,200.
    """
    if override is not None:
        base = override
        source = "override"
    elif web_price is not None:
        if fee_fraction >= Decimal("1"):
            raise PricingError(f"fee fraction {fee_fraction} must be below 1.0")
        base = web_price / (Decimal("1") - fee_fraction)
        source = "grossup"
    else:
        raise PricingError(
            "no price available: the product page has no selling price and no "
            "'Price Override (USD)' was set in the spreadsheet"
        )

    if marketplace.currency != "USD":
        if fx_rate is None:
            raise PricingError(
                f"{marketplace.code} prices in {marketplace.currency} but no FX rate was "
                f"supplied. Set an explicit rate rather than listing a USD figure as "
                f"{marketplace.currency}."
            )
        base = base * fx_rate
        if list_price is not None:
            list_price = list_price * fx_rate

    price = quantize(base, marketplace.currency)
    if charm:
        price = charm_price(price, marketplace.currency)

    if price < floor:
        raise PricingError(
            f"computed price {price} is below the {floor} floor for "
            f"{marketplace.code} — refusing to list. Check the extracted web price "
            f"({web_price}) or set a Price Override."
        )

    if list_price is not None:
        list_price = quantize(list_price, marketplace.currency)
        if list_price <= price:
            # A strike-through that is not actually higher is a false discount claim, which
            # Amazon suppresses and the FTC treats as deceptive. Drop it rather than ship it.
            log.warning(
                "pricing.list_price_dropped",
                marketplace=marketplace.code,
                list_price=str(list_price),
                price=str(price),
                reason="list price not above selling price",
            )
            list_price = None

    fee = estimate_referral_fee(price) if marketplace.currency == "USD" else Decimal("0.00")
    quote = PriceQuote(
        marketplace=marketplace.code,
        currency=marketplace.currency,
        web_price=web_price if web_price is not None else price,
        price=price,
        list_price=list_price,
        fee_fraction=fee_fraction,
        estimated_referral_fee=fee,
        estimated_net=quantize(price - fee, marketplace.currency),
        source=source,
    )
    log.debug(
        "pricing.quote", **{"marketplace": marketplace.code, "price": str(price), "source": source}
    )
    return quote
