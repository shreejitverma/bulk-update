"""End to end: workbook -> build -> ebay submit, against a fake eBay."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fake_ebay import FakeEbay
from test_amazon_upload_e2e import build_ok, env, run  # noqa: F401 - env is a fixture

from anzorlist import cli
from anzorlist.channels.ebay.client import EbayClient
from anzorlist.config import settings as get_settings
from anzorlist.store.db import Ledger


@pytest.fixture
def ebay(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> FakeEbay:  # noqa: F811
    for key, value in {
        "EBAY_CLIENT_ID": "app-id",
        "EBAY_CLIENT_SECRET": "cert-id",
        "EBAY_REFRESH_TOKEN": "v^1.1#refresh",
        "EBAY_FULFILLMENT_POLICY_ID": "F1",
        "EBAY_PAYMENT_POLICY_ID": "P1",
        "EBAY_RETURN_POLICY_ID": "R1",
        "EBAY_MERCHANT_LOCATION_KEY": "home",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    fake = FakeEbay()
    monkeypatch.setattr(cli, "_make_ebay_client", lambda s: EbayClient(s, transport=fake.transport))
    return fake


def test_build_writes_an_ebay_listing_per_sku(env: dict[str, Any]) -> None:  # noqa: F811
    build_ok()
    ring = json.loads((env["data"] / "ebay" / "build" / "R985.json").read_text())
    assert ring["group_key"] == "R985-GROUP"
    assert ring["category_id"] == "261994"
    assert len(ring["items"]) == 29
    assert len(ring["title"]) <= 80


def test_submit_publishes_group_and_single_listing(
    env: dict[str, Any],  # noqa: F811
    ebay: FakeEbay,
) -> None:
    build_ok()
    result = run("ebay", "submit", "--confirm", input="y\n")
    assert result.exit_code == 0, result.output
    assert sorted(ebay.published) == ["E1154", "R985-GROUP"]
    group = ebay.groups["R985-GROUP"]
    assert group["variesBy"]["specifications"][0]["name"] == "Ring Size"
    assert set(group["variantSKUs"]) <= set(ebay.items)
    # Each size's offer carries its own price, and the group listing gets a category.
    offers = {o["sku"]: o for o in ebay.offers.values()}
    assert (
        offers["R985-7"]["pricingSummary"]["price"] != offers["R985-12"]["pricingSummary"]["price"]
    )
    assert offers["E1154"]["categoryId"] == "261990"
    assert offers["E1154"]["listingPolicies"]["returnPolicyId"] == "R1"

    # Idempotent: a second run sends nothing.
    again = run("ebay", "submit", "--confirm", input="y\n")
    assert again.exit_code == 0 and "up to date" in again.output
    assert sorted(ebay.published) == ["E1154", "R985-GROUP"]


def test_rerun_after_a_rejection_updates_offers_instead_of_duplicating(
    env: dict[str, Any],  # noqa: F811
    ebay: FakeEbay,
) -> None:
    build_ok()
    ebay.reject_publish.add("E1154")
    first = run("ebay", "submit", "--confirm", "E1154", input="y\n")
    assert first.exit_code == 1 and "25002" in first.output
    offers_after_first = len(ebay.offers)
    ebay.reject_publish.clear()
    second = run("ebay", "submit", "--confirm", "E1154", input="y\n")
    assert second.exit_code == 0, second.output
    assert len(ebay.offers) == offers_after_first  # the existing offer was updated
    with Ledger(get_settings().state_db) as ledger:
        assert "E1154" in {e.sku for e in ledger.live_skus("EBAY_US")}


def test_publishing_is_gated(
    env: dict[str, Any],  # noqa: F811
    ebay: FakeEbay,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_ok()
    assert run("ebay", "submit").exit_code == 2
    monkeypatch.setenv("ANZOR_ALLOW_LIVE", "false")
    get_settings.cache_clear()
    assert run("ebay", "submit", "--confirm").exit_code == 2
    assert ebay.calls == []


def test_blocked_listing_is_not_sent(env: dict[str, Any], ebay: FakeEbay) -> None:  # noqa: F811
    result = run("build", "--no-copy", "--no-upload")
    assert result.exit_code == 1
    assert run("ebay", "submit", "--confirm", input="y\n").exit_code == 1
    assert ebay.published == []
