"""Pricing tests.

The gross-up is the single most valuable calculation in the system and the easiest to get
subtly wrong, so it gets an explicit round-trip test: after Amazon takes its fee, the seller
must net the original web price.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from anzorlist.marketplaces import resolve
from anzorlist.pricing import (
    PricingError,
    charm_price,
    estimate_referral_fee,
    price_for,
    quantize,
)

US = resolve("US")
UK = resolve("UK")
JP = resolve("JP")


class TestGrossUp:
    def test_grossup_nets_the_web_price(self):
        """The whole point: web / (1 - f), then take f back, returns web."""
        web = Decimal("1000.00")
        quote = price_for(web, US, fee_fraction=Decimal("0.20"))
        assert quote.price == Decimal("1250.00")
        # A naive 20% markup would be 1200, netting only 960.
        assert quote.price * Decimal("0.80") == web

    def test_naive_markup_would_lose_margin(self):
        """Documents the bug this design exists to prevent."""
        web = Decimal("1000.00")
        naive = web * Decimal("1.20")
        assert naive * Decimal("0.80") < web  # 960 — a 4% silent loss

    @pytest.mark.parametrize("fee", ["0.05", "0.10", "0.15", "0.20", "0.30"])
    def test_grossup_is_exact_for_any_fee(self, fee: str):
        f = Decimal(fee)
        quote = price_for(Decimal("799.99"), US, fee_fraction=f)
        recovered = quote.price * (Decimal("1") - f)
        assert abs(recovered - Decimal("799.99")) <= Decimal("0.01")

    def test_fee_of_one_is_rejected(self):
        with pytest.raises(PricingError, match="below 1.0"):
            price_for(Decimal("100"), US, fee_fraction=Decimal("1.0"))


class TestOverride:
    def test_override_bypasses_grossup(self):
        quote = price_for(Decimal("1000"), US, fee_fraction=Decimal("0.20"),
                          override=Decimal("1099.00"))
        assert quote.price == Decimal("1099.00")
        assert quote.source == "override"

    def test_no_price_and_no_override_is_an_error(self):
        with pytest.raises(PricingError, match="no price available"):
            price_for(None, US, fee_fraction=Decimal("0.20"))


class TestFloor:
    def test_below_floor_refuses_rather_than_listing(self):
        with pytest.raises(PricingError, match="below the"):
            price_for(Decimal("5.00"), US, fee_fraction=Decimal("0.20"),
                      floor=Decimal("10.00"))


class TestListPrice:
    def test_list_price_at_or_below_selling_price_is_dropped(self):
        """A strike-through that is not higher is a false discount claim."""
        quote = price_for(Decimal("1000"), US, fee_fraction=Decimal("0.20"),
                          list_price=Decimal("1100"))
        assert quote.price == Decimal("1250.00")
        assert quote.list_price is None  # 1100 < 1250, so it was dropped

    def test_genuine_list_price_survives(self):
        quote = price_for(Decimal("1000"), US, fee_fraction=Decimal("0.20"),
                          list_price=Decimal("1999"))
        assert quote.list_price == Decimal("1999.00")


class TestCurrency:
    def test_non_usd_without_fx_rate_is_refused(self):
        """A missing rate must never silently default to 1.0."""
        with pytest.raises(PricingError, match="no FX rate"):
            price_for(Decimal("1000"), UK, fee_fraction=Decimal("0.20"))

    def test_fx_rate_is_applied(self):
        quote = price_for(Decimal("1000"), UK, fee_fraction=Decimal("0.20"),
                          fx_rate=Decimal("0.79"))
        assert quote.price == Decimal("987.50")  # 1250 * 0.79
        assert quote.currency == "GBP"

    def test_zero_decimal_currency_has_no_minor_unit(self):
        quote = price_for(Decimal("1000"), JP, fee_fraction=Decimal("0.20"),
                          fx_rate=Decimal("150"))
        assert quote.price == quote.price.to_integral_value()
        assert "." not in str(quote.price)


class TestRounding:
    def test_half_up_not_bankers(self):
        """Retail rounds half away from zero; Python's default would round 0.125 down."""
        assert quantize(Decimal("10.125")) == Decimal("10.13")
        assert quantize(Decimal("10.135")) == Decimal("10.14")

    def test_charm_price_never_reduces_the_price(self):
        for raw in ("100.00", "100.01", "100.50", "100.99", "101.00"):
            price = Decimal(raw)
            assert charm_price(price) >= price


class TestReferralFee:
    def test_tiered_fee_above_breakpoint(self):
        # 250 * 0.20 + 750 * 0.05 = 50 + 37.50
        assert estimate_referral_fee(Decimal("1000")) == Decimal("87.50")

    def test_flat_fee_below_breakpoint(self):
        assert estimate_referral_fee(Decimal("200")) == Decimal("40.00")

    def test_minimum_fee_applies(self):
        assert estimate_referral_fee(Decimal("5")) == Decimal("2.00")
