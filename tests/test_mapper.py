"""Mapper tests: attribute shapes, precedence, variation families, and the safety gates."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from anzorlist.channels.amazon.mapper import AmazonMapper, attr, localized
from anzorlist.channels.etsy.mapper import EtsyMapper
from anzorlist.config import Settings
from anzorlist.generate.copy import ListingCopy, fallback_copy
from anzorlist.ingest.row import ListingRow
from anzorlist.models.product import Gemstone
from anzorlist.pricing import price_for


@pytest.fixture
def copy_obj() -> ListingCopy:
    return ListingCopy(
        title="Anzor Jewelry 18k Yellow Gold Ring",
        bullets=["Solid 18k yellow gold", "Handcrafted setting", "Presented in a gift box"],
        description="A handcrafted band in solid 18k yellow gold.",
        search_terms="band jewellery goldsmith",
    )


@pytest.fixture
def quote(us):
    return price_for(Decimal("1000.00"), us, fee_fraction=Decimal("0.20"))


def build(mapper, product, row, us, copy_obj, quote, images=("https://cdn.test/a.jpg",)):
    return mapper.build(
        product=product,
        row=row,
        marketplace=us,
        copy=copy_obj,
        quote=quote,
        image_urls=list(images),
    )


class TestAttributeShape:
    """Amazon requires a list of marketplace-scoped objects for every attribute."""

    def test_attr_wraps_in_a_marketplace_scoped_list(self, us):
        assert attr("Anzor", us) == [{"value": "Anzor", "marketplace_id": us.marketplace_id}]

    def test_customer_facing_text_carries_a_language_tag(self, us):
        out = localized("A ring", us)[0]
        assert out["language_tag"] == us.locale
        assert out["marketplace_id"] == us.marketplace_id

    def test_every_attribute_is_a_list_of_dicts(self, settings, ring, us, copy_obj, quote):
        listings = build(
            AmazonMapper(settings),
            ring,
            ListingRow(sku=ring.sku, variation_source="none"),
            us,
            copy_obj,
            quote,
        )
        for key, value in listings[0].attributes.items():
            assert isinstance(value, list), f"{key} is not a list"
            assert all(isinstance(v, dict) for v in value), f"{key} holds non-dict entries"
            assert all("marketplace_id" in v for v in value), f"{key} is not marketplace-scoped"


class TestVariations:
    def test_ring_sizes_become_a_parent_child_family(self, settings, ring, us, copy_obj, quote):
        listings = build(
            AmazonMapper(settings),
            ring,
            ListingRow(sku=ring.sku, variation_source="site"),
            us,
            copy_obj,
            quote,
        )
        parents = [x for x in listings if x.is_parent]
        children = [x for x in listings if x.parent_sku]
        assert len(parents) == 1
        assert len(children) >= 2
        assert all(c.parent_sku == parents[0].sku for c in children)

    def test_parent_is_listed_first(self, settings, ring, us, copy_obj, quote):
        """Amazon requires the parent to exist before children reference it."""
        listings = build(
            AmazonMapper(settings),
            ring,
            ListingRow(sku=ring.sku, variation_source="site"),
            us,
            copy_obj,
            quote,
        )
        assert listings[0].is_parent

    def test_parent_carries_no_offer(self, settings, ring, us, copy_obj, quote):
        """A parent is a container, never buyable. An offer on it breaks the family grouping."""
        listings = build(
            AmazonMapper(settings),
            ring,
            ListingRow(sku=ring.sku, variation_source="site"),
            us,
            copy_obj,
            quote,
        )
        parent = listings[0]
        assert parent.offer is None
        assert "purchasable_offer" not in parent.attributes
        assert "fulfillment_availability" not in parent.attributes

    def test_children_carry_offers_and_a_size(self, settings, ring, us, copy_obj, quote):
        listings = build(
            AmazonMapper(settings),
            ring,
            ListingRow(sku=ring.sku, variation_source="site"),
            us,
            copy_obj,
            quote,
        )
        for child in listings[1:]:
            assert child.offer is not None
            assert "purchasable_offer" in child.attributes
            assert "size" in child.attributes

    def test_variation_theme_is_consistent_across_the_family(
        self, settings, ring, us, copy_obj, quote
    ):
        listings = build(
            AmazonMapper(settings),
            ring,
            ListingRow(sku=ring.sku, variation_source="site"),
            us,
            copy_obj,
            quote,
        )
        themes = {x.attributes["variation_theme"][0]["name"] for x in listings}
        assert themes == {"SIZE"}

    def test_child_skus_are_unique_and_stable(self, settings, ring, us, copy_obj, quote):
        mapper = AmazonMapper(settings)
        row = ListingRow(sku=ring.sku, variation_source="site")
        first = [x.sku for x in build(mapper, ring, row, us, copy_obj, quote)]
        second = [x.sku for x in build(mapper, ring, row, us, copy_obj, quote)]
        assert first == second, "child SKUs must not change between runs"
        assert len(set(first)) == len(first), "child SKUs must be unique"

    def test_size_price_deltas_are_applied(self, settings, ring, us, copy_obj, quote):
        """Larger ring sizes use more metal; the site prices that, and so must we."""
        listings = build(
            AmazonMapper(settings),
            ring,
            ListingRow(sku=ring.sku, variation_source="site"),
            us,
            copy_obj,
            quote,
        )
        prices = {x.offer.price for x in listings if x.offer}
        assert len(prices) > 1, "expected at least one size to carry a price delta"

    def test_variation_source_none_gives_one_standalone(self, settings, ring, us, copy_obj, quote):
        listings = build(
            AmazonMapper(settings),
            ring,
            ListingRow(sku=ring.sku, variation_source="none"),
            us,
            copy_obj,
            quote,
        )
        assert len(listings) == 1
        assert not listings[0].is_parent
        assert listings[0].offer is not None

    def test_earrings_have_no_family(self, settings, earrings, us, copy_obj, quote):
        listings = build(
            AmazonMapper(settings),
            earrings,
            ListingRow(sku=earrings.sku, variation_source="site"),
            us,
            copy_obj,
            quote,
        )
        assert len(listings) == 1


class TestProductIdentifier:
    def test_no_upc_uses_the_gtin_exemption_flag(self, settings, ring, us, copy_obj, quote):
        listings = build(
            AmazonMapper(settings),
            ring,
            ListingRow(sku=ring.sku, variation_source="none"),
            us,
            copy_obj,
            quote,
        )
        a = listings[0].attributes
        assert a["supplier_declared_has_product_identifier_exemption"][0]["value"] is True
        assert "externally_assigned_product_identifier" not in a

    def test_valid_upc_is_sent_with_its_type(self, settings, ring, us, copy_obj, quote):
        row = ListingRow(sku=ring.sku, variation_source="none", upc_ean="036000291452")
        listings = build(AmazonMapper(settings), ring, row, us, copy_obj, quote)
        ident = listings[0].attributes["externally_assigned_product_identifier"][0]
        assert ident["type"] == "upc"
        assert ident["value"] == "036000291452"

    def test_invalid_check_digit_is_rejected_at_the_row(self):
        with pytest.raises(ValueError, match="check digit"):
            ListingRow(sku="R985", upc_ean="036000291453")


class TestPrecedence:
    def test_spreadsheet_override_beats_the_website(self, settings, ring, us, copy_obj, quote):
        row = ListingRow(
            sku=ring.sku, variation_source="none", item_name_override="My Own Title For This Ring"
        )
        listings = build(AmazonMapper(settings), ring, row, us, copy_obj, quote)
        assert listings[0].attributes["item_name"][0]["value"] == "My Own Title For This Ring"

    def test_blank_cell_defers_to_the_generated_value(self, settings, ring, us, copy_obj, quote):
        row = ListingRow(sku=ring.sku, variation_source="none")
        listings = build(AmazonMapper(settings), ring, row, us, copy_obj, quote)
        assert listings[0].attributes["item_name"][0]["value"] == copy_obj.title

    def test_two_tone_metal_is_omitted_not_guessed(self, settings, earrings, us, copy_obj, quote):
        """Amazon has no two-tone value; picking one colour would be a false claim."""
        if not (earrings.attributes.metal_color or "").startswith("Two-Tone"):
            pytest.skip("fixture is not two-tone")
        listings = build(
            AmazonMapper(settings),
            earrings,
            ListingRow(sku=earrings.sku, variation_source="none"),
            us,
            copy_obj,
            quote,
        )
        assert "metal_type" not in listings[0].attributes

    def test_metal_type_override_fills_the_gap(self, settings, earrings, us, copy_obj, quote):
        row = ListingRow(sku=earrings.sku, variation_source="none", metal_type="white_gold")
        listings = build(AmazonMapper(settings), earrings, row, us, copy_obj, quote)
        assert listings[0].attributes["metal_type"][0]["value"] == "white_gold"


class TestIssueDetection:
    def test_missing_main_image_is_blocking(self, settings, ring, us, copy_obj, quote):
        listings = build(
            AmazonMapper(settings),
            ring,
            ListingRow(sku=ring.sku, variation_source="none"),
            us,
            copy_obj,
            quote,
            images=(),
        )
        assert not listings[0].submittable
        assert any(i.code == "NoMainImage" for i in listings[0].blocking_issues)

    def test_gtin_exemption_without_brand_registry_warns(self, settings, ring, us, copy_obj, quote):
        listings = build(
            AmazonMapper(settings),
            ring,
            ListingRow(sku=ring.sku, variation_source="none"),
            us,
            copy_obj,
            quote,
        )
        assert any(i.code == "GtinExemptionUnverified" for i in listings[0].issues)
        assert listings[0].submittable  # a warning, not a blocker

    def test_payload_hash_is_deterministic(self, settings, ring, us, copy_obj, quote):
        mapper = AmazonMapper(settings)
        row = ListingRow(sku=ring.sku, variation_source="none")
        a = build(mapper, ring, row, us, copy_obj, quote)[0]
        b = build(mapper, ring, row, us, copy_obj, quote)[0]
        assert a.payload_hash == b.payload_hash


class TestProductType:
    @pytest.mark.parametrize(
        "sku,expected",
        [("R985", "RING"), ("E1154", "EARRING"), ("S220", "JEWELRY_SET"), ("E711", "EARRING")],
    )
    def test_inferred_from_sku_prefix(self, settings, products, sku, expected):
        mapper = AmazonMapper(settings)
        assert mapper.product_type(products[sku], ListingRow(sku=sku)) == expected

    def test_spreadsheet_override_wins(self, settings, ring):
        mapper = AmazonMapper(settings)
        row = ListingRow(sku=ring.sku, amazon_product_type="NECKLACE")
        assert mapper.product_type(ring, row) == "NECKLACE"


class TestFallbackCopy:
    def test_fallback_only_restates_extracted_specs(self, ring):
        """The fallback cannot hallucinate because it copies spec rows verbatim."""
        from anzorlist.generate.validate import validate_copy

        copy = fallback_copy(ring, "Anzor Jewelry")
        report = validate_copy(
            product=ring,
            title=copy.title,
            bullets=copy.bullets,
            description=copy.description,
            search_terms=copy.search_terms,
        )
        assert report.ok, [str(i) for i in report.errors]


class TestStonesAndSizes:
    """Every stone is named, a total is a real total, and each child names its size."""

    def test_every_stone_is_named_and_the_total_sums_them(
        self, settings, earrings, us, copy_obj, quote
    ):
        [listing] = build(
            AmazonMapper(settings),
            earrings,
            ListingRow(sku=earrings.sku, variation_source="none"),
            us,
            copy_obj,
            quote,
        )
        # E1154 carries 1.30 ct of diamond and 0.65 ct of emerald; the old mapper said 1.30.
        assert [g["value"] for g in listing.attributes["gem_type"]] == ["diamond", "emerald"]
        assert listing.attributes["total_gem_weight"][0]["value"] == pytest.approx(1.95)

    def test_a_partial_total_is_omitted_with_a_warning(
        self, settings, earrings, us, copy_obj, quote
    ):
        partial = earrings.model_copy(deep=True)
        partial.attributes.gemstones[1].carat_weight = None
        [listing] = build(
            AmazonMapper(settings),
            partial,
            ListingRow(sku=partial.sku, variation_source="none"),
            us,
            copy_obj,
            quote,
        )
        assert "total_gem_weight" not in listing.attributes
        [warning] = [i for i in listing.issues if i.code == "GemWeightOmitted"]
        assert not warning.blocking and "Emerald" in warning.message

    def test_gem_type_none_drops_the_extracted_weight(
        self, settings, earrings, us, copy_obj, quote
    ):
        [listing] = build(
            AmazonMapper(settings),
            earrings,
            ListingRow(sku=earrings.sku, variation_source="none", gem_type="none"),
            us,
            copy_obj,
            quote,
        )
        assert "gem_type" not in listing.attributes
        assert "total_gem_weight" not in listing.attributes

    def test_children_carry_a_clean_size_and_name_it_in_the_title(
        self, settings, ring, us, copy_obj, quote
    ):
        listings = build(
            AmazonMapper(settings), ring, ListingRow(sku=ring.sku), us, copy_obj, quote
        )
        parent, first = listings[0], listings[1]
        assert parent.attributes["item_name"][0]["value"] == copy_obj.title
        # The site's option text is "Size 7 (Women's Avg)"; the size selector shows "7".
        assert first.attributes["size"][0]["value"] == "7"
        assert first.attributes["item_name"][0]["value"] == f"{copy_obj.title}, Size 7"

    def test_an_unmappable_metal_warns_instead_of_guessing(
        self, settings, earrings, us, copy_obj, quote
    ):
        # E1154 is two-tone white and rose gold; Amazon's metal_type has no single value for it.
        [listing] = build(
            AmazonMapper(settings),
            earrings,
            ListingRow(sku=earrings.sku, variation_source="none"),
            us,
            copy_obj,
            quote,
        )
        assert "metal_type" not in listing.attributes
        [warning] = [i for i in listing.issues if i.code == "MetalTypeUnresolved"]
        assert not warning.blocking and "Two-Tone" in warning.message

    def test_fallback_title_names_every_stone(self, earrings):
        assert "Diamond and Emerald" in fallback_copy(earrings, "Anzor Jewelry").title


class TestEtsyMapper:
    def _build(self, settings, product, copy_obj, quote, row=None, files=("/p/main.jpg",)):
        return EtsyMapper(settings).build(
            product=product,
            row=row or ListingRow(sku=product.sku),
            copy=copy_obj,
            quote=quote,
            image_files=list(files),
        )

    def test_three_stones_use_one_ampersand(self, settings, ring, copy_obj, quote):
        product = ring.model_copy(deep=True)
        product.attributes.gemstones = [
            Gemstone(type=t, provenance_key="test") for t in ("Diamond", "Ruby", "Sapphire")
        ]
        listing = self._build(settings, product, copy_obj, quote)
        assert "Diamond, Ruby & Sapphire" in listing.title
        assert listing.submittable

    def test_a_title_override_with_two_ampersands_is_held(self, settings, ring, copy_obj, quote):
        row = ListingRow(sku=ring.sku, item_name_override="Gold & Diamond & Ruby Ring")
        listing = self._build(settings, ring, copy_obj, quote, row=row)
        assert [i.code for i in listing.blocking_issues] == ["EtsyTitleRule"]

    def test_tiff_photos_are_held(self, settings, ring, copy_obj, quote):
        listing = self._build(settings, ring, copy_obj, quote, files=("/p/a.jpg", "/p/b.tif"))
        [issue] = listing.blocking_issues
        assert issue.code == "EtsyImageFormat" and "b.tif" in issue.message

    def test_who_made_drives_the_handmade_tag(self, settings, ring, copy_obj, quote):
        own = self._build(settings, ring, copy_obj, quote)
        assert own.who_made == "i_did" and "handmade jewelry" in own.tags
        bought = self._build(
            settings.model_copy(update={"etsy_who_made": "someone_else"}), ring, copy_obj, quote
        )
        assert bought.listing_fields()["who_made"] == "someone_else"
        assert "handmade jewelry" not in bought.tags

    def test_who_made_is_validated(self):
        with pytest.raises(ValidationError, match="ETSY_WHO_MADE"):
            Settings(_env_file=None, ETSY_WHO_MADE="me")  # type: ignore[call-arg]
