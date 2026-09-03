"""Tests for the sanitizer and the anti-hallucination validator.

These are the safety-critical tests in the codebase. A regression here does not produce a
crash — it produces a live listing making an unsupported claim about a gemstone, which is an
FTC matter. Every check therefore has an explicit test with the failure spelled out.
"""

from __future__ import annotations

import pytest

from anzorlist.generate.sanitize import has_promotional_language, normalize_text, sanitize
from anzorlist.generate.validate import FactSheet, validate_copy
from anzorlist.models.product import Product


class TestSanitizer:
    @pytest.mark.parametrize(
        "text,kind",
        [
            ("Call us at 555-123-4567 to order today.", "phone"),
            ("Email sales@example.com for pricing on this piece.", "email"),
            ("Visit https://example.com/rings for the full collection.", "url"),
            ("Click here to add this beautiful ring to your cart now.", "navigation"),
            ("Free shipping on this item, ships same day from our store.", "shipping_promise"),
            ("Backed by our 30-day money-back guarantee on all purchases.", "guarantee"),
            ("The lowest price anywhere, we will price match any competitor.", "price_claim"),
            ("Layaway and financing available on all pieces over $500.", "financing"),
        ],
    )
    def test_policy_violations_are_removed(self, text: str, kind: str):
        result = sanitize(text)
        assert kind in result.removed, f"{kind} was not detected in {text!r}"

    def test_clean_text_survives_intact(self):
        text = ("This handcrafted band features a channel setting with milgrain detail "
                "along both edges of the shank.")
        result = sanitize(text)
        assert result.removed_count == 0
        assert "milgrain" in result.text

    def test_gutted_sentences_are_dropped_not_shipped(self):
        """A sentence that is only a phone number must not become 'Our  is  .'"""
        result = sanitize("Handcrafted in our workshop. Call 555-123-4567. Solid 14k gold.")
        assert "Handcrafted" in result.text
        assert "gold" in result.text
        assert "  " not in result.text

    def test_mojibake_is_repaired(self):
        assert normalize_text("Anzor’s “Signature” line") == 'Anzor\'s "Signature" line'

    def test_empty_input_is_safe(self):
        assert sanitize("").text == ""


class TestPromotionalDetector:
    @pytest.mark.parametrize("word", ["best", "sale", "free", "guaranteed", "perfect gift"])
    def test_flags_prohibited_words(self, word: str):
        assert word in has_promotional_language(f"A {word} choice for any occasion")

    def test_does_not_flag_substrings(self):
        """'bezel' must not trip the 'best' rule."""
        assert has_promotional_language("A bezel-set stone in a forest-green setting") == []


class TestFactSheet:
    def test_extracts_gems_and_metals_from_specs(self, ring: Product):
        facts = FactSheet(ring)
        assert facts.gems, "R985 should have at least one gemstone in its specs"
        assert facts.metals, "R985 should have at least one metal in its specs"

    def test_number_normalisation_treats_equivalent_forms_as_equal(self, ring: Product):
        facts = FactSheet(ring)
        for number in list(facts.numbers)[:5]:
            assert facts.supports_number(number)


class TestAntiHallucination:
    """Every claim in generated copy must trace back to the source specs."""

    def _validate(self, product: Product, **kwargs):
        base = {"title": "Anzor Ring", "bullets": [], "description": "", "search_terms": ""}
        base.update(kwargs)
        return validate_copy(product=product, **base)

    def test_invented_carat_weight_is_rejected(self, ring: Product):
        report = self._validate(ring, title="Anzor 7.77 ct Sapphire Ring")
        assert not report.ok
        assert any(i.code == "UnsupportedNumber" for i in report.errors)

    def test_invented_gemstone_is_rejected(self, ring: Product):
        facts = FactSheet(ring)
        absent = next(g for g in ("tanzanite", "opal", "peridot") if g not in facts.gems)
        report = self._validate(ring, description=f"Set with a brilliant {absent} centre stone.")
        assert not report.ok
        assert any(i.code == "UnsupportedGemstone" for i in report.errors)

    def test_invented_metal_is_rejected(self, ring: Product):
        facts = FactSheet(ring)
        absent = next(m for m in ("titanium", "palladium", "tungsten") if m not in facts.metals)
        report = self._validate(ring, description=f"Crafted from solid {absent}.")
        assert not report.ok
        assert any(i.code == "UnsupportedMetal" for i in report.errors)

    def test_promotional_language_is_rejected(self, ring: Product):
        report = self._validate(ring, title="The Best Sapphire Ring — Free Gift Box")
        assert not report.ok
        assert any(i.code == "Promotional" for i in report.errors)

    def test_all_caps_title_is_rejected(self, ring: Product):
        report = self._validate(ring, title="SOLID GOLD SAPPHIRE RING FOR WOMEN")
        assert any(i.code == "AllCaps" for i in report.errors)

    def test_forbidden_title_characters_are_rejected(self, ring: Product):
        report = self._validate(ring, title="Anzor Sapphire Ring! Best Value $$$")
        assert any(i.code == "ForbiddenChar" for i in report.errors)

    def test_overlong_title_is_rejected(self, ring: Product):
        report = self._validate(ring, title="Anzor " + ("Ring " * 60))
        assert any(i.code == "TooLong" for i in report.errors)

    def test_unverifiable_certification_claim_is_rejected(self, ring: Product):
        report = self._validate(ring, description="Each stone is certified and conflict-free.")
        assert not report.ok
        assert any(i.code == "UnverifiableClaim" for i in report.errors)

    def test_benign_numbers_are_allowed(self, ring: Product):
        """Small counts are not product claims and must not produce noise."""
        report = self._validate(ring, description="Presented in a 1 piece gift box.")
        assert not any(i.code == "UnsupportedNumber" for i in report.errors)


class TestFtcRules:
    """The FTC Jewelry Guides checks, tested against a synthetic product so the source
    qualifiers are unambiguous."""

    @staticmethod
    def _synthetic(spec_value: str) -> Product:
        from datetime import datetime, timezone

        from anzorlist.models.product import ProductFamily, SpecRow

        return Product(
            sku="TEST1",
            family=ProductFamily.RING,
            source_url="https://example.test/p",
            marketing_title_raw="Test Ring",
            specs=[SpecRow(label="Gemstone", value=spec_value, provenance_key="k")],
            content_hash="0" * 64,
            fetched_at=datetime.now(timezone.utc),
        )

    def test_lab_grown_must_not_be_called_natural(self):
        product = self._synthetic("Lab-grown sapphire, 1.5 ct")
        report = validate_copy(
            product=product,
            title="Natural Sapphire Ring",
            bullets=[], description="", search_terms="",
        )
        assert not report.ok
        assert any(i.code == "FtcOriginUpgrade" for i in report.errors)

    def test_simulated_must_not_be_called_genuine(self):
        product = self._synthetic("Simulated diamond centre stone")
        report = validate_copy(
            product=product,
            title="Genuine Diamond Ring",
            bullets=[], description="", search_terms="",
        )
        assert any(i.code == "FtcOriginUpgrade" for i in report.errors)

    def test_plating_qualifier_must_not_be_dropped(self):
        from datetime import datetime, timezone

        from anzorlist.models.product import ProductFamily, SpecRow

        product = Product(
            sku="TEST2",
            family=ProductFamily.RING,
            source_url="https://example.test/p",
            marketing_title_raw="Test Ring",
            specs=[SpecRow(label="Metal", value="14k gold plated brass",
                           provenance_key="k")],
            content_hash="0" * 64,
            fetched_at=datetime.now(timezone.utc),
        )
        report = validate_copy(
            product=product,
            title="Solid Gold Ring",  # drops "plated" — the deceptive claim
            bullets=[], description="", search_terms="",
        )
        assert not report.ok
        assert any(i.code == "FtcPlatingOmitted" for i in report.errors)

    def test_keeping_the_qualifier_passes(self):
        from datetime import datetime, timezone

        from anzorlist.models.product import ProductFamily, SpecRow

        product = Product(
            sku="TEST3",
            family=ProductFamily.RING,
            source_url="https://example.test/p",
            marketing_title_raw="Test Ring",
            specs=[SpecRow(label="Metal", value="14k gold plated brass",
                           provenance_key="k")],
            content_hash="0" * 64,
            fetched_at=datetime.now(timezone.utc),
        )
        report = validate_copy(
            product=product,
            title="14k Gold Plated Band Ring",
            bullets=[], description="", search_terms="",
        )
        assert not any(i.code == "FtcPlatingOmitted" for i in report.errors)


class TestCaratGemAssociation:
    """Binding carat weights to the right stone.

    Regression guard for a real defect found on live SKU R1279, whose Item Details read
    ``.25 cwt. Diamonds 0.50ct. Aquamarine``. The original "nearest carat figure" search
    crossed a gem name and attached the aquamarine's 0.50 ct to the diamond — publishing a
    diamond weight double the true one. Overstating carat weight is an FTC Jewelry Guides
    violation, and the copy validator cannot catch it because the error is in the extracted
    attribute, not in generated prose.
    """

    @staticmethod
    def _parse(text: str):
        from anzorlist.extract.parser import ProductParser

        parser = ProductParser.__new__(ProductParser)
        parser.warnings = []
        return parser, {k: str(v) for k, v in parser._carat_by_gem(text).items()}

    def test_multi_stone_weight_precedes_gem(self):
        _, got = self._parse(".25 cwt. Diamonds 0.50ct. Aquamarine (Total Weights)")
        assert got == {"diamond": "0.25", "aquamarine": "0.50"}

    def test_multi_stone_weight_follows_gem(self):
        _, got = self._parse("Diamond 0.25 ct, Aquamarine 0.50 ct")
        assert got == {"diamond": "0.25", "aquamarine": "0.50"}

    @pytest.mark.parametrize("text", ["1.45 ct Sapphire", "Sapphire 1.45ct", "1.45 carat Sapphire"])
    def test_single_stone_either_orientation(self, text: str):
        _, got = self._parse(text)
        assert got == {"sapphire": "1.45"}

    def test_shared_total_weight_is_refused_not_split(self):
        """'Diamonds and Sapphires 2.00 ctw' gives no per-stone weight. Guessing would be wrong."""
        parser, got = self._parse("Diamonds and Sapphires 2.00 ctw")
        assert got == {}
        assert any(w.kind == "AmbiguousParse" for w in parser.warnings)

    def test_no_gems_yields_nothing(self):
        _, got = self._parse("Gemstone(s) --")
        assert got == {}

    def test_aquamarine_is_in_the_vocabulary(self):
        """It was absent, so the stone vanished from the listing entirely."""
        from anzorlist.extract.parser import GEM_TYPES

        for gem in ("Aquamarine", "Tanzanite", "Morganite", "Moissanite", "Opal"):
            assert gem in GEM_TYPES


class TestRealMultiStoneFixture:
    """End-to-end guard on the SKU that exposed the carat-association defect.

    The synthetic cases above test the algorithm; this tests the real page, so a future
    change to spec-row selection or title handling cannot quietly reintroduce the bug.
    """

    @staticmethod
    def _r1279():
        import hashlib
        from pathlib import Path

        from anzorlist.extract.client import FetchResult, SiteClient
        from anzorlist.extract.parser import parse_product

        path = Path(__file__).parent / "fixtures" / "R1279.html"
        raw = path.read_bytes()
        html, encoding = SiteClient._decode(raw)
        return parse_product(FetchResult(
            sku="R1279",
            url="https://www.anzorjewelrycorp.com/Scripts/prodview.asp?SKU=R1279",
            html=html, raw_bytes=raw, content_hash=hashlib.sha256(raw).hexdigest(),
            encoding=encoding, from_cache=True, cache_path=path,
        ))

    def test_source_row_really_is_two_stones(self):
        """Guards the premise: if the page changes, the rest of this class is meaningless."""
        gem_row = next(r for r in self._r1279().specs if "gem" in r.label.lower())
        assert "Diamond" in gem_row.value and "Aquamarine" in gem_row.value

    def test_diamond_gets_its_own_weight_not_the_aquamarine_s(self):
        from decimal import Decimal

        diamond = next(g for g in self._r1279().attributes.gemstones if g.type == "Diamond")
        assert diamond.carat_weight == Decimal("0.25"), (
            "the diamond must not inherit the aquamarine's 0.50 ct — overstating carat "
            "weight is an FTC Jewelry Guides violation"
        )

    def test_second_stone_is_not_dropped(self):
        from decimal import Decimal

        gems = {g.type: g.carat_weight for g in self._r1279().attributes.gemstones}
        assert "Aquamarine" in gems, "aquamarine was missing from GEM_TYPES and vanished"
        assert gems["Aquamarine"] == Decimal("0.50")
